#!/usr/bin/env python3
"""
APEX CAPITAL -- FIXED INCOME PIPELINE
=======================================
Daily orchestrator for the Fixed Income pod.
Capital: $30,000
Strategy: Buy bonds/credit instruments at attractive spreads to risk-free rate.
Same margin-of-safety philosophy as First Eagle applied to DEBT.

Pipeline steps:
  1. Credit market conditions (fi_credit_monitor)
  2. Run all 7 R&D research agents -> picks
  3. Score each pick (fi_data_layer.get_bond_opportunity_score)
  4. Filter: only STRONG_BUY or BUY proceed
  5. Size positions (regime-adaptive allocation)
  6. Write fi_picks_queue.json
  7. Run FI board (fi_board.py)
  8. Send daily email

Run: python3 fi_pipeline.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

PICKS_QUEUE_FILE = DATA_DIR / "fi_picks_queue.json"
PIPELINE_LOG_FILE = DATA_DIR / "fi_pipeline_log.json"

FI_CAPITAL = 30000.00

# Position sizing caps
ETF_MAX_POSITION   = 5000.0   # per ETF
ISSUER_MAX_POSITION = 3000.0  # per individual issuer

# Regime-adaptive credit allocation (% of FI_CAPITAL)
REGIME_ALLOCATION = {
    "TIGHT":      {"hy": 0.30, "ig": 0.50, "treasury": 0.20},
    "NORMAL":     {"hy": 0.40, "ig": 0.40, "treasury": 0.20},
    "WIDE":       {"hy": 0.60, "ig": 0.30, "treasury": 0.10},
    "DISTRESSED": {"hy": 0.70, "ig": 0.20, "treasury": 0.10},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("fi_pipeline")

# Research agents to run
RESEARCH_AGENTS = [
    ("agents/cos_fi_creditspread_agent.py",    "FICreditSpreadAgent"),
    ("agents/cos_fi_investmentgrade_agent.py", "FIInvestmentGradeAgent"),
    ("agents/cos_fi_highyield_agent.py",       "FIHighYieldAgent"),
    ("agents/cos_fi_duration_agent.py",        "FIDurationAgent"),
    ("agents/cos_fi_distressed_agent.py",      "FIDistressedAgent"),
    ("agents/cos_fi_convertible_agent.py",     "FIConvertibleAgent"),
    ("agents/cos_fi_preferred_agent.py",       "FIPreferredAgent"),
]


def _load_agent(file_rel: str, class_name: str):
    """Dynamically load agent class from file."""
    path = BASE_DIR / file_rel
    spec = importlib.util.spec_from_file_location(class_name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, class_name)


def _size_position(pick: dict, regime: str, spreads: dict) -> float:
    """
    Determine position size in USD.
    ETFs: up to $5,000. Issuers: up to $3,000.
    Duration targets: inverted curve -> 40% in TLT/IEF; normal -> 20%.
    """
    ticker = pick.get("ticker", "")
    category = pick.get("category", "etf").lower()
    score  = pick.get("opportunity_score", 50)
    is_duration = ticker in {"TLT", "IEF", "SHY", "SGOV", "BIL"}

    curve_inverted = spreads.get("yield_curve_slope", 0) < 0

    if is_duration:
        duration_budget = FI_CAPITAL * (0.40 if curve_inverted else 0.20)
        max_pos = min(ETF_MAX_POSITION, duration_budget)
    elif category == "etf":
        alloc = REGIME_ALLOCATION.get(regime, REGIME_ALLOCATION["NORMAL"])
        if ticker in {"HYG", "JNK", "USHY"}:
            max_pos = min(ETF_MAX_POSITION, FI_CAPITAL * alloc["hy"] * 0.5)
        elif ticker in {"LQD", "VCIT", "USIG"}:
            max_pos = min(ETF_MAX_POSITION, FI_CAPITAL * alloc["ig"] * 0.5)
        else:
            max_pos = ETF_MAX_POSITION
    else:
        max_pos = ISSUER_MAX_POSITION

    # Scale by score: STRONG_BUY (75+) = full; BUY (60-74) = 75%; else 50%
    if score >= 75:
        size = max_pos
    elif score >= 60:
        size = max_pos * 0.75
    else:
        size = max_pos * 0.50

    return round(size, 2)


def run_fi_pipeline():
    log.info("=" * 60)
    log.info("APEX CAPITAL -- FIXED INCOME PIPELINE")
    log.info("Capital: $%.0f", FI_CAPITAL)
    log.info("=" * 60)

    # Step 1: Credit market conditions
    from fi_credit_monitor import run_credit_monitor
    market  = run_credit_monitor()
    spreads = market["spreads"]
    regime  = spreads["spread_regime"]
    signals = market["signals"]

    _f = lambda v, fmt: "n/a" if v is None else format(v, fmt)
    log.info(
        "HY spread: %sbps | IG spread: %sbps | 10yr: %s%% | "
        "Slope: %s | Regime: %s | Signals: %d | Data: %s",
        _f(spreads["hy_spread"], ".0f"), _f(spreads["ig_spread"], ".0f"),
        _f(spreads["treasury_10yr"], ".2f"), _f(spreads["yield_curve_slope"], ".3f"),
        regime, len(signals), market.get("data_quality", "UNKNOWN"),
    )

    # ── Step 1b: no credit read => no pipeline ────────────────────────────
    # Every stage below is a function of the regime: the agents score off it,
    # fi_data_layer sizes off it, the board votes on it. There is no partial
    # answer available when the regime is UNKNOWN, and the wrong thing to do
    # is the thing this code used to do implicitly — treat a dead feed as a
    # normal market and trade the full book on it.
    #
    # Abstaining is cheap: FI runs once a weekday and its holdings are bond
    # ETFs on a quarterly review cycle. Missing one session costs nothing.
    if regime == "UNKNOWN" or market.get("data_quality") != "LIVE":
        _missing = ", ".join(market.get("degraded") or ["unspecified feed"])
        log.error("=" * 60)
        log.error("FI PIPELINE ABSTAINING — credit read is DEGRADED (%s).", _missing)
        log.error("The regime gates every downstream stage, so there is no "
                  "partial run to make. Zero picks, zero orders. Next scheduled "
                  "run re-fetches; the degraded read is not cached.")
        log.error("=" * 60)
        _abstain = {
            "run_at":        datetime.now(timezone.utc).isoformat(),
            "capital":       FI_CAPITAL,
            "regime":        "UNKNOWN",
            "spreads":       spreads,
            "data_quality":  market.get("data_quality", "DEGRADED"),
            "degraded":      market.get("degraded", []),
            "raw_picks":     0,
            "qualified":     0,
            "abstained":     True,
            "abstain_reason": (f"credit read degraded — no data from {_missing}. "
                               f"Scoring off default spreads would fabricate a regime."),
            "agent_results": [],
            "board_result":  {"approved_count": 0, "rejected_count": 0,
                              "approved": [], "rejected": []},
        }
        with open(DATA_DIR / "fi_pipeline_report.json", "w") as f:
            json.dump(_abstain, f, indent=2)
        return _abstain

    # Step 2: Run all 7 R&D agents
    raw_picks = []
    agent_results = []
    for file_rel, class_name in RESEARCH_AGENTS:
        try:
            AgentClass = _load_agent(file_rel, class_name)
            agent = AgentClass(spread_context=spreads)
            report = agent.run()
            recs = report.get("recommendations", [])
            log.info("Agent %s returned %d picks", class_name, len(recs))
            raw_picks.extend(recs)
            agent_results.append({
                "agent":   class_name,
                "picks":   len(recs),
                "status":  "ok",
            })
        except Exception as exc:
            log.error("Agent %s failed: %s", class_name, exc)
            agent_results.append({
                "agent":  class_name,
                "picks":  0,
                "status": f"error: {exc}",
            })

    log.info("Total raw picks from agents: %d", len(raw_picks))

    # Deduplicate by ticker (keep highest conviction)
    seen = {}
    for p in raw_picks:
        t = p.get("ticker", "")
        if t not in seen or p.get("conviction", 0) > seen[t].get("conviction", 0):
            seen[t] = p
    deduped = list(seen.values())
    log.info("After deduplication: %d unique picks", len(deduped))

    # Step 3: Score each pick
    from fi_data_layer import get_bond_opportunity_score
    scored_picks = []
    for pick in deduped:
        ticker = pick.get("ticker", "")
        try:
            opp = get_bond_opportunity_score(ticker, spreads)
            pick["opportunity_score"] = opp["opportunity_score"]
            pick["verdict"]           = opp["verdict"]
            pick["credit_tier"]       = opp["credit_quality"].get("credit_tier", "UNKNOWN")
            pick["credit_score"]      = opp["credit_quality"].get("credit_score", 50)
        except Exception as exc:
            log.warning("Scoring %s failed: %s", ticker, exc)
            pick["opportunity_score"] = 40
            pick["verdict"]           = "HOLD"
        scored_picks.append(pick)

    # Step 4: Filter — include WATCH when BUY/STRONG_BUY candidates are scarce
    # (Chairman directive 2026-06-09: agents should widen scope and surface more
    # candidates. Manager and FI board make the final call; we don't pre-filter
    # too aggressively here.)
    strong_picks = [p for p in scored_picks if p.get("verdict") in ("STRONG_BUY", "BUY")]
    if len(strong_picks) >= 3:
        qualified = strong_picks
        log.info("Qualified picks (BUY/STRONG_BUY): %d of %d", len(qualified), len(scored_picks))
    else:
        # Not enough high-conviction picks — include WATCH tier so the board
        # has candidates to review rather than seeing an empty queue
        qualified = [p for p in scored_picks if p.get("verdict") in ("STRONG_BUY", "BUY", "WATCH")]
        log.info(
            "Sparse BUY pipeline (%d). Widening to WATCH tier: %d total candidates.",
            len(strong_picks), len(qualified)
        )

    # Step 5: Size positions
    for pick in qualified:
        pick["position_size_usd"] = _size_position(pick, regime, spreads)

    # Sort by score desc
    qualified.sort(key=lambda p: p.get("opportunity_score", 0), reverse=True)

    # Step 6: Write fi_picks_queue.json
    queue_payload = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "capital":       FI_CAPITAL,
        "regime":        regime,
        "spreads":       spreads,
        "market_signals": signals,
        "picks":         qualified,
        "total_picks":   len(qualified),
        "agent_results": agent_results,
    }
    with open(PICKS_QUEUE_FILE, "w") as f:
        json.dump(queue_payload, f, indent=2, default=str)
    log.info("Picks queue written -> %s (%d picks)", PICKS_QUEUE_FILE, len(qualified))

    # Step 7: Run FI board
    board_result = {}
    try:
        import fi_board
        board_result = fi_board.run_fi_board()
        log.info("FI board complete: %d approved, %d rejected",
                 board_result.get("approved_count", 0),
                 board_result.get("rejected_count", 0))
    except Exception as exc:
        log.error("FI board failed: %s", exc)
        board_result = {"error": str(exc)}

    # Step 8: Send daily email (never let email formatting kill the pipeline)
    try:
        _send_daily_email(spreads, regime, signals, qualified, board_result)
    except Exception as _em_exc:
        log.error("FI daily email failed (non-fatal): %s", _em_exc)

    # Write pipeline log
    pipeline_log = {
        "run_at":        datetime.now(timezone.utc).isoformat(),
        "capital":       FI_CAPITAL,
        "regime":        regime,
        "spreads":       spreads,
        "raw_picks":     len(raw_picks),
        "qualified":     len(qualified),
        "agent_results": agent_results,
        "board_result":  board_result,
    }
    with open(PIPELINE_LOG_FILE, "w") as f:
        json.dump(pipeline_log, f, indent=2, default=str)
    # Logic Team and PodHealth monitors check fi_pipeline_report.json —
    # write the same payload there so health checks see the run.
    with open(DATA_DIR / "fi_pipeline_report.json", "w") as f:
        json.dump(pipeline_log, f, indent=2, default=str)

    log.info("FI pipeline complete.")
    return pipeline_log


def _send_daily_email(spreads, regime, signals, picks, board_result):
    """
    Buffer the Fixed Income daily summary for inclusion in the 7 PM evening digest.

    The email_buffer system (email_buffer.py) collects reports from all pods
    throughout the day and sends a single consolidated digest at 7 PM.  This
    prevents duplicate emails when the pipeline runs more than once per day
    (e.g., from the recovery agent or catch-up scheduler).

    A sentinel file (data/fi_email_sent_YYYY-MM-DD.flag) prevents the same
    day's report from being buffered more than once even if this function is
    called multiple times.
    """
    date_str     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sentinel     = DATA_DIR / f"fi_email_sent_{date_str}.flag"

    # Guard: only buffer once per calendar day
    if sentinel.exists():
        log.info("Daily email already buffered for %s — skipping duplicate", date_str)
        return

    subject  = f"Apex Capital — Fixed Income Daily | {date_str}"

    alloc = REGIME_ALLOCATION.get(regime, REGIME_ALLOCATION["NORMAL"])
    lines = [
        "APEX CAPITAL — FIXED INCOME DAILY REPORT",
        "=" * 55,
        f"Date:   {date_str}",
        f"Capital: ${FI_CAPITAL:,.0f}",
        "",
        "CREDIT MARKET CONDITIONS",
        "-" * 40,
        f"  Regime:           {regime}",
        f"  HY Spread:        {spreads['hy_spread']:.0f} bps  (basis points — the extra yield premium",
        f"                    that high-yield/junk bonds pay over safe US Treasuries)",
        f"  IG Spread:        {spreads['ig_spread']:.0f} bps  (investment-grade corporate bond spread)",
        f"  10yr Treasury:    {spreads['treasury_10yr']:.2f}%",
        f"  2yr Treasury:     {spreads.get('treasury_2yr', 0) or 0:.2f}%",
        f"  Yield Curve:      {spreads['yield_curve_slope']:.3f}% ({'INVERTED — short rates > long rates, historically a recession signal' if spreads['yield_curve_slope'] < 0 else 'NORMAL'})",
        f"  Fed Funds:        {spreads.get('fed_funds', 0) or 0:.2f}%  (overnight lending rate set by the Federal Reserve)",
        "",
        "REGIME ALLOCATION TARGETS",
        "-" * 40,
        f"  High Yield:  {alloc['hy']*100:.0f}%  (${FI_CAPITAL*alloc['hy']:,.0f})",
        f"  Inv Grade:   {alloc['ig']*100:.0f}%  (${FI_CAPITAL*alloc['ig']:,.0f})",
        f"  Treasuries:  {alloc['treasury']*100:.0f}%  (${FI_CAPITAL*alloc['treasury']:,.0f})",
        "",
    ]

    if signals:
        lines += ["MARKET SIGNALS", "-" * 40]
        for s in signals:
            lines.append(f"  [{s['type']}] {s.get('instrument','')}: {s.get('reason', s.get('message',''))}")
        lines.append("")

    if picks:
        lines += [f"PICKS SUBMITTED TO BOARD ({len(picks)} total)", "-" * 40]
        for p in picks[:10]:
            lines.append(
                f"  {p.get('ticker','?'):6s} | Score: {p.get('opportunity_score',0):3.0f} "
                f"| {p.get('verdict','?'):10s} | ${p.get('position_size_usd',0):,.0f}"
            )
        lines.append("")

    approved = board_result.get("approved_count", 0)
    rejected = board_result.get("rejected_count", 0)
    lines += [
        "BOARD RESULT",
        "-" * 40,
        f"  Approved: {approved}",
        f"  Rejected: {rejected}",
        "",
        "Apex Capital Fixed Income Pod",
    ]

    body = "\n".join(lines)
    try:
        from email_buffer import buffer_email
        buffer_email(subject, body, sender_name="FI Pipeline")
        sentinel.touch()
        log.info("Daily email buffered for evening digest: %s", subject)
    except Exception as exc:
        log.error("Daily email buffer failed: %s", exc)


if __name__ == "__main__":
    run_fi_pipeline()
