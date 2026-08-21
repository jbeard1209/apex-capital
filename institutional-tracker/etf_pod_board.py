"""
APEX CAPITAL — ETF POD BOARD (Pod 3: Macro / ETF Rotation)
============================================================
Three-member deliberative board that votes on ETF trade orders for Pod 3.

Board members:
  1. ETF Investment Director  — momentum-based allocation calls
  2. ETF Risk Officer         — risk flags (NO veto over execution)
  3. ETF Strategist           — rotation coherence + diversification check

Deliberation process:
  1. Load etf_research_brief.json (from etf_research_agent.py)
  2. Load pod3_macro pod state from pod_ledger
  3. Each board member produces their position + vote (APPROVE / MODIFY / REJECT)
  4. Majority wins (2-of-3). Director + Strategist overrule Risk Officer if needed.
  5. Generate BUY/SELL orders vs current pod3 positions
  6. Write orders to data/etf_pod_orders.json
  7. Write board minutes to data/etf_board_meeting.json
  8. Send email summary (graceful skip if email system unavailable)

IMPORTANT — Risk Officer governance:
  The ETF Risk Officer CANNOT veto execution. Risk flags are logged and
  acknowledged in the orders file, but if Director + Strategist both vote
  APPROVE/MODIFY, execution proceeds regardless of Risk Officer's vote.
  The Risk Officer's role is to ensure risks are documented, not to block trades.

Execution pathway:
  The Manager Agent reads etf_pod_orders.json and executes all orders. No veto.
  Budget enforced by pod_ledger.can_trade() before each order.
"""

import json
import fcntl
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE = Path(__file__).parent
DATA = BASE / "data"

BRIEF_PATH    = DATA / "etf_research_brief.json"
ORDERS_PATH   = DATA / "etf_pod_orders.json"
MINUTES_PATH  = DATA / "etf_board_meeting.json"
LOCK_FILE     = DATA / ".etf_pod_board.lock"
MACRO_PATH    = DATA / "pod3_macro_signals.json"    # pod_macro_agent scored ETFs
CANARY_ALERT  = DATA / "canary_alert.json"
CANARY_STATE  = DATA / "canary_state.json"

POD_ID = "pod3_macro"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ETF_UNIVERSE: List[str] = [
    "XLK", "XLE", "XLF", "XLU", "XLP", "XLV",
    "XLI", "GLD", "TLT", "QQQ", "XLY", "XLB",
]
DEFENSIVE_ETFS: List[str] = ["XLU", "XLP", "GLD", "TLT"]

# ETF -> sector asset-class grouping (for diversification check)
ETF_ASSET_CLASS: Dict[str, str] = {
    "XLK":  "equity_tech",
    "QQQ":  "equity_tech",
    "XLE":  "equity_energy",
    "XLF":  "equity_financial",
    "XLU":  "equity_utility",
    "XLP":  "equity_staples",
    "XLV":  "equity_health",
    "XLI":  "equity_industrial",
    "XLY":  "equity_discretionary",
    "XLB":  "equity_materials",
    "GLD":  "commodities",
    "TLT":  "bonds",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ETF_BOARD] %(levelname)s %(message)s",
)
log = logging.getLogger("etf_pod_board")


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

class SingleInstanceLock:
    """Prevents multiple simultaneous board meetings via fcntl.flock."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        try:
            self._fh = open(self.lock_path, "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            if self._fh:
                self._fh.close()
            return False

    def release(self) -> None:
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_brief() -> dict:
    with open(BRIEF_PATH, "r") as fh:
        return json.load(fh)


def _load_macro_scores() -> dict:
    """
    Load pod_macro_agent's pre-scored ETF momentum rankings from pod3_macro_signals.json.
    Returns a dict of {ticker: momentum_score} or {} if unavailable.
    These scores are injected into the brief's ETF entries so the board sees
    both the research agent's raw data AND the macro agent's composite scores.
    """
    try:
        with open(MACRO_PATH, "r") as fh:
            data = json.load(fh)
        # Format: {"ranked_etfs": [{"ticker": "XLK", "momentum_score": 19.67, ...}, ...]}
        ranked = data.get("ranked_etfs", [])
        scores = {entry["ticker"]: entry.get("momentum_score", 0.0) for entry in ranked}
        log.info("Macro scores loaded from pod_macro_agent: %d ETFs scored", len(scores))
        return scores
    except FileNotFoundError:
        log.info("pod3_macro_signals.json not found — macro scores unavailable this session")
        return {}
    except Exception as exc:
        log.warning("pod3_macro_signals.json load error: %s — proceeding without macro scores", exc)
        return {}


def _load_canary_signals() -> dict:
    """
    Load canary alert signals written by the canary agent.
    Reads data/canary_alert.json; gracefully returns {} if missing or malformed.
    Active alerts are injected into the regime section before board deliberation.
    """
    try:
        with open(CANARY_ALERT, "r") as fh:
            data = json.load(fh)
        log.info("Canary signals loaded: %d key(s)", len(data))
        return data
    except FileNotFoundError:
        log.info("canary_alert.json not found — no canary signals this session.")
        return {}
    except json.JSONDecodeError as exc:
        log.warning("canary_alert.json malformed: %s — treating as empty", exc)
        return {}


def _check_market_emergency(brief: dict) -> Tuple[bool, str]:
    """
    Inspect the research brief's regime section for emergency market conditions.

    Returns (is_emergency: bool, reason: str).

    Emergency triggers (any one is sufficient):
      1. VIX > 30  (severe stress)
      2. SPY 1-day drop > 4%  (brief["regime"]["spy_1d_return"] < -4.0)
      3. Regime == "bear" AND VIX > 25

    If an emergency is detected, the normal board deliberation is bypassed and
    EMERGENCY_EXIT orders are written for all current pod3 positions.
    """
    regime = brief.get("regime", {})
    vix    = regime.get("vix", 0.0)
    spy_1d = regime.get("spy_1d_return")        # None if unavailable
    regime_label = regime.get("regime_label", "")

    if vix > 30:
        return True, f"VIX={vix:.1f} exceeds 30 — severe market stress"

    if spy_1d is not None and spy_1d < -4.0:
        return True, f"SPY 1-day drop of {spy_1d:.2f}% exceeds -4% threshold"

    if regime_label == "bear" and vix > 25:
        return True, f"Bear regime confirmed with VIX={vix:.1f}>25"

    return False, ""


def _generate_emergency_exit_orders(pod_state: dict) -> List[dict]:
    """
    Generate EMERGENCY_EXIT SELL orders for all current pod3 positions.
    Used when _check_market_emergency() returns True.
    """
    current_pos = pod_state.get("positions", {})
    orders: List[dict] = []
    for ticker, pos in current_pos.items():
        qty   = pos.get("qty", 0)
        price = pos.get("current_price", pos.get("avg_entry", 0.0))
        if qty <= 0:
            continue
        dollar_value = round(qty * price, 2)
        orders.append({
            "action": "EMERGENCY_EXIT",
            "ticker": ticker,
            "qty": qty,
            "price": round(price, 2),
            "dollar_value": dollar_value,
            "reason": "EMERGENCY EXIT — market emergency conditions detected by board.",
        })
        log.warning(
            "EMERGENCY_EXIT %s qty=%.0f @ $%.2f ($%.0f)",
            ticker, qty, price, dollar_value,
        )
    return orders


def _load_pod_state() -> dict:
    """Load current pod3_macro state from pod_ledger."""
    try:
        sys.path.insert(0, str(BASE))
        import pod_ledger
        return pod_ledger.get_pod_summary(POD_ID)
    except Exception as exc:
        log.warning("Could not load pod3 state from ledger: %s — using empty state", exc)
        return {
            "pod_id": POD_ID,
            "budget": 30000.0,
            "budget_remaining": 30000.0,
            "cash_balance": 30000.0,
            "deployed_value": 0.0,
            "positions_value": 0.0,
            "total_value": None,
            "positions": {},
        }


def _pod_budget(pod_state: dict) -> float:
    """The pod's sizing denominator: its BUDGET, a constant it was granted.

    This module used to size against `pod_state["total_value"]`, which was
    budget headroom plus market value. Two problems with that as a denominator:
    it moved with prices, so the "60% of budget" concentration test meant a
    different dollar amount every day; and after a drawdown it shrank, which is
    exactly backwards — a pod that has lost money is not entitled to less than
    its mandate, it is entitled to the same mandate with less deployed.

    `total_value` is now None on purpose, so a stale reader gets a TypeError
    rather than a plausible wrong number. (2026-08-19)
    """
    for key in ("budget", "budget_inception"):
        v = pod_state.get(key)
        try:
            v = float(v)
            if v == v and v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 30000.0


def _pod_unspent(pod_state: dict) -> float:
    """Unspent entitlement. NOT cash this pod holds — there is one cash balance
    and it is at the broker (see pod_ledger.get_firm_summary)."""
    v = pod_state.get("budget_remaining")
    if v is None:
        v = pod_state.get("cash_balance")
    try:
        v = float(v)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Scoring helper (shared across board members)
# ---------------------------------------------------------------------------

def _momentum_score(etf_entry: dict) -> float:
    """
    Momentum score formula (consistent with pod_macro_agent):
      score = (ret_1m * 0.4) + (ret_3m * 0.3) + (pct_from_50ma * 0.2) - (volatility_20d * 0.1)
    """
    r1m  = etf_entry.get("price_return_1m", 0.0)
    r3m  = etf_entry.get("price_return_3m", 0.0)
    p50  = etf_entry.get("pct_from_50ma", 0.0)
    vol  = etf_entry.get("volatility_20d", 0.0)
    return round((r1m * 0.4) + (r3m * 0.3) + (p50 * 0.2) - (vol * 0.1), 4)


# ---------------------------------------------------------------------------
# LLM reasoning layer (Fable 5 / any apex_llm model)
# ---------------------------------------------------------------------------

_ETF_BOARD_SYSTEM = """You are a member of the Apex Capital ETF Pod Board (Pod 3: Macro/ETF Rotation).
Pod 3 manages a $10k macro ETF rotation strategy. Universe: XLK, XLE, XLF, XLU, XLP, XLV, XLI, GLD, TLT, QQQ, XLY, XLB.
Defensive ETFs (bear/stress): XLU, XLP, GLD, TLT.

Vote on the proposed rotation: APPROVE | MODIFY | REJECT
Reply in exactly this JSON format (no other text):
{"vote": "APPROVE" | "MODIFY" | "REJECT", "reasoning": "one to two concise sentences"}"""


def _etf_llm_vote(role: str, context: str) -> "tuple[str, str]":
    try:
        from apex_llm import reason as _r, is_available
        if not is_available("etf_pod_board"):
            return None, None
        import json as _j
        raw = _r(_ETF_BOARD_SYSTEM, f"You are the {role}.\n{context}",
                 agent_name="etf_pod_board", max_tokens=200)
        if not raw:
            return None, None
        p = _j.loads(raw.strip())
        v, r = p.get("vote", "").upper(), p.get("reasoning", "")
        return (v, r) if v in ("APPROVE", "MODIFY", "REJECT") and r else (None, None)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Board Member 1: ETF Investment Director
# ---------------------------------------------------------------------------

def _etf_investment_director(brief: dict) -> dict:
    """
    ETF Investment Director — momentum-based allocation calls.

    Logic:
    - If regime is bear (bull=False AND vix>25): restrict to DEFENSIVE_ETFS only
    - If SPY RSI > 75 (overbought): max 2 ETFs, prefer value/defensive
    - Otherwise: pick top 3 by momentum_score from full universe

    Returns: recommended_etfs, reasoning, vote
    """
    regime = brief.get("regime", {})
    etfs   = brief.get("etfs", {})

    bull        = regime.get("bull", True)
    vix         = regime.get("vix", 20.0)
    spy_rsi     = regime.get("spy_rsi", 50.0)
    bear_mode   = (not bull) and (vix > 25)
    overbought  = spy_rsi > 75

    # Score all ETFs in universe that appear in brief
    scored: List[Tuple[str, float, dict]] = []
    for etf in ETF_UNIVERSE:
        entry = etfs.get(etf)
        if entry is None:
            continue
        score = _momentum_score(entry)
        scored.append((etf, score, entry))

    scored.sort(key=lambda x: x[1], reverse=True)

    reasoning_parts: List[str] = []

    if bear_mode:
        # Restrict to defensive ETFs only
        defensive = [(e, s, d) for (e, s, d) in scored if e in DEFENSIVE_ETFS]
        recommended = [e for (e, s, d) in defensive[:3]]
        reasoning_parts.append(
            f"BEAR MODE active (bull=False, VIX={vix:.1f}>25). "
            f"Restricted to defensive ETFs: {DEFENSIVE_ETFS}."
        )
        if recommended:
            reasoning_parts.append(f"Top defensive picks by momentum: {recommended}")
        else:
            recommended = ["XLU", "GLD", "TLT"]
            reasoning_parts.append("No defensive ETFs in brief — using default fallback: XLU, GLD, TLT")

    elif overbought:
        # SPY overbought: reduce to 2 ETFs max, prefer value/defensive
        # Prefer GLD, TLT, XLP, XLU even if not top by momentum
        defensive_scored = [(e, s, d) for (e, s, d) in scored if e in DEFENSIVE_ETFS]
        non_tech_scored  = [(e, s, d) for (e, s, d) in scored if e not in ("XLK", "QQQ")]

        # Pick 2: one defensive + one best non-tech
        picks = []
        if defensive_scored:
            picks.append(defensive_scored[0][0])
        if non_tech_scored:
            for e, s, d in non_tech_scored:
                if e not in picks:
                    picks.append(e)
                    break
        if len(picks) < 2 and scored:
            for e, s, d in scored:
                if e not in picks:
                    picks.append(e)
                    break
        recommended = picks[:2]
        reasoning_parts.append(
            f"SPY RSI={spy_rsi:.1f}>75 (OVERBOUGHT). Reducing to 2 ETFs max. "
            f"Avoiding pure-momentum top picks — preferring value/defensive."
        )
        reasoning_parts.append(f"Selected: {recommended}")

    else:
        # Normal bull: top 3 by momentum
        recommended = [e for (e, s, d) in scored[:3]]
        top3_detail = [(e, round(s, 2)) for (e, s, d) in scored[:3]]
        reasoning_parts.append(
            f"BULL regime (VIX={vix:.1f}, SPY_RSI={spy_rsi:.1f}). "
            f"Picking top 3 by momentum score."
        )
        reasoning_parts.append(f"Top 3: {top3_detail}")

    # Score snapshot for the record
    scores_snapshot = {e: round(s, 4) for (e, s, d) in scored}

    # Try LLM to enhance reasoning / potentially override vote
    top5 = [(e, round(s, 2)) for e, s, d in scored[:5]]
    llm_vote, llm_reasoning = _etf_llm_vote(
        "ETF Investment Director",
        f"Regime: bull={bull}, VIX={vix:.1f}, SPY_RSI={spy_rsi:.1f}, "
        f"bear_mode={bear_mode}, overbought={overbought}.\n"
        f"Top ETFs by momentum: {top5}.\n"
        f"Rule-based recommendation: {recommended}.\n"
        "Confirm or improve this ETF rotation decision.",
    )
    final_vote      = llm_vote or "APPROVE"
    final_reasoning = llm_reasoning or " ".join(reasoning_parts)

    log.info("Director recommendation: %s (vote=%s, llm=%s)", recommended, final_vote, bool(llm_vote))

    return {
        "member": "ETF Investment Director",
        "recommended_etfs": recommended,
        "bear_mode": bear_mode,
        "overbought_mode": overbought,
        "momentum_scores": scores_snapshot,
        "reasoning": final_reasoning,
        "risk_flags": [],
        "vote": final_vote,
    }


# ---------------------------------------------------------------------------
# Board Member 2: ETF Risk Officer
# ---------------------------------------------------------------------------

def _etf_risk_officer(brief: dict, proposed_etfs: List[str], pod_state: dict) -> dict:
    """
    ETF Risk Officer — reviews risk but CANNOT veto execution.

    NOTE: If Director + Strategist both APPROVE/MODIFY, execution proceeds
    regardless of Risk Officer's vote. The Risk Officer's role is to ensure
    risks are documented and acknowledged, not to block trades.

    Checks:
    - All proposed ETFs from same sector? (correlation risk)
    - VIX > 25? (elevated volatility risk)
    - Any geo risk flags on proposed ETFs?
    - SPY RSI > 75? (overbought entry risk)
    - Any single ETF would exceed 60% of pod budget? (concentration risk)
    """
    regime      = brief.get("regime", {})
    etfs        = brief.get("etfs", {})
    vix         = regime.get("vix", 20.0)
    spy_rsi     = regime.get("spy_rsi", 50.0)
    pod_total   = _pod_budget(pod_state)

    risk_flags: List[str] = []
    critical_flags = 0

    # 1. Correlation / same-sector concentration
    asset_classes = [ETF_ASSET_CLASS.get(e, "unknown") for e in proposed_etfs]
    unique_classes = set(asset_classes)
    if len(unique_classes) == 1 and len(proposed_etfs) > 1:
        flag = f"CORRELATION RISK: All proposed ETFs ({proposed_etfs}) are in the same asset class ({asset_classes[0]})"
        risk_flags.append(flag)
        critical_flags += 1
        log.warning("Risk flag: %s", flag)

    # 2. VIX > 25
    if vix > 25:
        flag = f"VOLATILITY RISK: VIX={vix:.1f}>25 — elevated market volatility"
        risk_flags.append(flag)
        critical_flags += 1
        log.warning("Risk flag: %s", flag)

    # 3. Geo risk on any proposed ETF
    geo_flagged = [e for e in proposed_etfs if etfs.get(e, {}).get("geo_risk", False)]
    if geo_flagged:
        flag = f"GEO RISK: Geopolitical flags on proposed ETFs: {geo_flagged}"
        risk_flags.append(flag)
        log.warning("Risk flag: %s", flag)

    # 4. SPY RSI > 75 (overbought entry risk)
    if spy_rsi > 75:
        flag = f"OVERBOUGHT ENTRY RISK: SPY RSI={spy_rsi:.1f}>75 — entering at elevated valuations"
        risk_flags.append(flag)
        log.warning("Risk flag: %s", flag)

    # 5. Concentration: single ETF > 60% of total pod value
    if proposed_etfs and pod_total > 0:
        per_etf_alloc = pod_total / len(proposed_etfs)
        pct_of_pod = (per_etf_alloc / pod_total) * 100.0
        if pct_of_pod > 60.0:
            flag = (
                f"CONCENTRATION RISK: Equal-weight allocation gives "
                f"{pct_of_pod:.0f}% per ETF (>{60}% threshold for single-name)"
            )
            # Only critical if we have just 1 ETF
            if len(proposed_etfs) == 1:
                risk_flags.append(flag)
                critical_flags += 1
                log.warning("Risk flag: %s", flag)

    # Determine Risk Officer vote
    n_flags = len(risk_flags)
    if n_flags <= 1:
        vote = "APPROVE"
    elif n_flags == 2:
        vote = "MODIFY"
    else:
        # >= 3 flags OR >= 2 critical flags
        if critical_flags >= 2:
            vote = "REJECT"
        else:
            vote = "MODIFY"

    # If REJECT, suggest safer fallback
    fallback_suggestion: Optional[List[str]] = None
    if vote == "REJECT":
        fallback_suggestion = ["XLU", "GLD", "TLT"]
        log.info(
            "Risk Officer voting REJECT — suggesting safer fallback: %s. "
            "NOTE: This is advisory only. If Director + Strategist approve, execution proceeds.",
            fallback_suggestion,
        )

    # Try LLM to enhance risk assessment
    flags_summary = "; ".join(risk_flags) if risk_flags else "No flags"
    llm_vote, llm_reasoning = _etf_llm_vote(
        "ETF Risk Officer",
        f"Proposed ETFs: {proposed_etfs}.\n"
        f"Risk flags ({n_flags} total, {critical_flags} critical): {flags_summary}.\n"
        f"VIX={vix:.1f}, SPY_RSI={spy_rsi:.1f}, pod_total=${pod_total:,.0f}.\n"
        f"Rule-based vote: {vote}.\n"
        "Note: your vote is advisory — Director + Strategist override if both APPROVE/MODIFY. "
        "Assess whether these risks warrant changing the rotation decision.",
    )
    final_vote      = llm_vote or vote
    final_reasoning = llm_reasoning

    log.info(
        "Risk Officer: %d flag(s), %d critical — vote=%s (llm=%s)",
        n_flags, critical_flags, final_vote, bool(llm_vote),
    )

    result = {
        "member": "ETF Risk Officer",
        "recommended_etfs": fallback_suggestion or proposed_etfs,
        "risk_flags": risk_flags,
        "critical_flags": critical_flags,
        "fallback_suggestion": fallback_suggestion,
        "vote": final_vote,
        "vote_note": (
            "Risk Officer vote is ADVISORY ONLY. "
            "If ETF Investment Director AND ETF Strategist both vote APPROVE or MODIFY, "
            "execution proceeds regardless. Risk flags are logged for the record."
        ),
    }
    if final_reasoning:
        result["reasoning"] = final_reasoning
    return result


# ---------------------------------------------------------------------------
# Board Member 3: ETF Strategist
# ---------------------------------------------------------------------------

def _etf_strategist(brief: dict, proposed_etfs: List[str], risk_flags: List[str]) -> dict:
    """
    ETF Strategist — ensures rotation logic is coherent and diversified.

    Checks:
    - ETFs span at least 2 different asset classes / sectors
    - Rotation aligns with R&D sector breadth (are we buying what R&D likes?)
    - Historical context: does current analogue suggest this rotation worked?
    - Recommends position sizing
    """
    etfs    = brief.get("etfs", {})
    regime  = brief.get("regime", {})

    reasoning_parts: List[str] = []
    strategist_flags: List[str] = []

    # 1. Diversification check
    asset_classes = [ETF_ASSET_CLASS.get(e, "unknown") for e in proposed_etfs]
    unique_classes = set(asset_classes)
    if len(unique_classes) < 2 and len(proposed_etfs) > 1:
        strategist_flags.append(
            f"DIVERSIFICATION CONCERN: All picks in same asset class ({list(unique_classes)[0]})"
        )
        reasoning_parts.append("Rotation lacks cross-sector diversification.")
    else:
        reasoning_parts.append(
            f"Diversification OK: {len(unique_classes)} distinct asset classes across {len(proposed_etfs)} ETFs."
        )

    # 2. R&D alignment check — are we buying what R&D covered and liked?
    rd_covered_positive = [
        e for e in proposed_etfs
        if etfs.get(e, {}).get("rd_covered") and (etfs.get(e, {}).get("rd_score", 0) or 0) >= 7
    ]
    rd_covered_any = [e for e in proposed_etfs if etfs.get(e, {}).get("rd_covered")]

    if rd_covered_positive:
        reasoning_parts.append(
            f"R&D alignment: {rd_covered_positive} have strong R&D sector scores (>=7/10)."
        )
    elif rd_covered_any:
        reasoning_parts.append(
            f"Partial R&D alignment: {rd_covered_any} covered by R&D (scores may be mixed)."
        )
    else:
        reasoning_parts.append(
            "No R&D sector coverage for proposed ETFs — rotation is momentum-only, no R&D confirmation."
        )
        strategist_flags.append("No R&D sector confirmation for any proposed ETF.")

    # 3. Historical analogue check
    hist_label    = regime.get("historical_analogue", "")
    sectors_led   = regime.get("sectors_led_in_analogue", [])
    etf_sectors   = [etfs.get(e, {}).get("sector", "") for e in proposed_etfs]

    analogue_match = any(
        any(led.lower() in sector.lower() for led in sectors_led)
        for sector in etf_sectors
        if sector
    )

    if hist_label:
        if analogue_match:
            reasoning_parts.append(
                f"Historical analogue '{hist_label}' confirms rotation: "
                f"sectors {sectors_led} led in that regime — overlap with our picks."
            )
        else:
            reasoning_parts.append(
                f"Historical analogue '{hist_label}' (leaders: {sectors_led}) has limited "
                f"overlap with proposed ETF sectors {etf_sectors}."
            )
            strategist_flags.append(
                f"Rotation diverges from historical analogue leaders ({sectors_led})."
            )

    # 4. Position sizing recommendation
    n = len(proposed_etfs)
    if n == 0:
        sizing_note = "No ETFs proposed — no sizing recommendation."
    elif n == 1:
        sizing_note = "Single ETF: 100% of allocated capital."
    else:
        # Check if one ETF has significantly better momentum (>3pt gap)
        scores = {e: _momentum_score(etfs.get(e, {})) for e in proposed_etfs}
        sorted_scores = sorted(scores.values(), reverse=True)
        if len(sorted_scores) >= 2 and (sorted_scores[0] - sorted_scores[1]) > 3.0:
            top_etf = max(scores, key=lambda x: scores[x])
            sizing_note = (
                f"Momentum gap >3pts: overweight {top_etf} at ~40%, "
                f"equal-weight remaining at ~{60 // (n - 1)}% each."
            )
        else:
            sizing_note = f"Equal weight: ~{100 // n}% per ETF ({n} ETFs)."

    reasoning_parts.append(f"Sizing: {sizing_note}")

    # Determine Strategist vote (rule-based)
    n_flags = len(strategist_flags)
    if n_flags == 0:
        rule_vote = "APPROVE"
    elif n_flags == 1:
        rule_vote = "MODIFY"
    else:
        rule_vote = "REJECT"

    # Try LLM to enhance strategic reasoning
    flags_summary = "; ".join(strategist_flags) if strategist_flags else "None"
    llm_vote, llm_reasoning = _etf_llm_vote(
        "ETF Strategist",
        f"Proposed ETFs: {proposed_etfs}.\n"
        f"Asset class diversity: {len(unique_classes)} class(es) — {list(unique_classes)}.\n"
        f"Historical analogue: '{hist_label}', sectors led: {sectors_led}. Analogue match: {analogue_match}.\n"
        f"R&D aligned: {rd_covered_positive}. Sizing: {sizing_note}.\n"
        f"Strategy flags: {flags_summary}.\n"
        f"Rule-based vote: {rule_vote}.\n"
        "Assess whether this rotation is strategically coherent and well-diversified.",
    )
    final_vote      = llm_vote or rule_vote
    final_reasoning = llm_reasoning or " ".join(reasoning_parts)

    log.info("Strategist: %d flag(s) — vote=%s (llm=%s) | sizing=%s",
             n_flags, final_vote, bool(llm_vote), sizing_note)

    return {
        "member": "ETF Strategist",
        "recommended_etfs": proposed_etfs,
        "risk_flags": strategist_flags,
        "sizing_note": sizing_note,
        "reasoning": final_reasoning,
        "analogue_match": analogue_match,
        "vote": final_vote,
    }


# ---------------------------------------------------------------------------
# Voting and reconciliation
# ---------------------------------------------------------------------------

def _tally_votes(
    director_pos: dict,
    risk_officer_pos: dict,
    strategist_pos: dict,
) -> Tuple[str, str, List[str]]:
    """
    Tally votes and return (final_vote, final_etf_list, reconciliation_notes).

    Rules:
    - Majority wins (2-of-3)
    - If Director + Strategist agree: Risk Officer is overruled (risk flags logged)
    - Final ETF list is Director's recommendation unless Strategist REJECTS
      (Strategist's concerns are used to modify, not block)
    """
    votes = {
        "ETF Investment Director": director_pos["vote"],
        "ETF Risk Officer": risk_officer_pos["vote"],
        "ETF Strategist": strategist_pos["vote"],
    }

    approved_count = sum(1 for v in votes.values() if v == "APPROVE")
    modified_count = sum(1 for v in votes.values() if v == "MODIFY")
    rejected_count = sum(1 for v in votes.values() if v == "REJECT")

    notes: List[str] = []
    dir_vote  = director_pos["vote"]
    strat_vote = strategist_pos["vote"]
    ro_vote   = risk_officer_pos["vote"]

    # Director and Strategist alignment overrules Risk Officer
    if dir_vote in ("APPROVE", "MODIFY") and strat_vote in ("APPROVE", "MODIFY"):
        if ro_vote == "REJECT":
            notes.append(
                "Risk Officer voted REJECT but is OVERRULED by Director + Strategist majority. "
                "Risk Officer flags are logged and acknowledged but execution proceeds. "
                "This is per governance policy: Risk Officer cannot veto execution."
            )
        final_vote = "APPROVED"
        # Use Director's ETF list as the base
        final_etfs = director_pos["recommended_etfs"]
        notes.append(f"Director + Strategist majority: ETF list = {final_etfs}")

    elif dir_vote == "REJECT" or (dir_vote == "REJECT" and strat_vote == "REJECT"):
        final_vote = "REJECTED"
        # Fall back to Risk Officer's safer suggestion if available
        final_etfs = risk_officer_pos.get("fallback_suggestion") or ["XLU", "GLD", "TLT"]
        notes.append(f"Director REJECT — falling back to defensive rotation: {final_etfs}")

    else:
        # Mixed bag — use MODIFIED
        final_vote = "MODIFIED"
        # Use Director's list but note it as modified
        final_etfs = director_pos["recommended_etfs"]
        notes.append(
            f"Mixed votes (APPROVE={approved_count} MODIFY={modified_count} REJECT={rejected_count}). "
            f"Proceeding with Director's recommendation as MODIFIED: {final_etfs}"
        )

    log.info(
        "VOTE TALLY — Director:%s Risk:%s Strategist:%s => %s | ETFs: %s",
        dir_vote, ro_vote, strat_vote, final_vote, final_etfs,
    )
    return final_vote, final_etfs, notes


# ---------------------------------------------------------------------------
# Emergency re-deliberation (Change 1)
# ---------------------------------------------------------------------------

def _director_revote_on_risk_concerns(
    brief: dict,
    original_pos: dict,
    risk_concerns: List[str],
) -> dict:
    """
    Re-evaluation by the ETF Investment Director after Risk Officer raised concerns.
    The Director explicitly reviews each concern and re-casts their vote.

    If >= 2 critical concerns are present, the Director downgrades to MODIFY.
    If any concern mentions CORRELATION or VOLATILITY, the Director may shift
    their recommended list toward defensives.
    """
    regime = brief.get("regime", {})
    etfs   = brief.get("etfs", {})

    original_vote  = original_pos["vote"]
    original_etfs  = original_pos["recommended_etfs"]
    bear_mode      = original_pos.get("bear_mode", False)
    vix            = regime.get("vix", 20.0)

    concern_text = " ".join(risk_concerns).upper()
    has_correlation = "CORRELATION" in concern_text
    has_volatility  = "VOLATILITY" in concern_text
    has_concentration = "CONCENTRATION" in concern_text
    n_critical_concerns = sum(1 for c in risk_concerns if any(
        kw in c.upper() for kw in ("CORRELATION RISK", "VOLATILITY RISK", "CONCENTRATION RISK")
    ))

    revised_etfs = list(original_etfs)
    reasoning_parts = [
        f"[ROUND 2 RE-EVALUATION] Director reviewing {len(risk_concerns)} Risk Officer concern(s):"
    ]
    for i, concern in enumerate(risk_concerns, 1):
        reasoning_parts.append(f"  Concern {i}: {concern}")

    # Adjust recommendation based on concern type
    if has_correlation and not bear_mode:
        # Diversify — replace second pick with a different asset class
        reasoning_parts.append(
            "Director acknowledges CORRELATION risk. Reviewing rotation for cross-asset diversification."
        )
        defensive_options = [e for e in ["GLD", "TLT", "XLU", "XLP"] if e not in revised_etfs]
        if defensive_options and len(revised_etfs) > 1:
            replaced = revised_etfs[-1]
            revised_etfs[-1] = defensive_options[0]
            reasoning_parts.append(
                f"Director adjusts: replacing {replaced} with {defensive_options[0]} "
                f"to improve diversification across asset classes."
            )

    if has_volatility and vix > 25:
        reasoning_parts.append(
            f"Director acknowledges VOLATILITY risk (VIX={vix:.1f}). "
            "Confirming defensive bias is appropriate in this environment."
        )
        # Ensure at least one defensive ETF is present
        has_defensive = any(e in DEFENSIVE_ETFS for e in revised_etfs)
        if not has_defensive:
            defensive_options = [e for e in DEFENSIVE_ETFS if e not in revised_etfs]
            if defensive_options and len(revised_etfs) > 1:
                replaced = revised_etfs[-1]
                revised_etfs[-1] = defensive_options[0]
                reasoning_parts.append(
                    f"Director adjusts: replacing {replaced} with {defensive_options[0]} "
                    f"for volatility-regime defensiveness."
                )

    # Re-cast vote
    if n_critical_concerns >= 2:
        revised_vote = "MODIFY"
        reasoning_parts.append(
            f"Director downgrades vote to MODIFY (was {original_vote}) — "
            f"{n_critical_concerns} critical concerns acknowledged."
        )
    else:
        revised_vote = original_vote
        reasoning_parts.append(
            f"Director maintains vote: {original_vote} — concerns noted but not critical enough to change position."
        )

    log.info(
        "Director revote: %s -> %s | revised ETFs: %s",
        original_vote, revised_vote, revised_etfs,
    )

    return {
        "member": "ETF Investment Director",
        "round": 2,
        "recommended_etfs": revised_etfs,
        "original_vote": original_vote,
        "vote": revised_vote,
        "risk_concerns_reviewed": risk_concerns,
        "reasoning": " ".join(reasoning_parts),
        "risk_flags": original_pos.get("risk_flags", []),
        "bear_mode": bear_mode,
        "overbought_mode": original_pos.get("overbought_mode", False),
        "momentum_scores": original_pos.get("momentum_scores", {}),
    }


def _strategist_revote_on_risk_concerns(
    brief: dict,
    original_pos: dict,
    risk_concerns: List[str],
    revised_director_etfs: List[str],
) -> dict:
    """
    Re-evaluation by the ETF Strategist after Risk Officer raised concerns.
    The Strategist explicitly reviews each concern and re-casts their vote
    against the (potentially revised) Director ETF list.
    """
    concern_text = " ".join(risk_concerns).upper()
    has_geo      = "GEO RISK" in concern_text
    n_critical_concerns = sum(1 for c in risk_concerns if any(
        kw in c.upper() for kw in ("CORRELATION RISK", "VOLATILITY RISK", "CONCENTRATION RISK")
    ))

    original_vote = original_pos["vote"]

    reasoning_parts = [
        f"[ROUND 2 RE-EVALUATION] Strategist reviewing {len(risk_concerns)} Risk Officer concern(s):"
    ]
    for i, concern in enumerate(risk_concerns, 1):
        reasoning_parts.append(f"  Concern {i}: {concern}")

    # Check if revised ETF list (from Director round 2) is better diversified
    from_ac = [ETF_ASSET_CLASS.get(e, "unknown") for e in revised_director_etfs]
    unique_ac = set(from_ac)
    if len(unique_ac) >= 2:
        reasoning_parts.append(
            f"Strategist confirms revised ETF list {revised_director_etfs} has improved "
            f"diversification: {len(unique_ac)} distinct asset classes."
        )
        diversification_ok = True
    else:
        reasoning_parts.append(
            f"Strategist notes revised ETF list still concentrated in {list(unique_ac)}."
        )
        diversification_ok = False

    if has_geo:
        reasoning_parts.append(
            "Strategist acknowledges GEO RISK flag. "
            "Rotation proceeds with geo-flag risk noted in minutes."
        )

    # Re-cast vote
    if not diversification_ok and n_critical_concerns >= 2:
        revised_vote = "MODIFY"
        reasoning_parts.append(
            f"Strategist downgrades vote to MODIFY (was {original_vote}) — "
            "diversification still suboptimal after Director revision."
        )
    elif n_critical_concerns >= 3:
        revised_vote = "MODIFY"
        reasoning_parts.append(
            f"Strategist downgrades vote to MODIFY (was {original_vote}) — "
            f"{n_critical_concerns} critical concerns require caution."
        )
    else:
        revised_vote = original_vote
        reasoning_parts.append(
            f"Strategist maintains vote: {original_vote} — concerns reviewed and addressed."
        )

    log.info(
        "Strategist revote: %s -> %s | ETFs confirmed: %s",
        original_vote, revised_vote, revised_director_etfs,
    )

    return {
        "member": "ETF Strategist",
        "round": 2,
        "recommended_etfs": revised_director_etfs,
        "original_vote": original_vote,
        "vote": revised_vote,
        "risk_concerns_reviewed": risk_concerns,
        "reasoning": " ".join(reasoning_parts),
        "risk_flags": original_pos.get("risk_flags", []),
        "sizing_note": original_pos.get("sizing_note", ""),
        "analogue_match": original_pos.get("analogue_match", False),
    }


# ---------------------------------------------------------------------------
# Trade generation
# ---------------------------------------------------------------------------

def _generate_orders(
    final_etfs: List[str],
    pod_state: dict,
    brief: dict,
) -> List[dict]:
    """
    Compare recommended ETFs to current pod3 positions.
    Generate BUY/SELL orders with quantities and dollar values.

    Position size: min(budget_remaining / n_etfs, budget / n_etfs)
    Quantity: math.floor(position_size / current_price)  — always floor, never exceed budget
    """
    etfs_data       = brief.get("etfs", {})
    current_pos     = pod_state.get("positions", {})
    cash_balance    = _pod_unspent(pod_state)
    total_value     = _pod_budget(pod_state)
    n_etfs          = len(final_etfs) if final_etfs else 1

    current_tickers = set(current_pos.keys())
    target_tickers  = set(final_etfs)

    to_sell = current_tickers - target_tickers
    to_buy  = target_tickers - current_tickers

    orders: List[dict] = []

    # SELL orders first
    for ticker in sorted(to_sell):
        pos = current_pos.get(ticker, {})
        qty = pos.get("qty", 0)
        price = pos.get("current_price", pos.get("avg_entry", 0.0))
        if qty <= 0:
            continue
        dollar_value = round(qty * price, 2)
        orders.append({
            "action": "SELL",
            "ticker": ticker,
            "qty": qty,
            "price": round(price, 2),
            "dollar_value": dollar_value,
            "reason": f"{ticker} removed from rotation — not in board's approved ETF list.",
        })
        log.info("ORDER SELL %s qty=%.0f @ $%.2f ($%.0f)", ticker, qty, price, dollar_value)

    # BUY orders
    # After sells, estimate available cash (sells add proceeds back)
    sell_proceeds = sum(o["dollar_value"] for o in orders if o["action"] == "SELL")
    available_cash = cash_balance + sell_proceeds

    position_size = min(available_cash / n_etfs, total_value / n_etfs) if n_etfs > 0 else 0.0

    for ticker in sorted(to_buy):
        entry = etfs_data.get(ticker, {})
        price = entry.get("current_price", 0.0)
        if price <= 0:
            log.warning("BUY skipped for %s — no valid price (%.2f)", ticker, price)
            orders.append({
                "action": "BUY",
                "ticker": ticker,
                "qty": 0,
                "price": 0.0,
                "dollar_value": 0.0,
                "reason": f"{ticker} in target list but price unavailable — skipped.",
                "skipped": True,
            })
            continue

        qty = math.floor(position_size / price)
        if qty <= 0:
            log.warning(
                "BUY qty=0 for %s — position_size $%.0f < price $%.2f",
                ticker, position_size, price
            )
            orders.append({
                "action": "BUY",
                "ticker": ticker,
                "qty": 0,
                "price": round(price, 2),
                "dollar_value": 0.0,
                "reason": f"{ticker} in target list but insufficient cash for even 1 share.",
                "skipped": True,
            })
            continue

        dollar_value = round(qty * price, 2)
        momentum = _momentum_score(entry)
        orders.append({
            "action": "BUY",
            "ticker": ticker,
            "qty": qty,
            "price": round(price, 2),
            "dollar_value": dollar_value,
            "reason": (
                f"{ticker} added to rotation. momentum_score={momentum:.2f}, "
                f"ret_1m={entry.get('price_return_1m', 0):.1f}%, "
                f"RSI={entry.get('rsi_14', 0):.1f}, "
                f"trend={entry.get('trend', 'unknown')}."
            ),
        })
        log.info("ORDER BUY %s qty=%d @ $%.2f ($%.0f)", ticker, qty, price, dollar_value)

    # HOLDs (not generating explicit orders, just for record)
    holds = current_tickers & target_tickers
    for ticker in sorted(holds):
        log.info("HOLD %s — already in pod3 and in target list", ticker)

    return orders


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            json.dump(data, fh, indent=2)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Email summary (graceful skip)
# ---------------------------------------------------------------------------

def _send_board_summary(
    final_vote: str,
    final_etfs: List[str],
    orders: List[dict],
    risk_flags: List[str],
    regime: dict,
) -> None:
    """
    Send a brief board meeting summary email.
    Gracefully skips if email system is unavailable.
    """
    try:
        sys.path.insert(0, str(BASE))
        from email_sender import send_daily_report  # type: ignore

        buys  = [o for o in orders if o["action"] == "BUY"  and not o.get("skipped")]
        sells = [o for o in orders if o["action"] == "SELL"]

        buy_rows  = "".join(
            f"<tr><td>BUY</td><td>{o['ticker']}</td><td>{o['qty']}</td>"
            f"<td>${o['price']:.2f}</td><td>${o['dollar_value']:,.0f}</td></tr>"
            for o in buys
        )
        sell_rows = "".join(
            f"<tr><td>SELL</td><td>{o['ticker']}</td><td>{o['qty']}</td>"
            f"<td>${o['price']:.2f}</td><td>${o['dollar_value']:,.0f}</td></tr>"
            for o in sells
        )

        flags_html = (
            "<ul>" + "".join(f"<li>{f}</li>" for f in risk_flags) + "</ul>"
            if risk_flags
            else "<p>None</p>"
        )

        html = f"""
        <html><body style="background:#0d1117;color:#c9d1d9;font-family:Arial,sans-serif;padding:24px;">
        <h2 style="color:#58a6ff;">ETF Pod 3 Board Meeting — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>
        <p>Board vote: <strong style="color:{'#3fb950' if final_vote=='APPROVED' else '#f85149'}">{final_vote}</strong></p>
        <p>Target ETFs: <strong>{', '.join(final_etfs)}</strong></p>
        <p>Regime: {regime.get('regime_label','?')} | VIX={regime.get('vix',0):.1f} | SPY_RSI={regime.get('spy_rsi',0):.1f}</p>
        <h3 style="color:#58a6ff;">Trade Orders</h3>
        <table border="1" style="border-collapse:collapse;color:#c9d1d9;">
          <tr><th>Action</th><th>Ticker</th><th>Qty</th><th>Price</th><th>Value</th></tr>
          {buy_rows}{sell_rows}
        </table>
        <h3 style="color:#f0883e;">Risk Flags</h3>
        {flags_html}
        <p style="color:#6e7681;font-size:11px;">Risk Officer flags acknowledged. Execution proceeds per board governance.</p>
        </body></html>
        """

        success = send_daily_report(
            html_body=html,
            subject=f"ETF Pod 3 Board — {final_vote} | Target: {', '.join(final_etfs)}",
            sender_name="ETF Pod Board",
        )
        if success:
            log.info("Board summary email sent/buffered.")
        else:
            log.warning("Email send returned False — check email_sender config.")

    except Exception as exc:
        log.info("Email send skipped (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    log.info("ETF Pod Board convening — %s", now)

    # -----------------------------------------------------------------------
    # 1. Load inputs
    # -----------------------------------------------------------------------
    try:
        brief = _load_brief()
    except FileNotFoundError:
        log.error(
            "etf_research_brief.json not found at %s — run etf_research_agent.py first.", BRIEF_PATH
        )
        sys.exit(1)
    except json.JSONDecodeError as exc:
        log.error("etf_research_brief.json is malformed: %s", exc)
        sys.exit(1)

    pod_state = _load_pod_state()

    # Load macro agent scores and inject into each ETF entry in the brief
    macro_scores = _load_macro_scores()
    if macro_scores and "etfs" in brief:
        for ticker, entry in brief["etfs"].items():
            if ticker in macro_scores:
                entry["macro_momentum_score"] = macro_scores[ticker]
        log.info("Macro scores injected into brief for %d ETF(s)", len(macro_scores))

    # Load canary signals and inject into the regime section (Change 3)
    canary_signals = _load_canary_signals()
    active_canary_alerts: List[str] = []
    if canary_signals:
        alerts = canary_signals.get("alerts", canary_signals.get("active_alerts", []))
        if isinstance(alerts, list):
            active_canary_alerts = [str(a) for a in alerts]
        elif isinstance(alerts, dict):
            active_canary_alerts = [f"{k}: {v}" for k, v in alerts.items()]
        if active_canary_alerts:
            log.warning(
                "CANARY ALERTS ACTIVE (%d): %s",
                len(active_canary_alerts),
                active_canary_alerts,
            )
        # Inject canary_warning into the regime section so board members see it
        brief.setdefault("regime", {})["canary_warning"] = (
            active_canary_alerts if active_canary_alerts else None
        )

    regime = brief.get("regime", {})

    log.info(
        "Regime: %s | VIX=%.1f | SPY_RSI=%.1f | warnings=%d",
        regime.get("regime_label", "unknown"),
        regime.get("vix", 0),
        regime.get("spy_rsi", 0),
        regime.get("warning_count", 0),
    )
    log.info(
        "Pod3 state: unspent budget=$%.0f | deployed=$%.0f | budget=$%.0f | n_positions=%d",
        _pod_unspent(pod_state),
        pod_state.get("deployed_value", pod_state.get("positions_value", 0)),
        _pod_budget(pod_state),
        len(pod_state.get("positions", {})),
    )

    # -----------------------------------------------------------------------
    # 1b. Market emergency check (Change 2) — overrides normal deliberation
    # -----------------------------------------------------------------------
    is_emergency, emergency_reason = _check_market_emergency(brief)
    if is_emergency:
        log.warning("=" * 60)
        log.warning("MARKET EMERGENCY DETECTED: %s", emergency_reason)
        log.warning("BYPASSING NORMAL DELIBERATION — GENERATING EMERGENCY EXIT ORDERS")
        log.warning("=" * 60)

        emergency_orders = _generate_emergency_exit_orders(pod_state)

        emergency_orders_doc = {
            "generated_at": now,
            "pod": POD_ID,
            "board_vote": "EMERGENCY_EXIT",
            "target_holdings": [],
            "orders": emergency_orders,
            "risk_flags": [f"EMERGENCY EXIT: {emergency_reason}"],
            "risk_officer_acknowledged": True,
            "risk_officer_vote": "N/A",
            "risk_officer_note": "Emergency exit bypassed normal deliberation.",
            "regime_context": {
                "label": regime.get("regime_label"),
                "vix": regime.get("vix"),
                "spy_rsi": regime.get("spy_rsi"),
                "spy_1d_return": regime.get("spy_1d_return"),
                "overbought": regime.get("overbought"),
                "warning_count": regime.get("warning_count"),
                "emergency_reason": emergency_reason,
            },
        }
        _write_json(ORDERS_PATH, emergency_orders_doc)
        log.warning("EMERGENCY EXIT orders written to %s", ORDERS_PATH)

        emergency_minutes = {
            "generated_at": now,
            "pod": POD_ID,
            "meeting_type": "ETF Pod 3 Board Meeting — EMERGENCY SESSION",
            "final_vote": "EMERGENCY_EXIT",
            "target_etfs": [],
            "regime": regime,
            "pod_state_at_meeting": {
                "budget_remaining": _pod_unspent(pod_state),
                "budget":       _pod_budget(pod_state),
                "n_positions":  len(pod_state.get("positions", {})),
            },
            "emergency": {
                "triggered": True,
                "reason": emergency_reason,
                "note": (
                    "Normal board deliberation was bypassed. "
                    "All pod3 positions are being liquidated via EMERGENCY_EXIT orders."
                ),
            },
            "canary_alerts": active_canary_alerts if active_canary_alerts else None,
            "deliberations": None,
            "reconciliation_notes": [f"EMERGENCY EXIT triggered: {emergency_reason}"],
            "risk_flags": [f"EMERGENCY EXIT: {emergency_reason}"],
            "orders_summary": {
                "n_buys": 0,
                "n_sells": len(emergency_orders),
                "n_skipped": 0,
                "total_buy_value": 0.0,
                "total_sell_value": round(sum(o["dollar_value"] for o in emergency_orders), 2),
            },
        }
        _write_json(MINUTES_PATH, emergency_minutes)
        log.warning("EMERGENCY SESSION minutes written to %s", MINUTES_PATH)

        return {
            "final_vote": "EMERGENCY_EXIT",
            "final_etfs": [],
            "orders": emergency_orders,
            "risk_flags": [f"EMERGENCY EXIT: {emergency_reason}"],
            "minutes": emergency_minutes,
        }

    # -----------------------------------------------------------------------
    # 2. Board member deliberations
    # -----------------------------------------------------------------------
    log.info("--- ETF Investment Director deliberating ---")
    director_pos = _etf_investment_director(brief)

    proposed_etfs = director_pos["recommended_etfs"]

    log.info("--- ETF Risk Officer reviewing --- (advisory only — cannot veto)")
    risk_officer_pos = _etf_risk_officer(brief, proposed_etfs, pod_state)

    all_risk_flags = risk_officer_pos["risk_flags"]

    log.info("--- ETF Strategist deliberating ---")
    strategist_pos = _etf_strategist(brief, proposed_etfs, all_risk_flags)

    # -----------------------------------------------------------------------
    # 3. Initial vote (Round 1)
    # -----------------------------------------------------------------------
    round1_vote, round1_etfs, round1_notes = _tally_votes(
        director_pos, risk_officer_pos, strategist_pos
    )

    # -----------------------------------------------------------------------
    # 3b. Emergency re-deliberation if Risk Officer flagged concerns (Change 1)
    # -----------------------------------------------------------------------
    ro_vote          = risk_officer_pos["vote"]
    ro_critical_flags = risk_officer_pos.get("critical_flags", 0)
    ro_risk_flags     = risk_officer_pos.get("risk_flags", [])

    emergency_revote_triggered = (ro_vote == "REJECT") or (ro_critical_flags >= 1)
    round2_director_pos   = None
    round2_strategist_pos = None

    if emergency_revote_triggered:
        log.warning(
            "EMERGENCY RE-VOTE TRIGGERED — Risk Officer voted %s with %d critical flag(s).",
            ro_vote, ro_critical_flags,
        )
        log.warning("Risk Officer concerns: %s", ro_risk_flags)
        log.info("--- Director re-deliberating (Round 2) ---")
        round2_director_pos = _director_revote_on_risk_concerns(
            brief, director_pos, ro_risk_flags
        )
        log.info("--- Strategist re-deliberating (Round 2) ---")
        round2_strategist_pos = _strategist_revote_on_risk_concerns(
            brief, strategist_pos, ro_risk_flags, round2_director_pos["recommended_etfs"]
        )
        # Final vote uses Round 2 positions for Director + Strategist
        # Risk Officer does not revote — their concerns are already on record
        final_vote, final_etfs, reconciliation_notes = _tally_votes(
            round2_director_pos, risk_officer_pos, round2_strategist_pos
        )
        reconciliation_notes.insert(
            0,
            "EMERGENCY RE-VOTE: Round 2 deliberation triggered by Risk Officer concerns. "
            f"Risk Officer vote ({ro_vote}) with {ro_critical_flags} critical flag(s) "
            "caused Director and Strategist to re-examine each concern before final vote."
        )
        log.info(
            "Round 2 result — Director:%s Strategist:%s => Final: %s | ETFs: %s",
            round2_director_pos["vote"], round2_strategist_pos["vote"],
            final_vote, final_etfs,
        )
    else:
        final_vote        = round1_vote
        final_etfs        = round1_etfs
        reconciliation_notes = round1_notes

    # Combine all risk flags across board
    combined_flags = list(all_risk_flags) + list(strategist_pos.get("risk_flags", []))

    # -----------------------------------------------------------------------
    # 4. Generate trade orders
    # -----------------------------------------------------------------------
    orders = _generate_orders(final_etfs, pod_state, brief)

    # -----------------------------------------------------------------------
    # 5. Write etf_pod_orders.json
    # -----------------------------------------------------------------------
    # Manager Agent reads etf_pod_orders.json and executes all orders. No veto.
    # Budget enforced by pod_ledger.can_trade() before each order.
    orders_doc = {
        "generated_at": now,
        "pod": POD_ID,
        "board_vote": final_vote,
        "target_holdings": final_etfs,
        "orders": orders,
        "risk_flags": combined_flags,
        "risk_officer_acknowledged": True,
        "risk_officer_vote": risk_officer_pos["vote"],
        "risk_officer_note": (
            "Risk Officer flags logged and acknowledged. "
            "Execution proceeds per board governance: "
            "Director + Strategist majority overrides Risk Officer vote."
        ),
        "regime_context": {
            "label": regime.get("regime_label"),
            "vix": regime.get("vix"),
            "spy_rsi": regime.get("spy_rsi"),
            "spy_1d_return": regime.get("spy_1d_return"),
            "overbought": regime.get("overbought"),
            "warning_count": regime.get("warning_count"),
        },
        "canary_alerts": active_canary_alerts if active_canary_alerts else None,
        "emergency_revote": emergency_revote_triggered,
    }
    _write_json(ORDERS_PATH, orders_doc)
    log.info("Orders written to %s", ORDERS_PATH)

    # -----------------------------------------------------------------------
    # 6. Write board meeting minutes
    # -----------------------------------------------------------------------
    # Build deliberations section — always show Round 1; show Round 2 only if triggered
    deliberations_record: dict = {
        "round_1": {
            "etf_investment_director": director_pos,
            "etf_risk_officer": risk_officer_pos,
            "etf_strategist": strategist_pos,
            "tally": {
                "vote": round1_vote,
                "target_etfs": round1_etfs,
                "notes": round1_notes,
            },
        }
    }
    if emergency_revote_triggered:
        deliberations_record["emergency_revote_triggered"] = True
        deliberations_record["revote_reason"] = (
            f"Risk Officer voted {ro_vote} and raised {ro_critical_flags} critical flag(s). "
            "Director and Strategist required to re-examine each concern before final vote."
        )
        deliberations_record["round_2"] = {
            "note": (
                "Emergency Re-Vote Triggered by Risk Officer. "
                "Director and Strategist re-cast votes after reviewing each specific concern. "
                "Risk Officer does not revote — their concerns are already on record."
            ),
            "risk_officer_concerns_reviewed": ro_risk_flags,
            "etf_investment_director": round2_director_pos,
            "etf_strategist": round2_strategist_pos,
            "tally": {
                "vote": final_vote,
                "target_etfs": final_etfs,
                "notes": reconciliation_notes,
            },
        }

    minutes = {
        "generated_at": now,
        "pod": POD_ID,
        "meeting_type": "ETF Pod 3 Board Meeting",
        "final_vote": final_vote,
        "target_etfs": final_etfs,
        "regime": regime,
        "canary_alerts": active_canary_alerts if active_canary_alerts else None,
        "pod_state_at_meeting": {
            "budget_remaining": _pod_unspent(pod_state),
            "budget":       _pod_budget(pod_state),
            "n_positions":  len(pod_state.get("positions", {})),
        },
        "deliberations": deliberations_record,
        "reconciliation_notes": reconciliation_notes,
        "risk_flags": combined_flags,
        "orders_summary": {
            "n_buys":  sum(1 for o in orders if o["action"] == "BUY"  and not o.get("skipped")),
            "n_sells": sum(1 for o in orders if o["action"] == "SELL"),
            "n_skipped": sum(1 for o in orders if o.get("skipped")),
            "total_buy_value":  round(sum(o["dollar_value"] for o in orders if o["action"] == "BUY"), 2),
            "total_sell_value": round(sum(o["dollar_value"] for o in orders if o["action"] == "SELL"), 2),
        },
    }
    _write_json(MINUTES_PATH, minutes)
    log.info("Board minutes written to %s", MINUTES_PATH)

    # -----------------------------------------------------------------------
    # 7. Email summary (graceful skip)
    # -----------------------------------------------------------------------
    _send_board_summary(final_vote, final_etfs, orders, combined_flags, regime)

    log.info(
        "Board meeting complete. Vote=%s | Target=%s | Orders: %d buy, %d sell",
        final_vote,
        final_etfs,
        minutes["orders_summary"]["n_buys"],
        minutes["orders_summary"]["n_sells"],
    )

    return {
        "final_vote": final_vote,
        "final_etfs": final_etfs,
        "orders": orders,
        "risk_flags": combined_flags,
        "minutes": minutes,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('ETFPodBoard'):
        raise SystemExit(0)
    lock = SingleInstanceLock(LOCK_FILE)
    if not lock.acquire():
        log.error("Another instance of etf_pod_board is already running. Exiting.")
        sys.exit(1)

    try:
        result = run()
        import pprint
        print("\n=== ETF POD 3 BOARD RESULT ===")
        print(f"  Final Vote : {result['final_vote']}")
        print(f"  Target ETFs: {result['final_etfs']}")
        print(f"  Risk Flags : {len(result['risk_flags'])}")
        print("\n=== ORDERS ===")
        for order in result["orders"]:
            skipped = " [SKIPPED]" if order.get("skipped") else ""
            print(
                f"  {order['action']:4s} {order['ticker']:4s} "
                f"qty={order['qty']:3d} @ ${order['price']:7.2f} "
                f"= ${order['dollar_value']:8,.2f}{skipped}"
            )
        if result["risk_flags"]:
            print("\n=== RISK FLAGS (acknowledged) ===")
            for f in result["risk_flags"]:
                print(f"  - {f}")
    finally:
        lock.release()
