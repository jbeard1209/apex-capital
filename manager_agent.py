# =============================================================================
# APEX CAPITAL — MANAGER AGENT
# =============================================================================
# The Manager Agent is the decision-maker and executor.
#
# Workflow:
#   1. Run (or load) the Review Agent's recommendations
#   2. Evaluate each recommendation against strategy rules and risk limits
#   3. Approve, modify, or reject each recommendation with a written rationale
#   4. Execute approved changes via AlpacaTrader
#   5. Produce and email a full management report to the Chairman (Jack Beard)
#
# Approval logic:
#   - SELL (stop-loss, take-profit, max-hold): auto-approved
#   - SELL (momentum reversal): approved if loss > 3% OR hold > 30 days
#   - TRIM: approved if overweight > 130% of limit
#   - ADD: approved only if buying_power > 15% of portfolio AND capacity exists
#   - BUY_NEW: approved only if buying_power > 20% AND < MAX_OPEN_POSITIONS
#   - WATCH/HOLD: no execution, logged for transparency
#
# Usage:
#   python3 manager_agent.py              # run review + manage + email
#   python3 manager_agent.py --dry-run    # analyse only, no trades
#   python3 manager_agent.py --no-review  # skip review, use last saved file
#   python3 manager_agent.py --no-email   # skip email, print to console only
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, HOLD_DAYS_MAX,
    MAX_POSITION_SIZE_PCT, MAX_OPEN_POSITIONS,
    STARTING_CAPITAL,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("manager_agent")

REVIEW_PATH  = "data/review_recommendations.json"
MANAGER_LOG  = "data/manager_decisions.json"


# =============================================================================
# EVALUATION ENGINE
# =============================================================================

def evaluate_recommendation(rec: dict, account: dict,
                             pdo_signal: Optional[str] = None,
                             canary_alert: Optional[dict] = None,
                             cvar_circuit_breaker: bool = False,
                             streak_context: Optional[dict] = None) -> dict:
    """
    Apply manager-level judgment to a single recommendation.
    pdo_signal:           PDO signal — 'DEPLOY_MORE', 'NEAR_OPTIMAL', 'REDUCE_EXPOSURE', 'HOLD_CASH'.
    canary_alert:         If set (level RED/ORANGE), switches to DEFENSIVE MODE — block BUYs, force SELLs.
    cvar_circuit_breaker: If True, all BUY/ADD orders are blocked (CVaR > 5% of portfolio).
    streak_context:       Win/loss streak from state.json:
                            {"win_streak": int, "loss_streak": int, "last_trade_result": str|None}
                          Loss streaks of 3+ tighten BUY thresholds; win streaks of 5+ loosen them.
    Returns the rec dict augmented with:
      - manager_decision: 'APPROVED' | 'REJECTED' | 'MODIFIED'
      - manager_action:   the action that will actually be executed
      - manager_rationale: list of strings explaining the decision
    """
    action          = rec["action"]
    ticker          = rec["ticker"]
    pnl_pct         = rec.get("unrealized_pnl_pct", 0) or 0
    hold_days       = rec.get("hold_days", 0) or 0
    weight_pct      = rec.get("weight_pct", 0) or 0
    portfolio_value = account["portfolio_value"]
    buying_power    = account["buying_power"]
    n_positions     = account["n_positions"]
    source_agent    = rec.get("source_agent", "ReviewAgent")

    # ── Streak context ─────────────────────────────────────────────────────
    _streak = streak_context or {}
    win_streak  = _streak.get("win_streak", 0)  or 0
    loss_streak = _streak.get("loss_streak", 0) or 0

    buying_power_pct = (buying_power / portfolio_value * 100) if portfolio_value > 0 else 0

    # PDO adjusts buy thresholds: DEPLOY_MORE loosens them, HOLD_CASH tightens them
    if pdo_signal == "DEPLOY_MORE":
        buy_bp_threshold  = 10    # relaxed: need only 10% buying power (normally 15%)
        buy_new_threshold = 15    # relaxed: need only 15% (normally 20%)
    elif pdo_signal == "HOLD_CASH":
        buy_bp_threshold  = 25    # tightened: need 25% buying power
        buy_new_threshold = 30    # tightened
    elif pdo_signal == "REDUCE_EXPOSURE":
        buy_bp_threshold  = 30
        buy_new_threshold = 35
    else:
        buy_bp_threshold  = 15   # default
        buy_new_threshold = 20

    # ── Streak adjustment — layer on top of PDO thresholds ─────────────────
    # A persistent loss streak signals the strategy is misaligned with current
    # conditions; tighten buy gates to reduce drawdown velocity.
    # A strong win streak signals the strategy is working well; modestly relax
    # gates to let momentum compound (capped — cannot go below the PDO floor).
    if loss_streak >= 5:
        buy_bp_threshold  = max(buy_bp_threshold,  30)
        buy_new_threshold = max(buy_new_threshold, 35)
        rationale.append(
            f"[STREAK] Loss streak {loss_streak} — buy thresholds tightened to "
            f"{buy_bp_threshold}%/{buy_new_threshold}%."
        ) if action in ("BUY_NEW", "ADD") else None
    elif loss_streak >= 3:
        buy_bp_threshold  = max(buy_bp_threshold,  20)
        buy_new_threshold = max(buy_new_threshold, 25)
        rationale.append(
            f"[STREAK] Loss streak {loss_streak} — buy thresholds moderately tightened."
        ) if action in ("BUY_NEW", "ADD") else None
    elif win_streak >= 5:
        buy_bp_threshold  = max(5,  buy_bp_threshold  - 3)
        buy_new_threshold = max(10, buy_new_threshold - 3)
        rationale.append(
            f"[STREAK] Win streak {win_streak} — buy thresholds modestly relaxed."
        ) if action in ("BUY_NEW", "ADD") else None

    decision    = "APPROVED"
    exec_action = action
    rationale  : list[str] = []

    # ── HARD SINGLE-NAME CEILING — non-waivable (board directive 2026-04-27) ─
    # If a long position exceeds 15% of portfolio it cannot be approved as
    # HOLD, ADD, or BUY_NEW regardless of PDO signal, momentum, or canary.
    # This ceiling exists above all other logic and cannot be overridden by
    # any downstream rule.  The review agent sets the TRIM/SELL flag; this
    # block ensures the Manager never *approves* adding to or maintaining an
    # already-overconcentrated position.
    _HARD_SINGLE_NAME_CEILING_PCT = 15.0
    if weight_pct > _HARD_SINGLE_NAME_CEILING_PCT and action in ("HOLD", "ADD", "BUY_NEW"):
        decision    = "MODIFIED"
        exec_action = "TRIM"
        rationale.append(
            f"[HARD CEILING] Position at {weight_pct:.1f}% of portfolio exceeds "
            f"the {_HARD_SINGLE_NAME_CEILING_PCT:.0f}% single-name limit. "
            f"Action overridden from {action} → TRIM. "
            f"No new capital can be approved into an over-concentrated position."
        )
        action = "TRIM"   # propagate so remaining rules see TRIM, not the original action

    # ── CRO BULLETIN ADVISORY ──────────────────────────────────────────────
    # The Chief Risk Officer openly publishes risk directives for all agents.
    # We read and respect this guidance before making any decision.
    cro_bulletin   = _load_cro_bulletin()
    cro_risk_level = cro_bulletin.get("risk_level", "UNKNOWN")
    cro_directives = cro_bulletin.get("directives", [])
    cro_alerts     = cro_bulletin.get("position_alerts", [])
    cro_guidance   = cro_bulletin.get("capital_guidance", "")

    # CRO tightens buy thresholds on top of PDO — take the stricter of the two
    if cro_risk_level in ("HIGH", "CRITICAL"):
        buy_bp_threshold  = max(buy_bp_threshold,  25)
        buy_new_threshold = max(buy_new_threshold, 30)
    elif cro_risk_level == "ELEVATED":
        buy_bp_threshold  = max(buy_bp_threshold,  20)
        buy_new_threshold = max(buy_new_threshold, 25)

    # Check for a CRO position-specific alert on this ticker
    ticker_alert = next((a for a in cro_alerts if a.get("ticker") == ticker), None)

    # ── CRO / PDO CONFLICT ARBITRATION ────────────────────────────────────
    # When CRO and PDO give contradictory directional guidance, the more
    # conservative signal wins. We annotate the rationale so the Chairman
    # can see the conflict — the Manager Agent itself is not told WHY the
    # CRO is grading it (information wall preserved) but the conflict signal
    # is visible in the public advisory channel.
    cro_pdo_conflict = False
    if cro_risk_level in ("HIGH", "CRITICAL") and pdo_signal == "DEPLOY_MORE":
        cro_pdo_conflict = True
        # Disable the max-conviction PDO override (line ~285) by poisoning pdo_signal
        # in scope only — we re-alias it so the rest of the function treats it as NEAR_OPTIMAL
        pdo_signal = "NEAR_OPTIMAL"   # CRO wins: block aggressive deployment

    # ── CANARY DEFENSIVE MODE ──────────────────────────────────────────────
    canary_level = (canary_alert or {}).get("level", "GREEN")
    if canary_level in ("RED", "ORANGE"):
        canary_tag = f"[CANARY {canary_level}]"
        triggers   = (canary_alert or {}).get("triggers", [])
        trigger_str = "; ".join(triggers) if triggers else "market stress detected"

        if action in ("BUY_NEW", "ADD"):
            decision    = "REJECTED"
            exec_action = "HOLD"
            rationale.append(
                f"{canary_tag} DEFENSIVE MODE: All new capital deployment blocked. "
                f"Canary detected: {trigger_str}. "
                f"Preserving cash until market conditions stabilise."
            )
            # Skip remaining logic — decision is final
            return {
                **rec,
                "manager_decision":  decision,
                "manager_action":    exec_action,
                "manager_rationale": rationale,
            }

        elif action == "SELL":
            rationale.append(
                f"{canary_tag} DEFENSIVE MODE: SELL fast-tracked. "
                f"Canary triggered: {trigger_str}. Exiting {ticker} immediately."
            )
            return {
                **rec,
                "manager_decision":  "APPROVED",
                "manager_action":    "SELL",
                "manager_rationale": rationale,
            }

        elif action == "TRIM":
            rationale.append(
                f"{canary_tag} DEFENSIVE MODE: TRIM elevated to SELL. "
                f"Canary triggered: {trigger_str}. "
                f"Full exit preferred over partial trim during market stress."
            )
            return {
                **rec,
                "manager_decision":  "APPROVED",
                "manager_action":    "SELL",
                "manager_rationale": rationale,
            }

    # ── CVaR CIRCUIT BREAKER ───────────────────────────────────────────────
    # Board-mandated (Special Session): if 1-day CVaR > 5% of portfolio,
    # all BUY and ADD orders are blocked for this session.
    if cvar_circuit_breaker and action in ("BUY_NEW", "ADD"):
        return {
            **rec,
            "manager_decision":  "REJECTED",
            "manager_action":    "HOLD",
            "manager_rationale": [
                f"[CVaR CIRCUIT BREAKER] All BUY/ADD orders blocked this session. "
                f"Portfolio 1-day CVaR exceeds 5% threshold. "
                f"Risk must normalise before new capital is deployed. "
                f"{ticker} held — revisit next cycle."
            ],
        }

    # ── SELL decisions ─────────────────────────────────────────────────────
    if action == "SELL":
        # CRO position alert prepended if present
        if ticker_alert:
            rationale.append(
                f"[CRO ALERT — {ticker_alert.get('severity','HIGH')}] "
                f"{ticker_alert['alert']}"
            )
        reasons = " ".join(rec.get("reasons", []))

        if "Stop-loss" in reasons or "stop_loss" in reasons.lower():
            rationale.append(
                f"AUTO-APPROVED: Stop-loss rule is non-negotiable. "
                f"Exiting {ticker} at {pnl_pct:.1f}% to protect capital."
            )

        elif "Take-profit" in reasons:
            rationale.append(
                f"AUTO-APPROVED: Take-profit target met (+{pnl_pct:.1f}%). "
                f"Locking in gains on {ticker}."
            )

        elif "Max hold" in reasons:
            rationale.append(
                f"AUTO-APPROVED: Position age ({hold_days}d) exceeds "
                f"maximum hold policy ({HOLD_DAYS_MAX}d). Closing {ticker}."
            )

        elif "Momentum reversed" in reasons:
            if pnl_pct < -3 or hold_days > 30:
                rationale.append(
                    f"APPROVED: Bearish momentum reversal with meaningful "
                    f"drawdown ({pnl_pct:.1f}%) or extended hold ({hold_days}d). "
                    f"Exiting {ticker} to redeploy capital."
                )
            else:
                decision    = "MODIFIED"
                exec_action = "WATCH"
                rationale.append(
                    f"MODIFIED to WATCH: Momentum is bearish but loss "
                    f"({pnl_pct:.1f}%) and hold ({hold_days}d) are minor. "
                    f"Will re-evaluate next cycle before exiting {ticker}."
                )

        elif "Short stop-loss" in reasons or "cover" in reasons.lower():
            rationale.append(
                f"AUTO-APPROVED: Short risk rule triggered on {ticker}. Covering."
            )

        else:
            rationale.append(f"APPROVED: Review Agent sell signal on {ticker} accepted.")

    # ── TRIM decisions ─────────────────────────────────────────────────────
    elif action == "TRIM":
        limit = MAX_POSITION_SIZE_PCT * 100
        if weight_pct > limit * 1.3:
            rationale.append(
                f"APPROVED: {ticker} is {weight_pct:.1f}% of portfolio — "
                f"materially over {limit:.0f}% limit. Trimming to restore balance."
            )
        else:
            decision    = "MODIFIED"
            exec_action = "WATCH"
            rationale.append(
                f"MODIFIED to WATCH: {ticker} is {weight_pct:.1f}% vs {limit:.0f}% "
                f"limit — marginally over. Will trim only if it grows further."
            )

    # ── ADD decisions ──────────────────────────────────────────────────────
    elif action == "ADD":
        if pdo_signal == "HOLD_CASH":
            decision    = "REJECTED"
            exec_action = "HOLD"
            rationale.append(
                f"REJECTED: PDO signal is HOLD_CASH (bear market). "
                f"No new capital deployment approved for {ticker}."
            )
        elif buying_power_pct >= buy_bp_threshold and n_positions < MAX_OPEN_POSITIONS:
            pdo_note = f" [PDO: {pdo_signal}]" if pdo_signal else ""
            rationale.append(
                f"APPROVED: Adding to {ticker}. Buying power {buying_power_pct:.1f}% "
                f"({n_positions}/{MAX_OPEN_POSITIONS} positions). "
                f"Momentum confirms upside.{pdo_note}"
            )
        else:
            decision    = "REJECTED"
            exec_action = "HOLD"
            rationale.append(
                f"REJECTED: Would like to add to {ticker} but "
                f"{'insufficient buying power' if buying_power_pct < buy_bp_threshold else 'max positions reached'} "
                f"({buying_power_pct:.1f}% BP, {n_positions}/{MAX_OPEN_POSITIONS} positions). "
                f"Keeping current size."
            )

    # ── BUY_NEW decisions ──────────────────────────────────────────────────
    elif action == "BUY_NEW":
        conviction = rec.get("conviction", 3)
        horizon    = rec.get("investment_horizon", "both")
        pdo_note   = f" [PDO: {pdo_signal}]" if pdo_signal else ""

        # ── OptionsFlow multi-signal gate — board-mandated ─────────────────
        # If this recommendation originates from OptionsFlowMonitorAgent,
        # it must be confirmed by at least one corroborating signal in the
        # current research batch (same ticker appearing in momentum, earnings,
        # or factor screening recommendations) before conviction is accepted.
        # Without confirmation: conviction reduced by 1★ to prevent herding.
        if source_agent and "optionsflow" in source_agent.lower().replace("_", ""):
            # Check execution quality history for any same-ticker signals from
            # other agents this cycle (simple proxy: check review recommendations
            # for same ticker with different source agent)
            confirmed_by_other_agent = False
            review_path = "data/review_recommendations.json"
            if os.path.exists(review_path):
                try:
                    with open(review_path) as _f:
                        rr = json.load(_f)
                    other_signals = [
                        r for r in rr.get("recommendations", [])
                        if r.get("ticker") == ticker
                        and r.get("source_agent", "") != source_agent
                        and r.get("action") in ("BUY_NEW", "ADD")
                    ]
                    confirmed_by_other_agent = len(other_signals) > 0
                except Exception:
                    pass
            if not confirmed_by_other_agent:
                conviction = max(1, conviction - 1)
                rec = {**rec, "conviction": conviction}
                rationale.append(
                    f"[OPTIONS FLOW GATE] OptionsFlowMonitorAgent signal for {ticker} "
                    f"has no corroborating signal from other research agents this cycle. "
                    f"Conviction reduced by 1★ to {conviction}★ (herding prevention). "
                    f"Signal will be reconsidered if momentum or earnings catalyst agent "
                    f"produces a matching pick in the next cycle."
                )

        if pdo_signal == "HOLD_CASH":
            decision    = "REJECTED"
            exec_action = "HOLD"
            rationale.append(
                f"REJECTED: PDO signal is HOLD_CASH. No new positions approved. "
                f"{ticker} from {source_agent} held for next review cycle."
            )
        elif buying_power_pct >= buy_new_threshold and n_positions < MAX_OPEN_POSITIONS:
            rationale.append(
                f"APPROVED: New position in {ticker} (via {source_agent}). "
                f"Buying power {buying_power_pct:.1f}%, "
                f"capacity available ({n_positions}/{MAX_OPEN_POSITIONS}). "
                f"Conviction {conviction}★, horizon: {horizon}.{pdo_note}"
            )
            if cro_pdo_conflict:
                rationale.append(
                    f"[CRO/PDO CONFLICT — {cro_risk_level}] CRO risk level overrides PDO DEPLOY_MORE signal. "
                    f"Conservative thresholds applied. PDO's aggressive deployment guidance suspended this cycle."
                )
            elif cro_directives and cro_risk_level not in ("UNKNOWN",):
                rationale.append(
                    f"[CRO Advisory — {cro_risk_level}] {cro_directives[0]}"
                )
        elif conviction >= 5 and pdo_signal == "DEPLOY_MORE" and n_positions < MAX_OPEN_POSITIONS:
            # Exceptional case: max-conviction + PDO says deploy → override BP threshold
            rationale.append(
                f"APPROVED (override): Max-conviction (5★) pick {ticker} from {source_agent}. "
                f"PDO recommends deploying more capital — approving despite lower buying power "
                f"({buying_power_pct:.1f}%).{pdo_note}"
            )
        else:
            decision    = "REJECTED"
            exec_action = "HOLD"
            rationale.append(
                f"REJECTED: New position in {ticker} (via {source_agent}) declined — "
                f"{'insufficient buying power' if buying_power_pct < buy_new_threshold else 'at max positions'} "
                f"({buying_power_pct:.1f}% BP, {n_positions}/{MAX_OPEN_POSITIONS}). "
                f"Will revisit when capacity opens.{pdo_note}"
            )

    # ── WATCH / HOLD ───────────────────────────────────────────────────────
    elif action in ("WATCH", "HOLD"):
        decision    = "APPROVED"
        exec_action = action
        rationale.append(
            f"No trade required for {ticker}. "
            f"{'Flagged for close monitoring.' if action == 'WATCH' else 'Thesis intact.'}"
        )

    # ── Execution Quality Feedback — board-mandated (Special Session) ──────
    # If a source agent has a history of high-slippage fills (>30 bps average),
    # tighten its effective buy_new_threshold by 3pp to offset the execution drag.
    if action in ("BUY_NEW", "ADD"):
        try:
            eq_path = "data/execution_quality.json"
            if os.path.exists(eq_path):
                with open(eq_path) as _f:
                    eq_history = json.load(_f)
                agent_fills = [
                    e["slippage_bps"] for e in eq_history
                    if e.get("source_agent") == source_agent
                    and e.get("slippage_bps") is not None
                    and e.get("executed") is True
                ]
                if len(agent_fills) >= 5:
                    avg_slip = sum(agent_fills) / len(agent_fills)
                    if avg_slip > 30:
                        buy_new_threshold += 3
                        buy_bp_threshold  += 3
                        rationale.append(
                            f"[EXECUTION QUALITY] {source_agent} has avg slippage of "
                            f"{avg_slip:.1f} bps over {len(agent_fills)} trades. "
                            f"Buy thresholds raised by 3pp to offset execution drag."
                        )
        except Exception:
            pass   # execution quality adjustment is advisory — never block a trade

    # ── Historical Trade Review context ────────────────────────────────────
    try:
        trade_review = _load_trade_review()
        decision, exec_action = _apply_historical_context(
            rec, trade_review, rationale, decision, exec_action
        )
    except Exception:
        pass

    # ── Knowledge Base Enhancement ─────────────────────────────────────────
    # Apply Harvard/Buffett/Munger/UChicago frameworks to every decision
    try:
        from manager_knowledge_base import knowledge_enhanced_rationale, format_knowledge_rationale
        momentum_trend = (
            "bullish" if pnl_pct > 2 else
            "bearish" if pnl_pct < -2 else
            "neutral"
        )
        ke = knowledge_enhanced_rationale(
            ticker=ticker,
            action=exec_action,
            pnl_pct=pnl_pct,
            hold_days=hold_days,
            momentum_trend=momentum_trend,
            rsi=rec.get("rsi", 50),
            ret_3m=rec.get("ret_3m", 0.0),
            portfolio_value=portfolio_value,
            position_cost=max(1, weight_pct / 100 * portfolio_value),
            n_positions=n_positions,
            sector=rec.get("sector", ""),
            regime=pdo_signal or "neutral",
        )
        kb_lines = format_knowledge_rationale(ke)
        if kb_lines:
            rationale.extend(kb_lines)
    except Exception:
        pass   # knowledge base is advisory — never block a trade decision

    return {
        **rec,
        "manager_decision":  decision,
        "manager_action":    exec_action,
        "manager_rationale": rationale,
    }


# =============================================================================
# EXECUTION QUALITY AUDITOR
# =============================================================================
# Tracks and estimates trade execution quality to surface fill drag.
# Since paper trading uses market orders (no limit), slippage is the spread
# between decision-time price and fill price. We estimate it using the
# last-known review price vs the order submission timestamp as a proxy.
# Real fill prices from Alpaca are used when available.
#
# Metrics logged per trade:
#   - decision_price:   price at which the Review Agent flagged the trade
#   - submission_time:  ISO timestamp when order was submitted
#   - intraday_context: was this order sent at open/mid/close?
#   - estimated_slippage_bps: (fill - decision) / decision * 10000 (buy)
#                              (decision - fill) / decision * 10000 (sell)
# These are written into the execution result dict for the trade review team
# and ExecutionAuditorAgent to consume.
# =============================================================================

def _assess_execution_timing() -> str:
    """Classify the intraday timing of this order submission."""
    hour = datetime.now().hour
    minute = datetime.now().minute
    if hour == 9 and minute < 45:
        return "MARKET_OPEN"     # first 15 min — widest spreads, most volatile
    elif hour == 15 and minute >= 45:
        return "MARKET_CLOSE"    # last 15 min — elevated volatility
    elif hour < 9 or hour >= 16:
        return "PRE_POST_MARKET" # pre/post — illiquid
    else:
        return "MID_SESSION"     # normal conditions


def _estimate_slippage_bps(decision_price: float, fill_price: float,
                            action: str) -> Optional[float]:
    """
    Estimate slippage in basis points. Positive = adverse (we paid more to buy
    or received less to sell). Negative = favorable execution.
    """
    if not decision_price or not fill_price or decision_price <= 0:
        return None
    if action in ("BUY_NEW", "ADD"):
        return ((fill_price - decision_price) / decision_price) * 10_000
    elif action in ("SELL", "TRIM"):
        return ((decision_price - fill_price) / decision_price) * 10_000
    return None


EXECUTION_QUALITY_LOG = "data/execution_quality.json"


def _log_execution_quality(result: dict, decision: dict) -> None:
    """Append an execution quality record to the quality log for auditing."""
    record = {
        "timestamp":        result.get("timestamp"),
        "ticker":           result.get("ticker"),
        "action":           result.get("action"),
        "order_id":         result.get("order_id"),
        "executed":         result.get("executed"),
        "intraday_context": result.get("intraday_context"),
        "decision_price":   decision.get("current_price"),
        "fill_price":       result.get("fill_price"),
        "slippage_bps":     result.get("slippage_bps"),
        "conviction":       decision.get("conviction"),
        "source_agent":     decision.get("source_agent"),
        "manager_decision": decision.get("manager_decision"),
    }
    os.makedirs("data", exist_ok=True)
    history = []
    if os.path.exists(EXECUTION_QUALITY_LOG):
        try:
            with open(EXECUTION_QUALITY_LOG) as f:
                history = json.load(f)
        except Exception:
            pass
    history.append(record)
    # Keep last 200 execution records
    if len(history) > 200:
        history = history[-200:]
    try:
        with open(EXECUTION_QUALITY_LOG, "w") as f:
            json.dump(history, f, indent=2, default=str)
    except Exception:
        pass


# =============================================================================
# TRADE EXECUTOR
# =============================================================================

def execute_decision(decision: dict, trader, dry_run: bool = False) -> dict:
    """
    Execute the manager-approved action for a single position.
    Returns an execution result dict.
    """
    action  = decision["manager_action"]
    ticker  = decision["ticker"]
    result  = {"ticker": ticker, "action": action, "executed": False,
               "order_id": None, "error": None, "timestamp": datetime.now().isoformat(),
               "intraday_context": _assess_execution_timing()}

    if action in ("HOLD", "WATCH", "REJECTED"):
        result["executed"] = False
        result["note"]     = "No trade executed per manager decision."
        return result

    if dry_run:
        result["executed"] = True
        result["note"]     = f"[DRY RUN] Would execute {action} on {ticker}"
        logger.info(f"  [DRY RUN] {action} {ticker}")
        return result

    try:
        if action == "SELL":
            order = trader.submit_sell(ticker, reason="manager_agent")
            if order:
                result["executed"] = True
                result["order_id"] = order.get("id")
                fill_price = order.get("filled_avg_price") or order.get("limit_price")
                if fill_price:
                    result["fill_price"] = float(fill_price)
                    result["slippage_bps"] = _estimate_slippage_bps(
                        decision.get("current_price", 0), float(fill_price), "SELL"
                    )
                logger.info(f"  EXECUTED SELL: {ticker}  order_id={result['order_id']}")
            else:
                result["error"] = "submit_sell returned None — position may not exist"

        elif action == "TRIM":
            positions = trader.get_positions()
            if not positions.empty and ticker in positions["ticker"].values:
                qty_held = float(positions[positions["ticker"] == ticker]["qty"].values[0])
                trim_qty = max(1, int(abs(qty_held) * 0.40))   # sell 40% of position
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce
                order_req = MarketOrderRequest(
                    symbol=ticker,
                    qty=trim_qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                order = trader.client.submit_order(order_req)
                result["executed"] = True
                result["order_id"] = str(order.id)
                result["qty_trimmed"] = trim_qty
                logger.info(f"  EXECUTED TRIM: {trim_qty} shares of {ticker}  order_id={result['order_id']}")
            else:
                result["error"] = f"No position in {ticker} to trim"

        elif action in ("ADD", "BUY_NEW"):
            # Pass actual conviction so AlpacaTrader can size the position correctly
            order = trader.submit_buy(ticker, conviction_score=decision.get("conviction", 3))
            if order:
                result["executed"] = True
                result["order_id"] = order.get("id")
                # Capture fill price for slippage analysis if Alpaca returns it
                fill_price = order.get("filled_avg_price") or order.get("limit_price")
                if fill_price:
                    result["fill_price"] = float(fill_price)
                    result["slippage_bps"] = _estimate_slippage_bps(
                        decision.get("current_price", 0), float(fill_price), action
                    )
                logger.info(f"  EXECUTED BUY: {ticker}  order_id={result['order_id']}"
                            + (f"  slippage~{result.get('slippage_bps','—'):.1f}bps"
                               if result.get("slippage_bps") is not None else ""))
            else:
                result["error"] = "submit_buy returned None"

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"  Execution error for {ticker}: {e}")

    # ── Log execution quality for auditing ───────────────────────────────
    if result.get("executed"):
        try:
            _log_execution_quality(result, decision)
        except Exception:
            pass   # quality logging is non-critical — never block a trade

    return result


# =============================================================================
# REPORT BUILDER
# =============================================================================

def build_management_report(decisions: list[dict], executions: list[dict],
                             account: dict, dry_run: bool) -> str:
    """Build a plain-text management report for the Chairman."""
    now = datetime.now().strftime("%B %d, %Y at %H:%M")
    mode_label = "DRY RUN — no real orders placed" if dry_run else "LIVE PAPER TRADING"

    exec_map = {e["ticker"]: e for e in executions}

    lines = [
        "═" * 70,
        "  APEX CAPITAL INTELLIGENCE",
        "  MANAGER AGENT — EXECUTIVE REPORT",
        f"  {now}  |  {mode_label}",
        "═" * 70,
        "",
        "  ACCOUNT SNAPSHOT",
        "  " + "─" * 40,
        f"  Portfolio Value : ${account['portfolio_value']:>12,.2f}",
        f"  Cash            : ${account['cash']:>12,.2f}",
        f"  Buying Power    : ${account['buying_power']:>12,.2f}",
        f"  Open Positions  : {account['n_positions']}",
        (f"  Today's P&L     : "
         f"{'+' if account['pnl_today'] >= 0 else ''}"
         f"${abs(account['pnl_today']):.2f} "
         f"({'+' if account['pnl_today_pct'] >= 0 else ''}{account['pnl_today_pct']:.2f}%)"),
        "",
    ]

    # Group by decision outcome
    approved  = [d for d in decisions if d["manager_decision"] == "APPROVED"
                 and d["manager_action"] not in ("HOLD", "WATCH")]
    modified  = [d for d in decisions if d["manager_decision"] == "MODIFIED"]
    rejected  = [d for d in decisions if d["manager_decision"] == "REJECTED"]
    watch_hold= [d for d in decisions if d["manager_action"] in ("HOLD", "WATCH")]

    # ── CHANGES MADE ─────────────────────────────────────────────────────
    lines += ["  CHANGES APPROVED & EXECUTED", "  " + "─" * 40]
    if approved:
        for d in approved:
            ex    = exec_map.get(d["ticker"], {})
            ok    = ex.get("executed", False)
            oid   = ex.get("order_id", "—")
            err   = ex.get("error", "")
            status = f"✓ Executed  (order: {oid})" if ok else f"✗ Failed: {err}"
            lines.append(f"  [{d['manager_action']:<8}] {d['ticker']:<6}  {status}")
            for rat in d["manager_rationale"]:
                lines.append(f"              {rat}")
            lines.append("")
    else:
        lines += ["  None — no changes executed this cycle.", ""]

    # ── MODIFIED DECISIONS ─────────────────────────────────────────────
    if modified:
        lines += ["  RECOMMENDATIONS MODIFIED BY MANAGER", "  " + "─" * 40]
        for d in modified:
            lines.append(
                f"  [{d['action']:<8}→{d['manager_action']:<8}] {d['ticker']}"
            )
            for rat in d["manager_rationale"]:
                lines.append(f"              {rat}")
            lines.append("")

    # ── REJECTED DECISIONS ─────────────────────────────────────────────
    if rejected:
        lines += ["  RECOMMENDATIONS REJECTED BY MANAGER", "  " + "─" * 40]
        for d in rejected:
            lines.append(f"  [{d['action']:<8} REJECTED] {d['ticker']}")
            for rat in d["manager_rationale"]:
                lines.append(f"              {rat}")
            lines.append("")

    # ── UNCHANGED POSITIONS ────────────────────────────────────────────
    if watch_hold:
        lines += ["  POSITIONS WITH NO ACTION (HOLD / WATCH)", "  " + "─" * 40]
        for d in watch_hold:
            flag = "WATCH" if d["manager_action"] == "WATCH" else "HOLD"
            pnl  = d.get("unrealized_pnl_pct")
            pnl_s = f"{pnl:+.1f}%" if pnl is not None else "—"
            lines.append(
                f"  [{flag:<5}] {d['ticker']:<6}  P&L: {pnl_s:>7}  "
                f"Hold: {d.get('hold_days', 0):>3}d  {d['reasons'][0][:45]}"
            )
        lines.append("")

    lines += [
        "═" * 70,
        "  Apex Capital Intelligence — Manager Agent",
        "  Paper trading simulation — not real investment advice",
        f"  Generated {now}",
        "═" * 70,
    ]

    return "\n".join(lines)


def build_html_report(decisions: list[dict], executions: list[dict],
                      account: dict, dry_run: bool) -> str:
    """HTML version of the management report for email delivery."""
    now        = datetime.now().strftime("%B %d, %Y at %H:%M")
    mode_label = "DRY RUN" if dry_run else "PAPER TRADING"
    exec_map   = {e["ticker"]: e for e in executions}

    pnl_color = "#3fb950" if account["pnl_today"] >= 0 else "#f85149"
    pnl_sign  = "+" if account["pnl_today"] >= 0 else ""

    approved   = [d for d in decisions if d["manager_decision"] == "APPROVED"
                  and d["manager_action"] not in ("HOLD", "WATCH")]
    modified   = [d for d in decisions if d["manager_decision"] == "MODIFIED"]
    rejected   = [d for d in decisions if d["manager_decision"] == "REJECTED"]
    watch_hold = [d for d in decisions if d["manager_action"] in ("HOLD", "WATCH")]

    def decision_rows(group: list[dict], show_exec: bool = False) -> str:
        html = ""
        for d in group:
            ex  = exec_map.get(d["ticker"], {})
            ok  = ex.get("executed", False)
            oid = ex.get("order_id", "")
            err = ex.get("error", "")

            if show_exec:
                status_html = (
                    f'<span style="color:#3fb950;">✓ Executed {oid[:8] if oid else ""}</span>'
                    if ok else
                    f'<span style="color:#f85149;">✗ {err or "Failed"}</span>'
                )
            else:
                action_badge_color = {"MODIFIED": "#f0883e", "REJECTED": "#f85149"}.get(
                    d["manager_decision"], "#8b949e")
                status_html = (
                    f'<span style="color:{action_badge_color};">'
                    f'{d["manager_decision"]}</span>'
                )

            action_color = {
                "SELL": "#f85149", "TRIM": "#f0883e", "ADD": "#3fb950",
                "BUY_NEW": "#58a6ff", "WATCH": "#d2a679", "HOLD": "#8b949e",
            }.get(d["manager_action"], "#c9d1d9")

            pnl = d.get("unrealized_pnl_pct")
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "—"

            html += f"""
            <tr>
              <td style="padding:10px 8px;font-weight:bold;color:#58a6ff;">{d['ticker']}</td>
              <td style="padding:10px 8px;color:{action_color};font-weight:bold;">
                {d['manager_action']}</td>
              <td style="padding:10px 8px;color:#c9d1d9;">{pnl_str}</td>
              <td style="padding:10px 8px;color:#c9d1d9;">{d.get('hold_days', 0)}d</td>
              <td style="padding:10px 8px;">{status_html}</td>
            </tr>
            <tr>
              <td colspan="5" style="padding:0 8px 10px 8px;color:#8b949e;font-size:12px;
                  border-bottom:1px solid #21262d;">
                {d['manager_rationale'][0] if d['manager_rationale'] else ''}
              </td>
            </tr>"""
        return html or '<tr><td colspan="5" style="color:#8b949e;padding:12px;">None this cycle.</td></tr>'

    table_header = """
      <thead>
        <tr style="background:#21262d;">
          <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">TICKER</th>
          <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">ACTION</th>
          <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">P&L</th>
          <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">HELD</th>
          <th style="padding:10px 8px;text-align:left;color:#8b949e;font-size:12px;">STATUS</th>
        </tr>
      </thead>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',Arial,sans-serif;
             padding:24px;max-width:960px;margin:0 auto;">

  <div style="border-bottom:2px solid #21262d;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#58a6ff;margin:0;font-size:22px;">
      APEX CAPITAL — Manager Agent Report
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:13px;">
      Chairman: Jack Beard &nbsp;·&nbsp; {now} &nbsp;·&nbsp; {mode_label}
    </p>
  </div>

  <!-- account snapshot -->
  <div style="background:#161b22;border:1px solid #30363d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;display:flex;gap:28px;flex-wrap:wrap;">
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Portfolio Value</div>
      <div style="font-size:22px;font-weight:bold;">${account['portfolio_value']:,.2f}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Cash</div>
      <div style="font-size:22px;font-weight:bold;">${account['cash']:,.2f}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Buying Power</div>
      <div style="font-size:22px;font-weight:bold;">${account['buying_power']:,.2f}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Open Positions</div>
      <div style="font-size:22px;font-weight:bold;">{account['n_positions']}</div>
    </div>
    <div>
      <div style="color:#8b949e;font-size:11px;text-transform:uppercase;">Today's P&amp;L</div>
      <div style="font-size:22px;font-weight:bold;color:{pnl_color};">
        {pnl_sign}${abs(account['pnl_today']):.2f} ({pnl_sign}{account['pnl_today_pct']:.2f}%)</div>
    </div>
  </div>

  <!-- changes executed -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Changes Approved &amp; Executed</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px;">
    {table_header}
    <tbody>{decision_rows(approved, show_exec=True)}</tbody>
  </table>

  <!-- modified -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Recommendations Modified by Manager</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px;">
    {table_header}
    <tbody>{decision_rows(modified)}</tbody>
  </table>

  <!-- rejected -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Recommendations Rejected by Manager</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px;">
    {table_header}
    <tbody>{decision_rows(rejected)}</tbody>
  </table>

  <!-- hold/watch -->
  <h2 style="color:#e6edf3;font-size:16px;margin-bottom:12px;">Positions Held (No Action)</h2>
  <table style="width:100%;border-collapse:collapse;background:#161b22;
                border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px;">
    {table_header}
    <tbody>{decision_rows(watch_hold)}</tbody>
  </table>

  <p style="color:#484f58;font-size:11px;margin-top:28px;border-top:1px solid #21262d;
            padding-top:12px;">
    Apex Capital Intelligence — Manager Agent &nbsp;·&nbsp;
    Paper trading simulation — not real investment advice &nbsp;·&nbsp;
    Generated {now}
  </p>
</body></html>"""
    return html


# =============================================================================
# MAIN ORCHESTRATION
# =============================================================================

def _load_pdo_signal() -> Optional[str]:
    """Load the latest PDO recommendation from disk."""
    pdo_path = "data/capital_efficiency_report.json"
    if not os.path.exists(pdo_path):
        return None
    try:
        with open(pdo_path) as f:
            return json.load(f).get("recommendation")
    except Exception:
        return None


def _load_trade_review() -> dict:
    """Load the latest historical trade review from disk."""
    path = "data/trade_review_report.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _apply_historical_context(rec: dict, trade_review: dict,
                               rationale: list, decision: str,
                               exec_action: str) -> tuple[str, str]:
    """
    Adjust a BUY_NEW/ADD decision based on historical trade review directives.
    Returns (possibly updated decision, exec_action).
    """
    if not trade_review or rec["action"] not in ("BUY_NEW", "ADD"):
        return decision, exec_action

    directives = trade_review.get("manager_directives", [])
    analytics  = trade_review.get("analytics", {})
    ticker     = rec.get("ticker", "")
    conviction = rec.get("conviction", 3)

    # Check if the ticker was previously traded and how it performed
    past_trades = [
        t for t in trade_review.get("trades", [])
        if t.get("ticker") == ticker
    ]
    if past_trades:
        latest = sorted(past_trades, key=lambda t: t.get("exit_date", ""))[-1]
        pnl    = latest.get("pnl_pct", 0)
        if pnl < -5:
            rationale.append(
                f"[HISTORY] {ticker} was previously closed at {pnl:+.1f}%. "
                "Extra scrutiny applied — ensure thesis has materially changed."
            )
        elif pnl > 5:
            rationale.append(
                f"[HISTORY] {ticker} was a previous winner (+{pnl:.1f}%). "
                "Favourable setup precedent."
            )

    # Apply directives from Review Team
    for d in directives:
        rule = d.get("rule", "")
        if rule == "RAISE_MIN_CONVICTION" and conviction < d.get("threshold", 4):
            if decision == "APPROVED":
                decision    = "MODIFIED"
                exec_action = "HOLD"
                rationale.append(
                    f"[REVIEW TEAM DIRECTIVE] {d['reason']} "
                    f"This pick has conviction {conviction}★ — below the raised minimum. Holding."
                )
        elif rule == "PREFER_HIGH_CONVICTION_ONLY" and conviction < 4:
            if decision == "APPROVED":
                decision    = "MODIFIED"
                exec_action = "HOLD"
                rationale.append(
                    f"[REVIEW TEAM DIRECTIVE] {d['reason']} Skipping {ticker} ({conviction}★)."
                )

    return decision, exec_action


def _load_cro_bulletin() -> dict:
    """
    Load the Chief Risk Officer's public risk bulletin.
    The CRO openly advises all agents on current risk conditions and directives.
    This bulletin contains ONLY risk guidance — no grades, no performance
    evaluations. The Manager Agent is expected to respect CRO directives.
    """
    path = "data/risk_bulletin.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_energy_research_picks() -> list[dict]:
    """Load energy research team picks from disk."""
    path = "data/energy_research_report.json"
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f).get("recommendations", [])
    except Exception:
        return []


def run_manager(dry_run: bool = False, skip_review: bool = False,
                skip_email: bool = False,
                extra_recommendations: Optional[list] = None,
                canary_alert: Optional[dict] = None) -> None:
    mode = "DRY RUN" if dry_run else "LIVE"
    logger.info(f"\n{'='*60}")
    logger.info(f"  APEX CAPITAL — MANAGER AGENT  [{mode}]")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}\n")

    # ── Canary Defensive Mode banner ─────────────────────────────────────
    canary_level = (canary_alert or {}).get("level", "GREEN")
    if canary_level in ("RED", "ORANGE"):
        triggers = (canary_alert or {}).get("triggers", [])
        logger.warning(f"  ⚠️  CANARY {canary_level} ALERT — DEFENSIVE MODE ACTIVE")
        logger.warning(f"  Triggers: {', '.join(triggers)}")
        logger.warning(f"  All BUY/ADD orders BLOCKED. SELLs fast-tracked.\n")

    # ── Step 1: Get review recommendations ──────────────────────────────
    if not skip_review:
        logger.info("  Step 1/5: Running Review Agent...")
        from review_agent import run_review
        review_report = run_review(quiet=True)
    else:
        logger.info("  Step 1/5: Loading saved review recommendations...")
        if not os.path.exists(REVIEW_PATH):
            logger.error(f"  No saved review found at {REVIEW_PATH}. Run without --no-review.")
            return
        with open(REVIEW_PATH) as f:
            review_report = json.load(f)

    recommendations = list(review_report["recommendations"])
    account         = review_report["account"]

    # Merge in energy research picks (avoid duplicate tickers)
    energy_picks  = extra_recommendations or _load_energy_research_picks()
    existing_tickers = {r["ticker"] for r in recommendations}
    for pick in energy_picks:
        if pick["ticker"] not in existing_tickers:
            recommendations.append(pick)
            existing_tickers.add(pick["ticker"])

    # Load PDO signal to calibrate approval thresholds
    pdo_signal = _load_pdo_signal()
    if pdo_signal:
        logger.info(f"  PDO signal: {pdo_signal} — adjusting approval thresholds accordingly")

    # Load CRO public risk bulletin
    cro_bulletin = _load_cro_bulletin()
    cvar_circuit_breaker_active = False
    if cro_bulletin:
        cro_level_run = cro_bulletin.get("risk_level", "UNKNOWN")
        logger.info(
            f"  CRO advisory: {cro_level_run} risk | "
            f"{cro_bulletin.get('capital_guidance','—')}"
        )
        for alert in cro_bulletin.get("position_alerts", []):
            logger.warning(
                f"  [CRO ALERT] {alert.get('ticker','?')}: {alert.get('alert','')}"
            )
        # ── CVaR CIRCUIT BREAKER — board-mandated (Special Session) ─────────
        # If 1-day CVaR exceeds 5% of portfolio value, all BUY/ADD blocked for session.
        var_context = cro_bulletin.get("var_context", {})
        cvar_1d     = var_context.get("cvar_95", 0)
        pv          = account.get("portfolio_value", 1)
        cvar_pct    = (cvar_1d / pv * 100) if pv > 0 else 0
        CVAR_CIRCUIT_BREAKER_PCT = 5.0
        if cvar_pct > CVAR_CIRCUIT_BREAKER_PCT:
            cvar_circuit_breaker_active = True
            logger.warning(
                f"  ⚡ [CVAR CIRCUIT BREAKER] 1-day CVaR = {cvar_pct:.1f}% of portfolio "
                f"(${cvar_1d:,.0f}) exceeds {CVAR_CIRCUIT_BREAKER_PCT:.0f}% threshold. "
                f"All BUY and ADD orders BLOCKED this session."
            )
        # Detect CRO/PDO conflict at the run level — log prominently
        if cro_level_run in ("HIGH", "CRITICAL") and pdo_signal == "DEPLOY_MORE":
            logger.warning(
                f"  ⚡ [CRO/PDO CONFLICT] CRO={cro_level_run} vs PDO=DEPLOY_MORE. "
                f"CRO signal takes precedence — aggressive deployment BLOCKED. "
                f"Buy thresholds tightened to 25%/30% BP. PDO override capability suspended."
            )
        elif cro_level_run in ("ELEVATED",) and pdo_signal == "DEPLOY_MORE":
            logger.warning(
                f"  ⚠ [CRO/PDO TENSION] CRO=ELEVATED vs PDO=DEPLOY_MORE. "
                f"Proceeding with elevated thresholds (20%/25% BP)."
            )

    logger.info(f"  {len(recommendations)} recommendation(s) to evaluate "
                f"({len(review_report['recommendations'])} from Review Agent, "
                f"{len(energy_picks)} from Energy Research Team)")

    # ── Step 2: Evaluate each recommendation ──────────────────────────────
    logger.info("  Step 2/5: Manager evaluating recommendations...")
    decisions = [evaluate_recommendation(rec, account, pdo_signal=pdo_signal,
                                         canary_alert=canary_alert,
                                         cvar_circuit_breaker=cvar_circuit_breaker_active)
                 for rec in recommendations]

    approved_count = sum(1 for d in decisions
                         if d["manager_decision"] == "APPROVED"
                         and d["manager_action"] not in ("HOLD", "WATCH"))

    logger.info(f"  Decisions: {approved_count} approved for execution, "
                f"{sum(1 for d in decisions if d['manager_decision']=='MODIFIED')} modified, "
                f"{sum(1 for d in decisions if d['manager_decision']=='REJECTED')} rejected")

    # ── Step 3: Execute approved trades ──────────────────────────────────
    logger.info("  Step 3/5: Executing approved changes...")
    executions: list[dict] = []

    if approved_count > 0:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()

        for d in decisions:
            if d["manager_decision"] == "APPROVED" and d["manager_action"] not in ("HOLD", "WATCH"):
                ex = execute_decision(d, trader, dry_run=dry_run)
                executions.append(ex)
    else:
        logger.info("  No trades to execute this cycle.")

    # ── Step 4: Persist decision log ──────────────────────────────────────
    logger.info("  Step 4/5: Saving decision log...")
    os.makedirs("data", exist_ok=True)
    log_entry = {
        "timestamp":     datetime.now().isoformat(),
        "dry_run":       dry_run,
        "account":       account,
        "decisions":     decisions,
        "executions":    executions,
    }

    history: list = []
    if os.path.exists(MANAGER_LOG):
        try:
            with open(MANAGER_LOG) as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append(log_entry)
    with open(MANAGER_LOG, "w") as f:
        json.dump(history[-50:], f, indent=2, default=str)   # keep last 50 runs

    logger.info(f"  Decision log saved → {MANAGER_LOG}")

    # ── Step 5: Build and send report ─────────────────────────────────────
    logger.info("  Step 5/5: Building management report...")
    text_report = build_management_report(decisions, executions, account, dry_run)
    html_report = build_html_report(decisions, executions, account, dry_run)

    # Always print to console
    print("\n" + text_report)

    if not skip_email:
        try:
            from email_sender import send_daily_report
            mode_label = " (DRY RUN)" if dry_run else ""
            subject = (
                f"Apex Capital — Manager Report{mode_label} | "
                f"{datetime.now().strftime('%b %d, %Y %H:%M')}"
            )
            from creative_agent import polish_email, metrics_for_manager
            polished_html = polish_email(
                html_report, subject, "manager_agent",
                metrics_for_manager(decisions, executions, account,
                                    pdo_signal=pdo_signal or "—"),
                priority="urgent" if any(
                    "Stop-loss" in " ".join(d.get("manager_rationale", []))
                    for d in decisions
                ) else "normal",
            )
            success = send_daily_report(
                html_body=polished_html,
                subject=subject,
                paper_day=0,
                sender_name="manager_agent",
                key_metrics=metrics_for_manager(decisions, executions, account,
                                                pdo_signal=pdo_signal or "—"),
            )
            if success:
                logger.info("  Report emailed to Chairman (jack.beard07@gmail.com)")
            else:
                logger.warning("  Email failed — report printed to console only")
        except Exception as e:
            logger.warning(f"  Email send error: {e} — report printed to console only")
    else:
        logger.info("  Email skipped (--no-email flag set)")

    # ── Canary RED: consider SH hedge ────────────────────────────────────
    if canary_level == "RED" and not dry_run:
        logger.warning("  [CANARY RED] Checking SH inverse-ETF hedge...")
        try:
            from alpaca_trader import AlpacaTrader
            hedge_trader = AlpacaTrader()
            positions    = hedge_trader.get_positions()
            sh_held = (
                not positions.empty and "SH" in positions["ticker"].values
                if hasattr(positions, "empty") else False
            )
            if not sh_held:
                order = hedge_trader.submit_buy("SH", conviction_score=0.5)
                if order:
                    logger.warning("  [CANARY RED] ✅ Opened SH hedge position (inverse S&P ETF)")
                else:
                    logger.warning("  [CANARY RED] SH order returned None — may already be held")
            else:
                logger.info("  [CANARY RED] SH hedge already in portfolio")
        except Exception as e:
            logger.warning(f"  [CANARY RED] SH hedge attempt failed: {e}")

    # ── Final summary ──────────────────────────────────────────────────────
    executed_ok = [e for e in executions if e.get("executed")]
    executed_fail = [e for e in executions if not e.get("executed") and e.get("error")]
    print(f"\n  SUMMARY: {len(executed_ok)} trade(s) executed"
          f"{', ' + str(len(executed_fail)) + ' failed' if executed_fail else ''}")
    if dry_run:
        print("  (DRY RUN — no real orders were placed)")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    dry_run_flag    = "--dry-run"    in sys.argv
    no_review_flag  = "--no-review"  in sys.argv
    no_email_flag   = "--no-email"   in sys.argv

    run_manager(
        dry_run=dry_run_flag,
        skip_review=no_review_flag,
        skip_email=no_email_flag,
    )
