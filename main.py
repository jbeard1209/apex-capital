#!/usr/bin/env python3
# =============================================================================
# MAIN ORCHESTRATOR — Daily Trading Pipeline
# =============================================================================
# Runs once per day (triggered by scheduler at 4:15 PM ET):
#
#   PRE-TRADE LAYER (safety + fresh context before any money moves)
#   0.  Stale-exit enforcement — close any SELL signals unexecuted from last cycle
#   0b. SystemPerformanceAnalyst — system health audit (runs every cycle, not just R&D days)
#   1.  Connect to Alpaca paper trading
#   2.  Enforce stop-loss / take-profit / max-hold rules (skips Step-0 closes — no dupe orders)
#   2b. Manage regime hedge (SH inverse ETF)
#   2c. Manage momentum short book
#   2d. CRO (Chief Risk Officer) — publish fresh risk bulletin + CVaR check
#   2d-b. Proactive tail hedge
#   2e. PDO (Portfolio Deployment Officer) — capital efficiency signal
#       ↳ MOVED HERE from old Step 3b so 13F trades use TODAY's PDO signal, not yesterday's
#
#   SIGNAL + RESEARCH LAYER
#   3.  Scan SEC EDGAR for new 13F institutional signals
#   3a. Execute 13F buy orders — full risk pipeline (canary + PDO + CRO + CVaR)
#   3b. Historical Trade Review Team (weekly post-mortem)
#   3c. R&D Team — sector scan + research picks (every 3 days; 72h TTL on saved picks)
#       Phase D: Energy Research Team (8 sub-sector agents, runs alongside R&D)
#   3d. Context agents — Historical + Geopolitical (every 2 days, independent sub-cycle)
#
#   REVIEW + MANAGEMENT LAYER
#   4.  Review Agent — full portfolio analysis → ranked recommendations
#   4b. Manager Agent — decisions & execution
#       ↳ Canary re-checked here — catches alerts that fired mid-pipeline
#
#   REPORTING LAYER
#   5.  Portfolio snapshot
#   6.  Backtest results (day 1 or --backtest flag)
#   7.  HTML report
#   8.  Email daily report
#   9.  Chief of Staff — daily brief + Agent Factory expansion
#   10. HR Team — evaluates all agents (reports to IRM; IRM eval is sealed)
#   11. IRM — independent board consultation + HR relay to Chairman
#   12. Automatic Board Meeting (CRO HIGH/CRITICAL, IRM ≥3 flags, or portfolio -4% today)
#       ↳ Board can override Manager Agent with 3/4 vote (MAJORITY or UNANIMOUS)
# =============================================================================

import os
import sys
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
LOG_DIR  = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State File — tracks paper trading day count & position entry dates
# ---------------------------------------------------------------------------
STATE_FILE = Path(__file__).parent / "data" / "state.json"
STATE_FILE.parent.mkdir(exist_ok=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            data = json.load(f)
        # Back-fill any fields added after initial state creation
        data.setdefault("win_streak",        0)
        data.setdefault("loss_streak",       0)
        data.setdefault("last_trade_result", None)   # "WIN" | "LOSS" | None
        data.setdefault("last_context_run",  "")     # date of last Historical/Geo context run
        return data
    return {
        "paper_day":         0,
        "start_date":        datetime.now().strftime("%Y-%m-%d"),
        "entry_dates":       {},   # {ticker: "YYYY-MM-DD"}
        "buy_log":           [],
        "sell_log":          [],
        "win_streak":        0,
        "loss_streak":       0,
        "last_trade_result": None,
        "last_context_run":  "",
    }


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Main Daily Run
# ---------------------------------------------------------------------------

def _load_canary_alert() -> dict:
    """Check if the Canary Agent has an active RED or ORANGE alert."""
    alert_path = Path(__file__).parent / "data" / "canary_alert.json"
    if not alert_path.exists():
        return {}
    try:
        with open(alert_path) as f:
            alert = json.load(f)
        if alert.get("level") in ("RED", "ORANGE"):
            return alert
    except Exception:
        pass
    return {}


def run_daily(send_email: bool = True, include_backtest: bool = False):
    """Execute the full daily trading pipeline."""
    logger.info("=" * 65)
    logger.info("  INSTITUTIONAL TRACKER — DAILY RUN")
    logger.info(f"  {datetime.now().strftime('%A, %B %d, %Y  %H:%M:%S')}")
    logger.info("=" * 65)

    # ── Morning Briefing — shared context for all agents ─────────────────
    # The morning briefing runs 30 min before the pipeline (9:15 AM EDT).
    # Every agent reads this file so they all start with identical shared context:
    # market status, risk level, open positions, top R&D picks, CoS items, IRM flags.
    _morning_briefing: dict = {}
    try:
        _mb_path = Path(__file__).parent / "data" / "morning_briefing.json"
        if _mb_path.exists():
            with open(_mb_path) as _mb_f:
                _morning_briefing = json.load(_mb_f)
            _mb_age = (
                datetime.now() -
                datetime.fromisoformat(str(_morning_briefing.get("generated_at", ""))[:19])
            ).total_seconds() / 3600
            if _mb_age < 4:
                logger.info(
                    f"  📋 Morning briefing loaded ({_mb_age:.1f}h old) | "
                    f"Market: {_morning_briefing.get('sections', {}).get('market', {}).get('status', '?')} | "
                    f"Risk: {_morning_briefing.get('sections', {}).get('risk', {}).get('risk_level', '?')} | "
                    f"Alerts: {len(_morning_briefing.get('alerts', []))}"
                )
                if _morning_briefing.get("alerts"):
                    for _alert in _morning_briefing["alerts"][:5]:
                        logger.info(f"    {_alert}")
            else:
                logger.info(f"  ⚠ Morning briefing is {_mb_age:.1f}h old — context may be stale")
        else:
            logger.info("  Morning briefing not found — running without shared context")
    except Exception as _mb_err:
        logger.debug(f"  Morning briefing load error (non-fatal): {_mb_err}")

    # ── Dead-man's switch ─────────────────────────────────────────────────
    # If manager_decisions.json is stale (>30h) when the pipeline starts,
    # the Manager Agent missed an entire cycle — send an immediate warning email.
    # This is a pre-flight check, not a pipeline abort: we continue the run but
    # the Chairman knows before any money moves.
    try:
        _dm_path = Path(__file__).parent / "data" / "manager_decisions.json"
        if _dm_path.exists():
            import json as _dm_json
            with open(_dm_path) as _dm_f:
                _dm_data = _dm_json.load(_dm_f)
            _dm_raw = _dm_data[-1] if isinstance(_dm_data, list) else _dm_data
            _dm_ts  = _dm_raw.get("generated_at") or _dm_raw.get("timestamp", "")
            if _dm_ts:
                _dm_age_h = (datetime.now() - datetime.fromisoformat(str(_dm_ts)[:19])).total_seconds() / 3600
                if _dm_age_h > 30:
                    logger.warning(
                        f"  🚨 DEAD-MAN'S SWITCH: manager_decisions.json is {_dm_age_h:.0f}h old — "
                        f"Manager Agent missed a cycle! Sending alert..."
                    )
                    if send_email:
                        try:
                            from email_sender import send_alert_email
                            send_alert_email(
                                subject=f"⚠️ DEAD-MAN'S SWITCH — Manager Agent missed a cycle ({_dm_age_h:.0f}h stale)",
                                body=(
                                    f"<p><strong>ALERT:</strong> The pipeline's dead-man's switch has triggered.</p>"
                                    f"<p><code>data/manager_decisions.json</code> is <strong>{_dm_age_h:.0f} hours old</strong> "
                                    f"(threshold: 30h). The Manager Agent did not complete its last cycle.</p>"
                                    f"<p>Possible causes: pipeline crash, scheduler failure, or Alpaca connectivity issue.</p>"
                                    f"<p>Pipeline is continuing this cycle. Please investigate.</p>"
                                ),
                            )
                        except Exception as _dm_e:
                            logger.warning(f"  Dead-man's switch email failed: {_dm_e}")
    except Exception as _dm_err:
        logger.debug(f"  Dead-man's switch check error: {_dm_err}")

    # ── Board meeting trigger state ────────────────────────────────────────
    # Tracks whether anything in this cycle warrants an automatic board meeting.
    # At most one board meeting fires per daily run (Step 11).
    _board_meeting_needed  = False
    _board_trigger_sources: list[str] = []   # which agents triggered it

    # ── Step 0: Stale-exit enforcement ────────────────────────────────────
    # Board directive 2026-05-01: any SELL recommendation that survived
    # more than one full pipeline cycle without being executed is a policy
    # breach.  This step runs FIRST, before anything else, and force-closes
    # any position flagged SELL by the review agent that is still open.
    # It is the daily equivalent of canary_agent._enforce_canary_exits().
    # _step0_closed_tickers: passed to Step 2's enforce_risk_rules so it
    # skips positions already closed here — prevents duplicate sell orders
    # and double entries in sell_log.
    _step0_closed_tickers: set = set()

    logger.info("Step 0: Stale-exit enforcement — checking for unexecuted SELL signals...")
    try:
        _rev_path = Path(__file__).parent / "data" / "review_recommendations.json"
        if _rev_path.exists():
            import json as _json_stale
            with open(_rev_path) as _rf:
                _rev_recs = _json_stale.load(_rf)
            _recs_list = _rev_recs if isinstance(_rev_recs, list) else _rev_recs.get("recommendations", [])
            _stale_sells = [
                r for r in _recs_list
                if r.get("action") == "SELL"
                and (r.get("unrealized_pnl_pct", 0) or 0) < -7.0   # stop-loss breach
                or (
                    r.get("action") == "SELL"
                    and (r.get("unrealized_pnl_pct", 0) or 0) > 20.0  # take-profit overdue
                )
            ]
            if _stale_sells:
                from alpaca_trader import AlpacaTrader as _AT
                from alpaca.trading.requests import MarketOrderRequest as _MOR
                from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                _trader_stale = _AT()
                _open_pos = {p.symbol: p for p in _trader_stale.client.get_all_positions()}
                for _sr in _stale_sells:
                    _tk = _sr.get("ticker", "")
                    _ap = _open_pos.get(_tk)
                    if not _ap:
                        continue
                    _qty = int(float(_ap.qty))
                    _side = _OS.SELL if _qty > 0 else _OS.BUY
                    try:
                        _req = _MOR(symbol=_tk, qty=abs(_qty), side=_side, time_in_force=_TIF.DAY)
                        _trader_stale.client.submit_order(_req)
                        _step0_closed_tickers.add(_tk)   # ← track for Step 2 dedup
                        logger.warning(
                            f"  ⚡ STALE EXIT ENFORCED: {_tk} "
                            f"pnl={_sr.get('unrealized_pnl_pct',0):+.1f}% — "
                            f"SELL signal was unexecuted; closed now."
                        )
                    except Exception as _oe:
                        logger.warning(f"  Stale-exit order failed for {_tk}: {_oe}")
                        # Intentionally NOT adding to _step0_closed_tickers on failure:
                        # Step 2 (enforce_risk_rules) will attempt the same sell again.
                        # This is the desired behaviour — the position still needs closing.
                        # If Step 2 also fails, the position remains open until next cycle.
            else:
                logger.info("  ✓ No stale SELL signals detected.")
    except Exception as _e:
        logger.warning(f"  Stale-exit check error: {_e}")

    # ── Step 0b: SystemPerformanceAnalyst — runs every cycle ─────────────
    # The SysPerf analyst is part of the R&D team but its job is monitoring,
    # not research — it must run EVERY cycle, not just every 4 days.
    # We run it here (before any trading) so its findings are fresh for HR
    # (Step 10) and the board (Step 12) if one fires today.
    logger.info("Step 0b: SystemPerformanceAnalyst — daily system health audit...")
    try:
        from rd_system_performance import SystemPerformanceAnalyst as _SPA
        _spa_result = _SPA().analyse(quiet=True)
        _spa_verdict = _spa_result.get("verdict", "?")
        _spa_score   = _spa_result.get("health_score", "?")
        _spa_crit    = _spa_result.get("critical_count", 0)
        logger.info(
            f"  SystemPerformanceAnalyst: {_spa_verdict} | "
            f"score={_spa_score}/10 | critical={_spa_crit}"
        )
        if _spa_crit > 0:
            logger.warning(
                f"  ⚠️  {_spa_crit} CRITICAL system finding(s) — "
                f"see data/rd_system_performance.json"
            )
    except Exception as _e:
        logger.warning(f"  SystemPerformanceAnalyst error: {_e}")

    # ── Canary alert check — defensive mode if active ──────────────────
    canary_alert = _load_canary_alert()
    if canary_alert:
        logger.warning(f"\n  ⚠️  CANARY {canary_alert.get('level')} ALERT ACTIVE")
        logger.warning(f"  Triggers: {', '.join(canary_alert.get('triggers', []))}")
        logger.warning(f"  Manager Agent will run in DEFENSIVE MODE\n")

    state      = load_state()
    paper_day  = state["paper_day"] + 1
    state["paper_day"] = paper_day

    # Determine if full R&D cycle should run today (every 3 days)
    # 3-day cadence eliminates the 1-day "dark window" that existed with 4-day cadence
    # when research TTL is 72h (previously picks could expire before the next run).
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_research = state.get("last_research_run", "")
    run_research = False
    if not last_research:
        run_research = True
    else:
        try:
            from datetime import date
            delta = (date.fromisoformat(today_str) - date.fromisoformat(last_research)).days
            run_research = delta >= 3   # changed from 4 → 3 to eliminate research dark window
        except Exception:
            run_research = True

    # Determine if context agents (Historical + Geopolitical) should run today (every 2 days)
    # These run on a tighter 2-day sub-cycle — independent of the full R&D cycle.
    last_context = state.get("last_context_run", "")
    run_context_agents = False
    if not last_context:
        run_context_agents = True
    else:
        try:
            from datetime import date as _date_ctx
            ctx_delta = (_date_ctx.fromisoformat(today_str) - _date_ctx.fromisoformat(last_context)).days
            run_context_agents = ctx_delta >= 2
        except Exception:
            run_context_agents = True

    # Determine if trade review team should run today (every 7 days)
    last_trade_review = state.get("last_trade_review_run", "")
    run_trade_review = False
    if not last_trade_review:
        run_trade_review = True
    else:
        try:
            from datetime import date as _date
            tr_delta = (_date.fromisoformat(today_str) - _date.fromisoformat(last_trade_review)).days
            run_trade_review = tr_delta >= 7
        except Exception:
            run_trade_review = True

    logger.info(f"\n📅 PAPER TRADING DAY {paper_day} / 30\n")

    # -----------------------------------------------------------------------
    # Step 1: Initialize Alpaca Trader
    # -----------------------------------------------------------------------
    logger.info("Step 1: Connecting to Alpaca paper trading...")
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        logger.info("  ✅ Alpaca connection established")
    except Exception as e:
        logger.error(f"  ❌ Alpaca connection failed: {e}")
        logger.error("  Check your API keys in config.py")
        return

    # -----------------------------------------------------------------------
    # Step 2: Enforce Risk Rules (stop-loss / take-profit / max hold)
    # -----------------------------------------------------------------------
    logger.info("\nStep 2: Enforcing risk rules on open positions...")
    entry_dates = {
        ticker: datetime.strptime(date_str, "%Y-%m-%d")
        for ticker, date_str in state["entry_dates"].items()
    }
    sells_executed = trader.enforce_risk_rules(
        entry_dates,
        exclude_tickers=_step0_closed_tickers,   # skip already-closed positions
    )

    for sell in sells_executed:
        logger.info(f"  SOLD {sell['ticker']} — reason: {sell.get('reason','')}, "
                    f"P&L: {sell.get('pnl_pct', 0):+.1f}%")
        state["sell_log"].append({
            "date":   datetime.now().strftime("%Y-%m-%d"),
            "ticker": sell["ticker"],
            "reason": sell.get("reason", ""),
            "pnl_pct": sell.get("pnl_pct", 0),
        })
        if sell["ticker"] in state["entry_dates"]:
            del state["entry_dates"][sell["ticker"]]

    # -----------------------------------------------------------------------
    # Step 2b: Manage Regime Hedge (SH inverse ETF in bear market)
    # -----------------------------------------------------------------------
    logger.info("\nStep 2b: Managing regime hedge...")
    hedge_result = trader.manage_regime_hedge()
    logger.info(f"  Regime: {hedge_result['regime']}")
    logger.info(f"  Hedge action: {hedge_result['action']}")

    # -----------------------------------------------------------------------
    # Step 2c: Manage Momentum Short Book
    # -----------------------------------------------------------------------
    logger.info("\nStep 2c: Managing momentum short positions...")
    short_actions = trader.manage_momentum_shorts()
    if short_actions:
        for a in short_actions:
            logger.info(f"  {a['action'].upper()}: {a['ticker']}")
    else:
        logger.info("  No short position changes today")

    # -----------------------------------------------------------------------
    # Step 2d: Chief Risk Officer — run BEFORE PDO so capital-deployment
    # decisions are grounded in today's fresh risk bulletin.
    # The CRO publishes data/risk_bulletin.json; PDO reads it at Step 3b.
    # -----------------------------------------------------------------------
    logger.info("\nStep 2d: Running Chief Risk Officer — generating fresh risk bulletin...")
    try:
        from risk_manager_agent import run_risk_manager
        run_risk_manager(send_email=send_email)
        logger.info("  ✅ CRO bulletin published → data/risk_bulletin.json")
    except Exception as e:
        logger.warning(f"  CRO error: {e}")

    # ── CRO board meeting trigger ──────────────────────────────────────────
    # If the CRO published a HIGH or CRITICAL bulletin, flag this cycle
    # for an automatic board meeting (Step 11).
    try:
        _cro_trigger_path = Path(__file__).parent / "data" / "risk_bulletin.json"
        if _cro_trigger_path.exists():
            with open(_cro_trigger_path) as _ctf:
                _cro_trigger = json.load(_ctf)
            _cro_trigger_level = _cro_trigger.get("risk_level", "UNKNOWN")
            if _cro_trigger_level in ("HIGH", "CRITICAL"):
                _board_meeting_needed = True
                _board_trigger_sources.append(f"CRO:{_cro_trigger_level}")
                logger.info(
                    f"  🔔 Board meeting flagged — CRO risk level: {_cro_trigger_level}"
                )
    except Exception as _e:
        logger.debug(f"  CRO board trigger check failed: {_e}")

    # -----------------------------------------------------------------------
    # Step 2d-b: Proactive Tail Hedge — board directive 2026-04-27
    # When CRO risk is HIGH or CRITICAL, buy a 5% SH allocation even if SPY
    # is still above its 200MA.  Runs after manage_regime_hedge() (Step 2b)
    # so the two hedge mechanisms don't double-up.
    # -----------------------------------------------------------------------
    try:
        rb_tail_path = Path(__file__).parent / "data" / "risk_bulletin.json"
        if rb_tail_path.exists():
            with open(rb_tail_path) as _rbt_f:
                _rbt = json.load(_rbt_f)
            _cro_level_for_hedge = _rbt.get("risk_level", "UNKNOWN")
            tail_result = trader.manage_tail_hedge(_cro_level_for_hedge)
            if tail_result["action"] not in ("none", "hedge_active"):
                logger.info(f"  Tail hedge: {tail_result['action']} | CRO risk: {_cro_level_for_hedge}")
    except Exception as e:
        logger.warning(f"  Tail hedge error: {e}")

    # -----------------------------------------------------------------------
    # CVaR Circuit Breaker — computed once here so it can be passed to BOTH
    # the 13F signal path (Step 4) and the Manager Agent (Step 4c).
    # Reads from the bulletin just published by the CRO above.
    # -----------------------------------------------------------------------
    cvar_circuit_breaker_active = False
    try:
        rb_path = Path(__file__).parent / "data" / "risk_bulletin.json"
        if rb_path.exists():
            with open(rb_path) as _rb_f:
                _rb = json.load(_rb_f)
            _acct_tmp  = trader.get_account()
            _cvar_1d   = _rb.get("cvar_95", 0) or 0
            _pv_tmp    = _acct_tmp.get("portfolio_value", 1) or 1
            _cvar_pct  = (_cvar_1d / _pv_tmp * 100)
            _CVAR_CB_THRESH = 5.0
            if _cvar_pct > _CVAR_CB_THRESH:
                cvar_circuit_breaker_active = True
                logger.warning(
                    f"  ⚠️  CVaR CIRCUIT BREAKER ACTIVE — "
                    f"1-day CVaR = {_cvar_pct:.1f}% of portfolio "
                    f"(threshold: {_CVAR_CB_THRESH:.0f}%). All BUY/ADD orders blocked."
                )
    except Exception as e:
        logger.warning(f"  CVaR circuit breaker check error: {e}")

    # -----------------------------------------------------------------------
    # Step 2e: Portfolio Deployment Officer — capital efficiency check
    # -----------------------------------------------------------------------
    # MOVED from Step 3b: PDO depends on the CRO bulletin (just published) and
    # its signal must be FRESH before 13F trades are evaluated below.
    # Previously PDO ran AFTER 13F execution, meaning 13F trades used yesterday's
    # PDO signal. Now PDO runs here so all trades this cycle use today's signal.
    # -----------------------------------------------------------------------
    logger.info("\nStep 2e: Portfolio Deployment Officer — capital efficiency check...")
    pdo_report = None
    try:
        from capital_efficiency_agent import run_pdo
        pdo_report = run_pdo(quiet=True)
        logger.info(
            f"  PDO: {pdo_report['recommendation']} | "
            f"Deployed: {pdo_report['deployment']['current_pct']:.1f}% | "
            f"Target: {pdo_report['deployment']['target_pct']:.1f}% | "
            f"Regime: {pdo_report['market_conditions']['regime'].upper()}"
        )
        for directive in pdo_report.get("manager_directives", []):
            logger.info(f"  PDO directive → {directive}")
    except Exception as e:
        logger.warning(f"  PDO error: {e}")

    # -----------------------------------------------------------------------
    # Step 3: Scan for New Signals (13F block purchases)
    # -----------------------------------------------------------------------
    logger.info("\nStep 3: Scanning SEC EDGAR for new institutional signals...")
    try:
        from signal_engine import run_signal_scan
        signals_df = run_signal_scan(verbose=False)

        if signals_df.empty:
            logger.info("  No new signals today "
                        "(13F filings are quarterly — signals appear ~45 days after quarter end)")
        else:
            logger.info(f"  {len(signals_df)} actionable signal(s) found:")
            for _, row in signals_df.iterrows():
                logger.info(f"    [{row['conviction_score']:.0f}⭐] {row['ticker']} — "
                            f"{row['institutions']}"
                            + (" [HIGH CONVICTION]" if row.get("high_conviction") else ""))

    except Exception as e:
        logger.warning(f"  Signal scan error: {e}")
        signals_df = __import__("pandas").DataFrame()

    # -----------------------------------------------------------------------
    # Step 4: Execute Buy Orders for 13F Signals — via full risk pipeline
    # -----------------------------------------------------------------------
    # CRITICAL FIX: 13F signals previously bypassed ALL risk controls by
    # calling trader.submit_buy() directly.  They now go through
    # evaluate_recommendation() so the canary alert, PDO signal, CRO ceiling,
    # and CVaR circuit breaker are all respected.
    # -----------------------------------------------------------------------
    logger.info("\nStep 4: Evaluating & executing 13F signals through risk pipeline...")
    buys_today = []

    if not signals_df.empty:
        from manager_agent import evaluate_recommendation as _eval_rec, execute_decision as _exec_dec, _load_pdo_signal as _load_pdo
        _pdo_signal_13f = _load_pdo()
        _acct_13f = trader.get_account()
        _snap_13f = trader.get_portfolio_snapshot()
        _acct_snap_13f = {
            "portfolio_value": _acct_13f["portfolio_value"],
            "cash":            _acct_13f["cash"],
            "buying_power":    _acct_13f["buying_power"],
            "pnl_today":       _acct_13f.get("pnl_today", 0),
            "pnl_today_pct":   _acct_13f.get("pnl_today_pct", 0),
            "n_positions":     _snap_13f["n_positions"],
        }

        for _, sig in signals_df.iterrows():
            ticker     = sig["ticker"]
            conviction = sig.get("conviction_score", 1.0)

            rec_13f = {
                "ticker":             ticker,
                "action":             "BUY_NEW",
                "side":               "long",
                "priority":           2 if conviction >= 4 else 3,
                "reasons":            [
                    f"13F institutional block-purchase — {sig.get('institutions', 'multiple institutions')} "
                    f"| conviction {conviction:.0f}★"
                    + (" [HIGH CONVICTION]" if sig.get("high_conviction") else "")
                ],
                "weight_pct":         0.0,
                "unrealized_pnl_pct": 0.0,
                "hold_days":          0,
                "conviction":         conviction,
                "source_agent":       "SignalEngine_13F",
            }

            decision_13f = _eval_rec(
                rec_13f,
                _acct_snap_13f,
                pdo_signal=_pdo_signal_13f,
                canary_alert=canary_alert or None,
                cvar_circuit_breaker=cvar_circuit_breaker_active,
                streak_context={
                    "win_streak":        state.get("win_streak", 0),
                    "loss_streak":       state.get("loss_streak", 0),
                    "last_trade_result": state.get("last_trade_result"),
                },
            )

            if decision_13f["manager_decision"] == "APPROVED":
                result = _exec_dec(decision_13f, trader, dry_run=False)
                if result.get("executed"):
                    buys_today.append(result)
                    state["entry_dates"][ticker] = datetime.now().strftime("%Y-%m-%d")
                    state["buy_log"].append({
                        "date":       today_str,
                        "ticker":     ticker,
                        "conviction": conviction,
                        "source":     "13F",
                    })
                    logger.info(f"  ✅ 13F BUY approved & executed: {ticker} ({conviction:.0f}★)")
            else:
                rationale = (decision_13f.get("manager_rationale") or [""])[0]
                logger.info(
                    f"  ✗  13F signal REJECTED for {ticker}: "
                    f"{rationale[:80]}"
                )
    else:
        logger.info("  No new 13F signals today")

    # -----------------------------------------------------------------------
    # Step 3a: Historical Trade Review Team — weekly performance post-mortem
    # -----------------------------------------------------------------------
    if run_trade_review:
        logger.info("\nStep 3a: Trade Review Team — weekly historical analysis...")
        try:
            from trade_review_team import run_trade_review as _run_tr
            trade_review_rpt = _run_tr(send_email=send_email)
            state["last_trade_review_run"] = today_str
            a = trade_review_rpt.get("analytics", {})
            logger.info(
                f"  Trade review complete — {a.get('total_trades',0)} trades | "
                f"Win rate: {a.get('win_rate','N/A')}% | "
                f"vs SPY: {a.get('benchmark_delta',0):+.1f}pp"
            )
        except Exception as e:
            logger.warning(f"  Trade Review Team error: {e}")
    else:
        logger.info(
            f"\nStep 3a: Trade Review Team — skipped "
            f"(last run: {last_trade_review}, runs every 7 days)"
        )

    # -----------------------------------------------------------------------
    # Step 3b: R&D Team — runs every 4 days
    #   Phase A: R&D Team Leader scans sectors + assigns work
    #   Phase B: Research agents run their sector analysis
    #   Phase C: R&D Team Leader compiles + emails Chairman
    # -----------------------------------------------------------------------
    research_picks = []
    if run_research:
        logger.info("\nStep 3c: R&D Team — sector scan + research cycle (every 3 days)...")

        # Phase A — Leader scans sectors and assigns to agents
        try:
            from rd_team_leader import RDTeamLeader
            leader      = RDTeamLeader()
            assignments = leader.scan_and_assign()
            state["last_research_run"] = today_str
            logger.info(f"  R&D Leader assigned {len(assignments)} sector(s) to research agents")
        except Exception as e:
            logger.warning(f"  R&D Leader (scan phase) error: {e}")
            assignments = []

        # Phase B — Research agents execute their assignments
        if assignments:
            try:
                from research_team import ResearchTeamCoordinator
                coordinator  = ResearchTeamCoordinator()
                research_rpt = coordinator.run(assignments=assignments, quiet=True)
                research_picks = research_rpt.get("recommendations", [])
                s = research_rpt.get("summary", {})
                logger.info(
                    f"  Research complete: {s.get('agents_run',0)} agents, "
                    f"{s.get('total_picks',0)} picks, "
                    f"{s.get('high_conviction',0)} high-conviction"
                )
            except Exception as e:
                logger.warning(f"  Research Team error: {e}")

        # Phase C — Leader compiles findings and emails Chairman
        try:
            import json as _json
            asgn_path = Path(__file__).parent / "data" / "sector_assignments.json"
            rpt_path  = Path(__file__).parent / "data" / "research_team_report.json"
            if asgn_path.exists() and rpt_path.exists():
                with open(asgn_path) as f:
                    asgn_payload  = _json.load(f)
                with open(rpt_path) as f:
                    research_full = _json.load(f)
                text_brief = leader.compile_briefing(research_full, asgn_payload)
                html_brief = leader.compile_html_briefing(research_full, asgn_payload)
                logger.info("  R&D Leader compiled Chairman briefing")
                if send_email:
                    leader.email_chairman(text_brief, html_brief)
        except Exception as e:
            logger.warning(f"  R&D Leader (compile phase) error: {e}")

        # Phase D — Energy Research Team (runs on every R&D cycle alongside main research)
        # The Energy Research Team covers 8 energy sub-sectors not covered by the main R&D agents.
        # Its picks are merged into research_picks and fed to the Manager Agent.
        # Staleness guard: same 72h TTL as main research — energy picks expire too.
        logger.info("  Phase D: Energy Research Team — 8 energy sub-sector agents...")
        try:
            from energy_research_team import EnergyResearchCoordinator
            energy_rpt   = EnergyResearchCoordinator().run(quiet=True)
            energy_picks = energy_rpt.get("recommendations", [])
            # Verify the freshly-written report is within TTL (sanity check)
            _e_gen_at = energy_rpt.get("generated_at", "")
            if _e_gen_at:
                from datetime import datetime as _edt
                _e_age_h = (_edt.now() - _edt.fromisoformat(str(_e_gen_at)[:19])).total_seconds() / 3600
                if _e_age_h > _RESEARCH_TTL_HOURS:
                    logger.warning(f"  Energy report already stale ({_e_age_h:.0f}h) — discarding picks")
                    energy_picks = []
            _seen_energy = {r["ticker"] for r in research_picks}
            _added = 0
            for pick in energy_picks:
                if pick["ticker"] not in _seen_energy:
                    research_picks.append(pick)
                    _seen_energy.add(pick["ticker"])
                    _added += 1
            es = energy_rpt.get("summary", {})
            logger.info(
                f"  Energy Research: {es.get('agents_run',0)} agents | "
                f"{es.get('total_picks',0)} picks | {_added} new (not in main R&D) "
                f"merged into research pipeline"
            )
        except Exception as e:
            logger.warning(f"  Energy Research Team error: {e}")

    else:
        logger.info(
            f"\nStep 3c: R&D Team — skipped "
            f"(last run: {last_research}, runs every 3 days)"
        )
        # Load saved picks from last research cycle for the Manager Agent.
        # STALENESS GUARD: picks older than 48 hours are discarded — thesis
        # conditions may have changed materially since the R&D cycle ran.
        import json as _json
        from datetime import datetime as _dt2
        _RESEARCH_TTL_HOURS = 72   # covers every day (R&D runs every 3 days, TTL = 72h = 3 days)

        # Also load and TTL-check saved energy research picks on non-R&D days
        _energy_rpt_path = Path(__file__).parent / "data" / "energy_research_report.json"
        if _energy_rpt_path.exists():
            try:
                with open(_energy_rpt_path) as _ef:
                    _energy_saved = _json.load(_ef)
                _e_gen = _energy_saved.get("generated_at", "")
                _e_age_h = _RESEARCH_TTL_HOURS + 1
                if _e_gen:
                    try:
                        _e_age_h = (_dt2.now() - _dt2.fromisoformat(_e_gen)).total_seconds() / 3600
                    except Exception:
                        pass
                if _e_age_h > _RESEARCH_TTL_HOURS:
                    logger.info(
                        f"  Energy picks EXPIRED ({_e_age_h:.0f}h old) — discarding"
                    )
                else:
                    _e_picks = _energy_saved.get("recommendations", [])
                    _seen_e  = {r["ticker"] for r in research_picks}
                    _e_added = sum(
                        1 for p in _e_picks
                        if p["ticker"] not in _seen_e
                        and not _seen_e.add(p["ticker"])  # type: ignore
                    )
                    research_picks += [p for p in _e_picks if p["ticker"] in _seen_e and p not in research_picks]
                    # cleaner dedup
                    _all_tickers_seen: set = {r["ticker"] for r in research_picks}
                    for _ep in _e_picks:
                        if _ep["ticker"] not in _all_tickers_seen:
                            research_picks.append(_ep)
                            _all_tickers_seen.add(_ep["ticker"])
                    logger.info(
                        f"  Loaded saved energy picks ({_e_age_h:.0f}h old, "
                        f"TTL={_RESEARCH_TTL_HOURS}h) — merged into research pipeline"
                    )
            except Exception:
                pass
        rpt_path = Path(__file__).parent / "data" / "research_team_report.json"
        if rpt_path.exists():
            try:
                with open(rpt_path) as f:
                    _rpt_data = _json.load(f)
                _gen_at = _rpt_data.get("generated_at", "")
                _age_h  = _RESEARCH_TTL_HOURS + 1   # default: treat as expired
                if _gen_at:
                    try:
                        _age_h = (_dt2.now() - _dt2.fromisoformat(_gen_at)).total_seconds() / 3600
                    except Exception:
                        pass
                if _age_h > _RESEARCH_TTL_HOURS:
                    logger.warning(
                        f"  Research picks EXPIRED ({_age_h:.0f}h old, TTL={_RESEARCH_TTL_HOURS}h) "
                        f"— discarding to prevent stale thesis trades"
                    )
                    research_picks = []
                else:
                    research_picks = _rpt_data.get("recommendations", [])
                    logger.info(
                        f"  Loaded {len(research_picks)} saved research pick(s) "
                        f"({_age_h:.0f}h old, TTL={_RESEARCH_TTL_HOURS}h)"
                    )
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Step 3d: Context Agents — Historical + Geopolitical (every 2 days)
    # -----------------------------------------------------------------------
    # These run on a tighter sub-cycle than the full R&D cycle.  Their job is
    # to give the Manager Agent and Board fresh macro/geopolitical framing even
    # on days when the full research cycle doesn't run.  Output is saved to
    # data/rd_historical_context.json and data/rd_geopolitical_context.json.
    # -----------------------------------------------------------------------
    if run_context_agents:
        logger.info("\nStep 3d: Context agents — Historical & Geopolitical (every 2 days)...")

        # Build real context inputs so agents produce portfolio-specific analysis:
        # - sectors: from current R&D assignments or held positions
        # - regime: from PDO report (already run at Step 2e)
        _ctx_sectors: list = []
        try:
            import json as _ctx_json
            _asgn_path = Path(__file__).parent / "data" / "sector_assignments.json"
            if _asgn_path.exists():
                with open(_asgn_path) as _af:
                    _asgn_data = _ctx_json.load(_af)
                _ctx_sectors = [
                    a.get("sector_name", a.get("sector", ""))
                    for a in (_asgn_data if isinstance(_asgn_data, list) else [])
                    if a.get("sector_name") or a.get("sector")
                ]
        except Exception:
            pass
        # Fall back to entry_dates tickers if no sector assignments exist
        if not _ctx_sectors:
            _ctx_sectors = list(state.get("entry_dates", {}).keys())

        _ctx_regime: dict = {}
        if pdo_report:
            _ctx_regime = {
                "regime":     pdo_report.get("market_conditions", {}).get("regime", "unknown"),
                "pdo_signal": pdo_report.get("recommendation", ""),
            }
        try:
            import json as _rb_ctx_json
            _rb_ctx_path = Path(__file__).parent / "data" / "risk_bulletin.json"
            if _rb_ctx_path.exists():
                with open(_rb_ctx_path) as _rbf:
                    _rb_ctx = _rb_ctx_json.load(_rbf)
                _ctx_regime["risk_level"] = _rb_ctx.get("risk_level", "UNKNOWN")
                _ctx_regime["vix"]        = _rb_ctx.get("vix", None)
        except Exception:
            pass

        try:
            from rd_historical_analyst import HistoricalContextAnalyst
            _hca_result = HistoricalContextAnalyst().analyze(
                assigned_sectors=_ctx_sectors, regime=_ctx_regime
            )
            state["last_context_run"] = today_str
            logger.info(
                f"  Historical Context: {len(_hca_result.get('bull_analogues', []))} bull "
                f"+ {len(_hca_result.get('bear_analogues', []))} bear analogues "
                f"(sectors: {len(_ctx_sectors)}, regime: {_ctx_regime.get('regime','?')}) ✓"
            )
        except Exception as e:
            logger.warning(f"  Historical Context Analyst error: {e}")

        # GeopoliticalResearchTeam.analyze() takes assignments (list of dicts) not sector strings
        _ctx_assignments = [{"sector_name": s, "sector": s} for s in _ctx_sectors]
        try:
            from rd_geopolitical_team import GeopoliticalResearchTeam
            _geo_result = GeopoliticalResearchTeam().analyze(
                assignments=_ctx_assignments, regime=_ctx_regime
            )
            logger.info(
                f"  Geopolitical Team: risk score {_geo_result.get('combined_risk_score', '?')} "
                f"| {len(_geo_result.get('high_risk_sectors', []))} high-risk sector(s) ✓"
            )
        except Exception as e:
            logger.warning(f"  Geopolitical Research Team error: {e}")
    else:
        logger.info(
            f"\nStep 3d: Context agents — skipped "
            f"(last run: {last_context}, runs every 2 days)"
        )

    # -----------------------------------------------------------------------
    # Step 4b: Review Agent — Analyze all open positions
    # -----------------------------------------------------------------------
    logger.info("\nStep 4b: Running Review Agent — portfolio analysis...")
    review_report = None
    try:
        from review_agent import run_review
        entry_dates_dt = {
            ticker: datetime.strptime(date_str, "%Y-%m-%d")
            for ticker, date_str in state["entry_dates"].items()
        }
        review_report = run_review(entry_dates=entry_dates_dt, quiet=True)
        summary = review_report.get("summary", {})
        logger.info(
            f"  Review complete — "
            f"SELL:{summary.get('SELL',0)}  TRIM:{summary.get('TRIM',0)}  "
            f"ADD:{summary.get('ADD',0)}  BUY_NEW:{summary.get('BUY_NEW',0)}  "
            f"WATCH:{summary.get('WATCH',0)}  HOLD:{summary.get('HOLD',0)}"
        )
    except Exception as e:
        logger.warning(f"  Review Agent error: {e}")

    # -----------------------------------------------------------------------
    # Step 4c: Manager Agent — Evaluate recommendations, execute, report
    # -----------------------------------------------------------------------
    # Re-check canary immediately before execution — the canary monitors 24/7
    # and could have fired between the pipeline start (Step 0) and now.
    # If it escalated during the pipeline run, we catch it here before any
    # money moves.
    _canary_refreshed = _load_canary_alert()
    if _canary_refreshed and _canary_refreshed.get("level") != canary_alert.get("level"):
        logger.warning(
            f"  ⚠️  CANARY LEVEL CHANGED during pipeline run: "
            f"{canary_alert.get('level','NONE')} → {_canary_refreshed.get('level')} "
            f"| Updating defensive mode before Manager Agent executes."
        )
        canary_alert = _canary_refreshed
    elif _canary_refreshed and not canary_alert:
        logger.warning(
            f"  ⚠️  CANARY ALERT ACTIVATED mid-pipeline: "
            f"{_canary_refreshed.get('level')} | "
            f"Manager Agent will run in DEFENSIVE MODE."
        )
        canary_alert = _canary_refreshed
    logger.info("\nStep 4c: Running Manager Agent — decisions & execution...")
    manager_decisions = []
    try:
        from manager_agent import evaluate_recommendation, execute_decision, build_management_report, _load_pdo_signal
        if review_report:
            acct_snap  = review_report["account"]
            pdo_signal = _load_pdo_signal()
            # Streak context — gives the Manager awareness of recent trade momentum
            _streak_ctx = {
                "win_streak":        state.get("win_streak", 0),
                "loss_streak":       state.get("loss_streak", 0),
                "last_trade_result": state.get("last_trade_result"),
            }
            # Merge review + research picks (dedup by ticker)
            all_recs       = list(review_report.get("recommendations", []))
            seen_tickers   = {r["ticker"] for r in all_recs}
            for pick in research_picks:
                if pick["ticker"] not in seen_tickers:
                    all_recs.append(pick)
                    seen_tickers.add(pick["ticker"])
            for rec in all_recs:
                decision = evaluate_recommendation(
                    rec, acct_snap,
                    pdo_signal=pdo_signal,
                    canary_alert=canary_alert or None,
                    cvar_circuit_breaker=cvar_circuit_breaker_active,
                    streak_context=_streak_ctx,
                )
                manager_decisions.append(decision)

            approved_trades = [
                d for d in manager_decisions
                if d["manager_decision"] == "APPROVED"
                and d["manager_action"] not in ("HOLD", "WATCH")
            ]
            logger.info(f"  Manager approved {len(approved_trades)} trade action(s)")

            executions = []
            for d in approved_trades:
                ex = execute_decision(d, trader, dry_run=False)
                executions.append(ex)
                if ex.get("executed"):
                    if d["manager_action"] in ("SELL", "TRIM"):
                        state["sell_log"].append({
                            "date":   datetime.now().strftime("%Y-%m-%d"),
                            "ticker": d["ticker"],
                            "reason": "manager_agent",
                            "pnl_pct": d.get("unrealized_pnl_pct", 0),
                        })
                        state["entry_dates"].pop(d["ticker"], None)
                    elif d["manager_action"] in ("ADD", "BUY_NEW"):
                        state["entry_dates"][d["ticker"]] = datetime.now().strftime("%Y-%m-%d")
                        state["buy_log"].append({
                            "date":      datetime.now().strftime("%Y-%m-%d"),
                            "ticker":    d["ticker"],
                            "conviction": 1.0,
                        })

            text_rpt = build_management_report(manager_decisions, executions, acct_snap,
                                               dry_run=False)
            logger.info("\n" + text_rpt)

            # ── Win/loss streak tracker ────────────────────────────────────
            # For each completed SELL this cycle, check P&L and update streaks.
            # A "win" = positive P&L at close; "loss" = negative.
            # Streaks are stored in state.json and visible to the Manager Agent context.
            for ex in executions:
                if ex.get("executed") and ex.get("side") == "sell":
                    _pnl = ex.get("pnl_pct", 0) or 0
                    if _pnl > 0:
                        state["win_streak"]        = state.get("win_streak", 0) + 1
                        state["loss_streak"]       = 0
                        state["last_trade_result"] = "WIN"
                    elif _pnl < 0:
                        state["loss_streak"]       = state.get("loss_streak", 0) + 1
                        state["win_streak"]        = 0
                        state["last_trade_result"] = "LOSS"
            _ws = state.get("win_streak", 0)
            _ls = state.get("loss_streak", 0)
            if _ws >= 3:
                logger.info(f"  🔥 Win streak: {_ws} consecutive profitable closes")
            if _ls >= 3:
                logger.warning(f"  🧊 Loss streak: {_ls} consecutive losing closes — review risk posture")

        else:
            logger.warning("  Skipping Manager Agent — no review report available")
    except Exception as e:
        logger.warning(f"  Manager Agent error: {e}")

    # -----------------------------------------------------------------------
    # Step 5: Get Portfolio Snapshot
    # -----------------------------------------------------------------------
    logger.info("\nStep 5: Capturing portfolio snapshot...")
    snapshot = trader.get_portfolio_snapshot()

    logger.info(f"  Portfolio Value:  ${snapshot['portfolio_value']:,.2f}")
    logger.info(f"  Cash:             ${snapshot['cash']:,.2f}")
    logger.info(f"  Today's P&L:      ${snapshot['pnl_today']:+,.2f} "
                f"({snapshot['pnl_today_pct']:+.2f}%)")
    logger.info(f"  Return vs Start:  {snapshot['return_vs_start']:+.2f}%")
    logger.info(f"  Open Positions:   {snapshot['n_positions']}")

    # ── Portfolio drawdown board trigger ──────────────────────────────────
    # If the portfolio loses more than 4% today, automatically convene the board.
    # This catches sharp intraday drops that the CRO's daily bulletin may not
    # have yet reflected (CRO runs at Step 2d, before the market's worst moves).
    _DRAWDOWN_BOARD_THRESHOLD = -4.0
    if snapshot["pnl_today_pct"] < _DRAWDOWN_BOARD_THRESHOLD:
        _board_meeting_needed = True
        _board_trigger_sources.append(
            f"DRAWDOWN:{snapshot['pnl_today_pct']:.1f}%"
        )
        logger.warning(
            f"  🚨 DRAWDOWN TRIGGER: Portfolio is {snapshot['pnl_today_pct']:.1f}% today "
            f"(threshold: {_DRAWDOWN_BOARD_THRESHOLD:.0f}%) — board meeting flagged."
        )

    # -----------------------------------------------------------------------
    # Step 6: Load Backtest Results (for first email or if requested)
    # -----------------------------------------------------------------------
    backtest_results = None
    if include_backtest or paper_day == 1:
        logger.info("\nStep 6: Loading backtest results for report...")
        try:
            from backtester import run_backtest
            backtest_results = run_backtest()
            logger.info("  ✅ Backtest complete")
        except Exception as e:
            logger.warning(f"  Backtest error: {e}")

    # -----------------------------------------------------------------------
    # Step 7: Generate HTML Report
    # -----------------------------------------------------------------------
    logger.info("\nStep 7: Generating HTML report...")
    try:
        from report_generator import build_html_report, save_report
        html = build_html_report(
            snapshot=snapshot,
            backtest_results=backtest_results,
            paper_day=paper_day,
        )
        filename = f"report_day_{paper_day:02d}_{datetime.now().strftime('%Y%m%d')}.html"
        report_path = save_report(html, filename)
        logger.info(f"  ✅ Report saved: {report_path}")
    except Exception as e:
        logger.error(f"  Report generation failed: {e}")
        html = f"<p>Report generation error: {e}</p>"

    # -----------------------------------------------------------------------
    # Step 8: Send Email
    # -----------------------------------------------------------------------
    if send_email:
        logger.info("\nStep 8: Sending daily email report...")
        try:
            from email_sender import send_daily_report
            from creative_agent import polish_email, metrics_for_daily_report
            subject_daily = (
                f"Apex Capital — Daily Report Day {paper_day} | "
                f"{datetime.now().strftime('%b %d, %Y')}"
            )
            polished_html = polish_email(
                html,
                subject_daily,
                "daily_report",
                metrics_for_daily_report(
                    portfolio_value=snapshot["portfolio_value"],
                    pnl_today=snapshot["pnl_today"],
                    pnl_pct=snapshot["pnl_today_pct"],
                    n_positions=snapshot["n_positions"],
                ),
            )
            success = send_daily_report(html_body=polished_html, paper_day=paper_day,
                                        sender_name="daily_report",
                                        key_metrics=metrics_for_daily_report(
                                            portfolio_value=snapshot["portfolio_value"],
                                            pnl_today=snapshot["pnl_today"],
                                            pnl_pct=snapshot["pnl_today_pct"],
                                            n_positions=snapshot["n_positions"],
                                        ))
            if success:
                logger.info("  ✅ Email sent successfully")
            else:
                logger.warning("  ⚠️  Email failed — check credentials in config.py")
        except Exception as e:
            logger.error(f"  Email error: {e}")
    else:
        logger.info("\nStep 8: Email sending skipped (--no-email flag)")

    # -----------------------------------------------------------------------
    # Step 9: Chief of Staff — daily brief + Agent Factory expansion
    # Runs last so it can read all other agents' outputs for its brief.
    # -----------------------------------------------------------------------
    logger.info("\nStep 9: Chief of Staff — daily brief + R&D expansion...")
    try:
        from chief_of_staff_agent import run_chief_of_staff
        cos_report = run_chief_of_staff(send_email=send_email)
        factory    = cos_report.get("factory_result", {})
        logger.info(
            f"  CoS brief sent | Factory: {len(factory.get('spawned', []))} new agent(s) | "
            f"Registry: {factory.get('registry_size', 0)} total"
        )
    except Exception as e:
        logger.warning(f"  Chief of Staff error: {e}")

    # -----------------------------------------------------------------------
    # Step 10: HR Team Review — evaluates all agents before IRM
    # -----------------------------------------------------------------------
    # HR runs BEFORE the IRM so IRM can bundle HR findings into his email.
    # HR never emails the Chairman directly — IRM is the relay.
    # HR also produces a sealed evaluation of the IRM himself, which the IRM
    # passes through to the Chairman as an opaque base64 annex without reading.
    # -----------------------------------------------------------------------
    logger.info("\nStep 10: HR Team Review — evaluating all agents (IRM relay mode)...")
    try:
        from hr_agent import run_hr_review
        hr_result = run_hr_review(send_email=False)   # IRM handles the email
        logger.info(
            f"  HR review complete — firm health: {hr_result.get('firm_health_score', '?')}/10 | "
            f"action required: {len(hr_result.get('action_required', []))} agent(s) | "
            f"IRM self-eval: sealed → data/hr_irm_sealed.json"
        )
    except Exception as e:
        logger.warning(f"  HR review error: {e}")

    # -----------------------------------------------------------------------
    # Step 11: IRM Daily Check — Independent of email pipeline
    # -----------------------------------------------------------------------
    # The IRM board runs here regardless of the --no-email flag.
    # IRM now also bundles the HR team report and the sealed IRM self-evaluation
    # (from Step 10) into its Chairman email. This makes the IRM the single
    # reporting channel for both independent risk oversight AND HR findings.
    # -----------------------------------------------------------------------
    logger.info("\nStep 11: IRM — independent daily board consultation (+ HR relay)...")
    try:
        from irm_review_agent import run_irm_daily
        irm_result = run_irm_daily(send_email=send_email)
        bp = irm_result.get("board_panel", {})
        _irm_flags = len(bp.get("combined_red_flags", []))
        _irm_score = bp.get("board_score", 0) or 0
        logger.info(
            f"  IRM check complete — verdict: {bp.get('board_verdict','—')[:60]} | "
            f"flags: {_irm_flags}"
        )
        # IRM board meeting trigger: ≥3 red flags or board score ≥7
        if _irm_flags >= 3 or _irm_score >= 7:
            _board_meeting_needed = True
            _board_trigger_sources.append(f"IRM:flags={_irm_flags},score={_irm_score}")
            logger.info(
                f"  🔔 Board meeting flagged — IRM: {_irm_flags} flags, score {_irm_score}"
            )
    except Exception as e:
        logger.warning(f"  IRM daily check error: {e}")

    # -----------------------------------------------------------------------
    # Step 12: Automatic Board Meeting
    # -----------------------------------------------------------------------
    # Fires when CRO publishes HIGH/CRITICAL risk OR IRM surfaces ≥3 red flags.
    # At most one meeting per daily cycle — both triggers are batched into a
    # single session. The triggering report is always anonymised before the
    # board receives it (Chairman is the only person who knows which agent
    # raised the original concern). The board deliberates, reaches consensus,
    # implements actionable trades, and emails the Chairman with full minutes.
    # -----------------------------------------------------------------------
    if _board_meeting_needed:
        _trigger_label = " + ".join(_board_trigger_sources)
        logger.info(f"\nStep 12: Automatic Board Meeting — triggered by: {_trigger_label}")
        try:
            from board_meeting import run_board_meeting
            board_result = run_board_meeting(dry_run=False, send_email=send_email)
            _bm_consensus = board_result.get("consensus", {})
            logger.info(
                f"  ✅ Board meeting complete — "
                f"{_bm_consensus.get('unanimous_count', 0)} unanimous, "
                f"{_bm_consensus.get('majority_count', 0)} majority, "
                f"{_bm_consensus.get('vetoed_count', 0)} vetoed"
            )
        except Exception as e:
            logger.warning(f"  Board meeting error: {e}")
    else:
        logger.info("\nStep 12: Board Meeting — no trigger threshold reached this cycle.")

    # -----------------------------------------------------------------------
    # Save State
    # -----------------------------------------------------------------------
    save_state(state)

    # -----------------------------------------------------------------------
    # Data Backup — rolling snapshot of data/ directory
    # -----------------------------------------------------------------------
    logger.info("\nData backup — saving daily snapshot of data/...")
    try:
        from backup_data import run_backup
        backup_result = run_backup(quiet=True)
        if backup_result.get("status") == "ok":
            logger.info(
                f"  ✅ Backup: {backup_result.get('backup_timestamp')} "
                f"({backup_result.get('file_count')} files)"
                + (f" | pruned: {backup_result['pruned']}" if backup_result.get('pruned') else "")
            )
        else:
            logger.warning(f"  Backup skipped: {backup_result.get('reason')}")
    except Exception as _be:
        logger.warning(f"  Backup error: {_be}")

    # -----------------------------------------------------------------------
    # Note: CIT (Continuous Improvement Team) runs on its own cron schedule
    # (6:00 AM ET + 6:00 PM ET) independent of the main pipeline.
    # Its proposals queue up in data/cit_pending_proposals.json and are voted
    # on at the next board meeting (Step 12 above).
    # -----------------------------------------------------------------------

    logger.info("\n" + "=" * 65)
    logger.info(f"  DAILY RUN COMPLETE — Day {paper_day}/30")
    logger.info(f"  Portfolio: ${snapshot['portfolio_value']:,.2f} "
                f"({snapshot['return_vs_start']:+.2f}% from start)")
    if paper_day == 30:
        logger.info("  🎉 30-DAY PAPER TRADING PERIOD COMPLETE!")
    logger.info("=" * 65)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Institutional Tracker — Daily Run")
    parser.add_argument("--no-email",     action="store_true", help="Skip sending email")
    parser.add_argument("--backtest",     action="store_true", help="Include backtest in report")
    parser.add_argument("--reset",        action="store_true", help="Reset paper trading state")
    args = parser.parse_args()

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            print("✅ State reset. Paper trading day counter cleared.")

    run_daily(
        send_email=not args.no_email,
        include_backtest=args.backtest,
    )
