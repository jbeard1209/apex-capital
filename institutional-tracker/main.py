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

import fcntl
import os
import sys
import json
import logging
import argparse
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Raise file-descriptor limit early — prevents "Too many open files" when
# importing manager_agent late in a heavy pipeline run (yfinance + sqlite +
# logging all hold FDs; macOS default soft limit is only 256).
# ---------------------------------------------------------------------------
try:
    import resource as _resource
    _soft, _hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    _target = min(8192, _hard)
    if _soft < _target:
        _resource.setrlimit(_resource.RLIMIT_NOFILE, (_target, _hard))
except Exception:
    pass  # Windows or permission error — best-effort only

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

# Install smart log filter — demotes known transient noise (DNS blips, yfinance
# timeouts, SH "available=0") from ERROR to WARNING so real errors stand out.
try:
    from apex_logging import setup_logging as _setup_apex_logging, flush_health_now as _flush_log_health
    _setup_apex_logging(level=logging.INFO)
    logger.debug("  Apex smart log filter installed")
except Exception as _log_filter_err:
    logger.debug(f"  Apex log filter not available: {_log_filter_err}")

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


def _classify_signal_source(source_agent: str) -> str:
    """Map source_agent name → one of '13F' | 'R&D' | 'Energy' | 'Manager'."""
    sa = (source_agent or "").lower()
    if "energy" in sa:
        return "Energy"
    if any(x in sa for x in ("rd", "r&d", "research", "sector", "momentum", "factor",
                               "optionsflow", "historical", "geopolit")):
        return "R&D"
    if "13f" in sa or "institutional" in sa:
        return "13F"
    return "Manager"


def _lookup_buy_source(state: dict, ticker: str) -> str:
    """Return the signal source recorded when this ticker was bought, or 'unknown'."""
    for entry in reversed(state.get("buy_log", [])):
        if entry.get("ticker") == ticker:
            return entry.get("source", "unknown")
    return "unknown"


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

    # ── Deputy mode check — if board meeting is active, run deputy instead ─
    try:
        from deputy_agents import activate_deputy_if_needed, DeputyManager
        if activate_deputy_if_needed("Manager Agent"):
            logger.info("  Board meeting in progress — running DeputyManager instead of full pipeline.")
            DeputyManager().run()
            logger.info("  Deputy run complete. Full pipeline deferred until board meeting ends.")
            return
    except Exception as _dep_import_e:
        logger.debug(f"  Deputy agent check error (non-fatal): {_dep_import_e}")

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

    # ── Pipeline heartbeat — write on every run ───────────────────────────
    # Lets any monitoring tool (CIT, catch_up, IRM) confirm the pipeline
    # actually started.  Distinct from manager_decisions.json, which only
    # exists once the Manager Agent finishes — this file exists the moment
    # the pipeline begins, so a crash mid-run is detectable.
    _pipeline_start_iso = datetime.now().isoformat()   # captured once; reused in completion HB
    try:
        import json as _hb_json
        _hb_path = Path(__file__).parent / "data" / "pipeline_heartbeat.json"
        _hb_path.parent.mkdir(parents=True, exist_ok=True)
        # `starts` is a rolling history, not just the latest stamp. The single
        # timestamp answers "did the pipeline run?"; only a history answers
        # "how OFTEN?" — and for six weeks the answer to the second question
        # was 12-22 times a day against a design of 4, with nothing able to
        # see it because every detector in the firm looks for absence.
        # coherence_laws.law_runs_match_schedule reads this against the live
        # crontab. Bounded at 400 so a runaway cannot grow the file without limit.
        try:
            _hb_prev = _hb_json.loads(_hb_path.read_text())
            _hb_starts = _hb_prev.get("starts") or []
            if not isinstance(_hb_starts, list):
                _hb_starts = []
        except Exception:
            _hb_prev, _hb_starts = {}, []
        _hb_starts.append(_pipeline_start_iso)
        with open(_hb_path, "w") as _hb_f:
            _hb_json.dump({
                "last_started":  _pipeline_start_iso,
                "pid":           os.getpid() if hasattr(os, "getpid") else None,
                "starts":        _hb_starts[-400:],
            }, _hb_f, indent=2)
    except Exception:
        pass

    # Dead-man's switch needs state for duration logging — load early if not already loaded
    # (full state load happens later at Step 1, but DMS needs _pipeline_start now)
    try:
        _early_state = load_state()
        _early_state["_pipeline_start"] = datetime.now().isoformat()
        save_state(_early_state)
    except Exception:
        pass

    # ── Dead-man's switch ─────────────────────────────────────────────────
    # Checks TWO signals:
    #   1. pipeline_heartbeat.json  — did the pipeline even start recently?
    #   2. manager_decisions.json   — did the Manager Agent complete a cycle?
    # Thresholds and the age computation both come from deadmans_switch.py now
    # — the firm's one definition of this alarm. Three modules used to carry
    # their own and disagreed about the same file on the same morning; the
    # weekend subtraction below was local to this block and added a flat 24h per
    # weekend DATE the gap so much as grazed, so a Friday 23:00 → Saturday 01:00
    # gap of two hours came out at zero. Over-subtraction hides outages.
    # catch_up.py handles the auto-remediation — this is a Chairman alert only.
    # Pipeline continues regardless.
    import deadmans_switch as _dms
    _DMS_THRESHOLD_H = _dms.STALE_H
    try:
        _dm_path = Path(__file__).parent / "data" / "manager_decisions.json"
        if _dm_path.exists():
            import json as _dm_json
            with open(_dm_path) as _dm_f:
                _dm_data = _dm_json.load(_dm_f)
            _dm_ts = _dms.latest_decision_ts(_dm_data)
            if _dm_ts:
                _dm_age_h        = _dms.decision_age_hours(_dm_data)
                _effective_age_h = _dms.effective_age_hours(_dm_data)
                if _effective_age_h > _DMS_THRESHOLD_H:
                    logger.warning(
                        f"  🚨 DEAD-MAN'S SWITCH: manager_decisions.json is {_dm_age_h:.0f}h old "
                        f"({_effective_age_h:.0f}h excl. weekends) — Manager Agent missed a cycle!"
                    )
                    if send_email:
                        try:
                            from email_sender import send_alert_email
                            send_alert_email(
                                subject=f"⚠️ DEAD-MAN'S SWITCH — Manager Agent missed a cycle ({_effective_age_h:.0f}h excl. weekends)",
                                body=(
                                    f"<p><strong>ALERT:</strong> The pipeline's dead-man's switch has triggered.</p>"
                                    f"<p><code>data/manager_decisions.json</code> is <strong>{_dm_age_h:.0f} hours old</strong> "
                                    f"({_effective_age_h:.0f}h excluding weekends — threshold: {_DMS_THRESHOLD_H}h).</p>"
                                    f"<p>The Manager Agent did not complete its last cycle on a trading day.</p>"
                                    f"<p>Possible causes: pipeline crash, scheduler failure, or Alpaca connectivity issue.</p>"
                                    f"<p>Pipeline is continuing this cycle. Please investigate.</p>"
                                ),
                            )
                        except Exception as _dm_e:
                            logger.warning(f"  Dead-man's switch email failed: {_dm_e}")
                else:
                    logger.info(
                        f"  ✓ Dead-man's switch: {_dm_age_h:.1f}h old "
                        f"({_effective_age_h:.1f}h excl. weekends, threshold: {_DMS_THRESHOLD_H}h) — OK"
                    )
            else:
                logger.warning(
                    "  ⚠ Dead-man's switch DID NOT RUN: manager_decisions.json carries "
                    "no parseable decision timestamp. The switch is silent, not clear."
                )
        else:
            logger.warning(
                "  ⚠ Dead-man's switch DID NOT RUN: manager_decisions.json is missing. "
                "The switch is silent, not clear."
            )
    # WARNING, not debug. This is the firm's dead-man's switch: if the check
    # itself falls over, the one thing that must not happen is that it goes by
    # unnoticed at a log level nobody reads. A quiet deadman is indistinguishable
    # from a healthy one, which is the whole failure it exists to catch.
    except Exception as _dm_err:
        logger.warning(f"  ⚠ Dead-man's switch check FAILED to run: {_dm_err}")

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

    # ── Reconcile the position ledger against the broker ───────────────────
    # Before ANY rule reads `entry_dates`, make it agree with what we actually
    # hold. It had drifted three ways at once — TRIM popped the date off a
    # position it only partially sold, positions opened on the other host were
    # never recorded here (there is no state sync), and pre-cutover fills had no
    # row at all — and the readers hid it by defaulting a missing date to
    # "yesterday", so the max-hold exit simply never fired on 13 positions.
    # Reconciling from fill history makes the ledger a projection of the one
    # record both hosts share instead of a diary each keeps privately. It
    # refuses (returns None) if either broker read fails, so a network blip
    # cannot empty the ledger.
    try:
        from entry_date_ledger import reconcile_state_file
        _recon = reconcile_state_file(state=state)   # mutates state in place
        if _recon and (_recon["healed"] or _recon["dropped"] or _recon["unknown"]):
            logger.info(
                f"  Ledger reconciled — healed {len(_recon['healed'])}, "
                f"dropped {len(_recon['dropped'])} phantom, "
                f"{len(_recon['unknown'])} undatable")
            for _s, _why in sorted(_recon["healed"].items()):
                logger.info(f"    + {_s}: entry {_why}")
    except Exception as _rec_e:
        logger.warning(f"  Entry-date reconciliation failed: {_rec_e}")

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

    logger.info(f"\n📅 TRADING DAY {paper_day}\n")

    # -----------------------------------------------------------------------
    # Step 1: Initialize Alpaca Trader
    # -----------------------------------------------------------------------
    logger.info("Step 1: Connecting to Alpaca...")
    _alpaca_ok = True
    try:
        from alpaca_trader import AlpacaTrader
        trader = AlpacaTrader()
        logger.info("  ✅ Alpaca connection established")
    except Exception as e:
        logger.error(f"  ❌ Alpaca connection failed: {e}")
        logger.warning("  ⚠️  Continuing without Alpaca — trading steps will be skipped, "
                       "research/analysis pipeline will still run.")
        trader = None
        _alpaca_ok = False

    # ── High-water mark tracking ───────────────────────────────────────────
    if _alpaca_ok:
        try:
            _hwm_acct = trader.get_account()
            _hwm_pv   = _hwm_acct.get("portfolio_value", 0) if _hwm_acct else 0
            if _hwm_pv > 0:
                _prev_hwm = state.get("high_water_mark", 0) or 0
                if _hwm_pv > _prev_hwm:
                    state["high_water_mark"] = _hwm_pv
                    logger.info(f"  📈 New high-water mark: ${_hwm_pv:,.2f}")
                elif _prev_hwm > 0:
                    _dd = (_prev_hwm - _hwm_pv) / _prev_hwm * 100
                    logger.info(f"  HWM: ${_prev_hwm:,.0f} | Current: ${_hwm_pv:,.0f} | Drawdown: {_dd:.1f}%")
        except Exception as _hwm_e:
            logger.warning(f"  HWM update skipped: {_hwm_e}")

    # -----------------------------------------------------------------------
    # Step 2: Enforce Risk Rules (stop-loss / take-profit / max hold)
    # -----------------------------------------------------------------------
    sells_executed = []
    if _alpaca_ok:
        logger.info("\nStep 2: Enforcing risk rules on open positions...")
        entry_dates = {
            ticker: datetime.strptime(date_str, "%Y-%m-%d")
            for ticker, date_str in state["entry_dates"].items()
        }
        sells_executed = trader.enforce_risk_rules(
            entry_dates,
            exclude_tickers=_step0_closed_tickers,
        )

        for sell in sells_executed:
            logger.info(f"  SOLD {sell['ticker']} — reason: {sell.get('reason','')}, "
                        f"P&L: {sell.get('pnl_pct', 0):+.1f}%")
            # `order_id` / `qty` / `submit_status` make this row CHECKABLE against
            # the broker. Without them a recorded exit and a real one are
            # indistinguishable, and this row is not a record of a sale — it is a
            # record of a DECISION to sell. `submit_sell` returns as soon as
            # Alpaca accepts the order, and Alpaca queues anything placed outside
            # the extended-hours window until the next open (median 17.3h, max
            # 65.5h on this account). So the row below, including its `pnl_pct`,
            # is written hours before the trade happens — and `pnl_pct` is the
            # position's UNREALIZED mark at decision time (alpaca_trader.py), not
            # the price the fill got. `cit_director` turns these into the firm's
            # win rate. Measured 2026-08-27: NTNX was a +20.6% "winner" in that
            # statistic while the broker had it ACCEPTED, filled 0/90.
            #
            # The semantics of `pnl_pct` are deliberately NOT changed here — the
            # win rate and every historical row depend on it, so re-basing it to
            # realized prices is the Chairman's call, not a side effect of this
            # fix. What changes is that the discrepancy is now measurable:
            # `coherence_laws.law_recorded_exits_are_executed` matches on
            # `order_id` and alarms when a booked exit never filled.
            state["sell_log"].append({
                "date":         datetime.now().strftime("%Y-%m-%d"),
                "ticker":       sell["ticker"],
                "reason":       sell.get("reason", ""),
                "pnl_pct":      sell.get("pnl_pct", 0),
                "signal_source": _lookup_buy_source(state, sell["ticker"]),
                "order_id":     sell.get("id", ""),
                "qty":          sell.get("qty"),
                "submit_status": sell.get("status", ""),
            })
            if sell["ticker"] in state["entry_dates"]:
                del state["entry_dates"][sell["ticker"]]
    else:
        logger.info("\nStep 2: SKIPPED (Alpaca unavailable)")

    # -----------------------------------------------------------------------
    # Step 2b: Manage Regime Hedge (SH inverse ETF in bear market)
    # -----------------------------------------------------------------------
    if _alpaca_ok:
        logger.info("\nStep 2b: Managing regime hedge...")
        hedge_result = trader.manage_regime_hedge()
        logger.info(f"  Regime: {hedge_result['regime']}")
        logger.info(f"  Hedge action: {hedge_result['action']}")
    else:
        logger.info("\nStep 2b: SKIPPED (Alpaca unavailable)")

    # -----------------------------------------------------------------------
    # Step 2c: Manage Momentum Short Book
    # -----------------------------------------------------------------------
    if _alpaca_ok:
        logger.info("\nStep 2c: Managing momentum short positions...")
        short_actions = trader.manage_momentum_shorts()
        if short_actions:
            for a in short_actions:
                logger.info(f"  {a['action'].upper()}: {a['ticker']}")
        else:
            logger.info("  No short position changes today")
    else:
        logger.info("\nStep 2c: SKIPPED (Alpaca unavailable)")

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
    # Also trigger if CRO has been HIGH for 3+ consecutive cycles (governance review).
    try:
        _cro_trigger_path = Path(__file__).parent / "data" / "risk_bulletin.json"
        if _cro_trigger_path.exists():
            with open(_cro_trigger_path) as _ctf:
                _cro_trigger = json.load(_ctf)
            _cro_trigger_level  = _cro_trigger.get("risk_level", "UNKNOWN")
            _cro_streak         = _cro_trigger.get("consecutive_high_cycles", 0)
            _cro_board_required = _cro_trigger.get("board_review_warranted", False)
            if _cro_trigger_level in ("HIGH", "CRITICAL"):
                _board_meeting_needed = True
                _board_trigger_sources.append(f"CRO:{_cro_trigger_level}")
                logger.info(
                    f"  🔔 Board meeting flagged — CRO risk level: {_cro_trigger_level}"
                )
            if _cro_board_required and _cro_streak >= 3:
                _board_trigger_sources.append(
                    f"CRO:governance_review(streak={_cro_streak}_cycles)"
                )
                logger.info(
                    f"  🔔 Governance board meeting flagged — CRO at HIGH/CRITICAL "
                    f"for {_cro_streak} consecutive cycles (Chairman directive 2026-06-09)"
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
            if _alpaca_ok:
                tail_result = trader.manage_tail_hedge(_cro_level_for_hedge)
            else:
                tail_result = {'action': 'skipped'}
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
            if not _alpaca_ok:
                raise Exception("Alpaca unavailable")
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
        # Guard None and missing keys — run_pdo() may return None or a partial dict
        if not pdo_report:
            raise ValueError("run_pdo() returned None")
        _deploy = pdo_report.get("deployment") or {}
        _mkt    = pdo_report.get("market_conditions") or {}
        logger.info(
            f"  PDO: {pdo_report.get('recommendation', '?')} | "
            f"Deployed: {_deploy.get('current_pct', 0):.1f}% | "
            f"Target: {_deploy.get('target_pct', 0):.1f}% | "
            f"Regime: {(_mkt.get('regime') or 'unknown').upper()}"
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

            # ── Predictive model enhancement ──────────────────────────────
            # Apply ML multiplier to conviction scores (non-breaking: falls back
            # to multiplier=1.0 until model is trained with enough data).
            try:
                from predictive_model.predictor import enhance_signals as _enhance
                _raw_sigs = signals_df.to_dict("records")
                _enhanced = _enhance(_raw_sigs)
                # Update signals_df with ml_enhanced_conviction
                import pandas as _pd_ml
                _enh_df = _pd_ml.DataFrame(_enhanced)
                if "ml_enhanced_conviction" in _enh_df.columns and not _enh_df.empty:
                    _merge = signals_df.merge(
                        _enh_df[["ticker", "ml_score", "predictive_multiplier",
                                 "ml_enhanced_conviction"]],
                        on="ticker", how="left"
                    )
                    if "ml_enhanced_conviction" in _merge.columns:
                        _merge["conviction_score"] = _merge["ml_enhanced_conviction"].fillna(
                            _merge["conviction_score"]
                        )
                        signals_df = _merge
                _n_boosted = sum(1 for s in _enhanced if s.get("predictive_multiplier", 1.0) > 1.0)
                _model_on  = any(s.get("model_active", False) for s in _enhanced)
                logger.info(
                    f"  Predictive model: {'ACTIVE' if _model_on else 'WARMING UP (not enough data yet)'} "
                    f"| {_n_boosted}/{len(_enhanced)} signal(s) boosted"
                )
            except Exception as _pred_e:
                logger.debug(f"  Predictive enhancer error (non-fatal): {_pred_e}")

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

    if not signals_df.empty and _alpaca_ok:
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
                    # `order_id` here is INSURANCE, not a repair. Measured on the
                    # VM 2026-08-27, the entry ledger is clean: 30 of 30
                    # verifiable buy_log rows have a filled broker order, 0
                    # unfilled, 9 predating the fetched window. The 4
                    # terminal-unfilled buy orders on this account in 45 days
                    # (3 rejected, 1 expired) were all SH and never reached
                    # buy_log at all. So no buy-side law is being built for a
                    # failure class that has happened zero times.
                    #
                    # What the field buys is that a future check can be EXACT.
                    # Without it the only way to tie a row to an order is
                    # (ticker, side, ±4 days) — and SH alone had 42 filled buys
                    # inside one 4-day window, a regime where that heuristic
                    # cannot identify anything. trade_review_team reconstructs
                    # completed trades from buy_log + sell_log and cit_director
                    # derives hold time from it, so a mis-tied row is a wrong
                    # number, not a missing one. Two lines against an id already
                    # sitting in `result`.
                    state["buy_log"].append({
                        "date":       today_str,
                        "ticker":     ticker,
                        "conviction": conviction,
                        "source":     "13F",
                        "order_id":   result.get("order_id", ""),
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
    # TTL defined here so both branches (run_research True/False) can access it safely
    _RESEARCH_TTL_HOURS = 72   # covers every day (R&D runs every 3 days, TTL = 72h = 3 days)
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
        # _RESEARCH_TTL_HOURS already defined above the if/else block

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
                    _all_tickers_seen: set = {r["ticker"] for r in research_picks}
                    for _ep in _e_picks:
                        if _ep["ticker"] not in _all_tickers_seen:
                            research_picks.append(_ep)
                            _all_tickers_seen.add(_ep["ticker"])
                    _e_added = len(_all_tickers_seen) - len({r["ticker"] for r in research_picks} - {_ep["ticker"] for _ep in _e_picks})
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

        # Both HistoricalContextAnalyst and GeopoliticalResearchTeam take
        # assignments (list of {"sector_name": ...} dicts), NOT sector strings.
        _ctx_assignments = [{"sector_name": s, "sector": s} for s in _ctx_sectors]
        try:
            from rd_historical_analyst import HistoricalContextAnalyst
            _hca_result = HistoricalContextAnalyst().analyze(_ctx_assignments, quiet=True)
            state["last_context_run"] = today_str
            _analogues = _hca_result.get("historical_analogues", {})
            logger.info(
                f"  Historical Context: "
                f"{len(_analogues.get('most_relevant_bull_analogues', []))} bull "
                f"+ {len(_analogues.get('most_relevant_bear_analogues', []))} bear analogues "
                f"(sectors: {len(_ctx_sectors)}, regime: {_ctx_regime.get('regime','?')}) ✓"
            )
        except Exception as e:
            logger.warning(f"  Historical Context Analyst error: {e}")
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
        # International Analyst (IMI) — foreign indices/FX/commodities feeding US
        # implications. It has five wired consumers but was scheduled NOWHERE, so
        # it had never produced a single report; the firm was structurally blind to
        # foreign-market shocks (missed the July 2026 KOSPI crash entirely). See
        # wiring_audit.check_orphan_agents(), which now guards this class of bug.
        try:
            import international_analyst as _intl
            # universe=None -> the module's own STOCK_UNIVERSE. Do NOT pass
            # _ctx_sectors: those can be sector NAMES, and _watch_tickers()
            # intersects against ticker lists, so it would silently return [].
            _intl_result = _intl.run(universe=None)
            logger.info(
                f"  International Analyst: {_intl_result.get('sentiment', '?')} "
                f"({len(_intl_result.get('notable', []))} notable move(s), "
                f"{len(_intl_result.get('watch_tickers', []))} watch ticker(s)) ✓"
            )
        except Exception as e:
            logger.warning(f"  International Analyst error: {e}")
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

    # Daily P&L snapshot — runs here because it reads what the review agent
    # just wrote, so its numbers and the review's can never be from different
    # moments. data/performance_history.json had three readers and no writer
    # at all until 2026-07-31; the single hand-written 2026-06-09 row in it
    # was what kept the "why are we down?" alarm permanently satisfied. It is
    # deliberately in the pipeline rather than on its own cron: no new
    # scheduled job to drift, and it is idempotent per date across the four to
    # six runs a day. It never writes loss_attribution — see the module
    # docstring for why that field stays human.
    try:
        from performance_snapshot import write_snapshot
        _snap = write_snapshot()
        if _snap:
            logger.info(
                f"  P&L snapshot: ${_snap['portfolio_value']:,.2f} "
                f"({_snap['pnl_inception_pct']:+.2f}% inception)")
    except Exception as e:
        logger.warning(f"  Performance snapshot error: {e}")

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
    executions        = []   # initialised here so the DMS write below always has a valid reference
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
            try:
                from pre_trade_challenge import challenge as _da_challenge, log_challenge_result as _da_log
                _da_available = True
            except ImportError:
                _da_available = False

            for rec in all_recs:
                # Devil's advocate for high-conviction buys
                if _da_available and rec.get("action") == "BUY_NEW" and rec.get("conviction", 0) >= 4:
                    _da_result = _da_challenge(rec["ticker"], rec.get("conviction", 0))
                    _da_log(_da_result)
                    if _da_result.blocked:
                        logger.warning(
                            "  [Devil's Advocate] VETOED %s — %s",
                            rec["ticker"], _da_result.block_reason
                        )
                        continue
                    if _da_result.warnings:
                        logger.info(
                            "  [Devil's Advocate] %s warnings: %s",
                            rec["ticker"], "; ".join(_da_result.warnings)
                        )

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
                        _pnl_pct = d.get("unrealized_pnl_pct", 0)
                        _src     = _lookup_buy_source(state, d["ticker"])
                        # SECOND sell_log writer. The one at ~line 568 is the
                        # review/exit path; this is the manager's. Both book a
                        # `pnl_pct` that cit_director turns into the firm's win
                        # rate, and until 2026-08-27 neither recorded which
                        # broker order was supposed to do it — so a booked exit
                        # and a real one were indistinguishable on both paths.
                        # `execute_decision` has always returned `order_id`
                        # (manager_agent.py sets it from submit_sell/TRIM); it
                        # was simply dropped on the floor here.
                        #
                        # Fixing only the first site would have been the
                        # `guardrail-on-one-path` shape all over again: the law
                        # would have matched exactly on review-path rows and
                        # silently fallen back to the 4-day ticker heuristic on
                        # every manager exit, which reads as working. What
                        # caught it was strengthening the AST test to check each
                        # row literal separately — the original unioned the keys
                        # of all of them, so this row's missing `order_id` was
                        # masked by the other row's present one.
                        #
                        # `qty` is the trimmed size on a TRIM and absent on a
                        # full SELL (manager_agent only sets `qty_trimmed` on
                        # the trim branch). A TRIM is a PARTIAL exit recorded as
                        # a sell_log row, which is why the law counts any
                        # `filled_qty > 0` as executed rather than demanding the
                        # full position.
                        state["sell_log"].append({
                            "date":          datetime.now().strftime("%Y-%m-%d"),
                            "ticker":        d["ticker"],
                            "reason":        "manager_agent",
                            "pnl_pct":       _pnl_pct,
                            "signal_source": _src,
                            "order_id":      ex.get("order_id", ""),
                            "qty":           ex.get("qty_trimmed"),
                        })
                        if d["manager_action"] == "SELL":
                            # Only a full exit clears the entry date. A TRIM sells
                            # 40% (manager_agent.execute_decision) and BY
                            # CONSTRUCTION always leaves a position behind, so
                            # popping here orphaned every trimmed holding: the
                            # broker still held it, the ledger no longer knew when
                            # it was bought, and review_agent then read it as
                            # "bought yesterday" forever, which is exactly the
                            # state in which a max-hold exit can never fire.
                            # entry_date_ledger.reconcile() re-derives this from
                            # fill history and would undo a wrong pop anyway — this
                            # branch is here so the ledger is never wrong in the
                            # first place, not only wrong until the next run.
                            state["entry_dates"].pop(d["ticker"], None)
                        # Record outcome in performance ledger for agent attribution
                        try:
                            from performance_ledger import record_outcome as _rec_outcome
                            _rec_outcome(
                                ticker    = d["ticker"],
                                outcome   = "WIN" if _pnl_pct > 0 else ("LOSS" if _pnl_pct < 0 else "FLAT"),
                                pnl_pct   = _pnl_pct,
                                exit_reason = d["manager_action"],
                                agents    = [_src] if _src else [],
                            )
                        except Exception as _ple:
                            logger.debug("  [Ledger] record_outcome failed: %s", _ple)
                    elif d["manager_action"] in ("ADD", "BUY_NEW"):
                        state["entry_dates"][d["ticker"]] = datetime.now().strftime("%Y-%m-%d")
                        # Compute Kelly multiplier for attribution (CIT-2026-204)
                        _conv = d.get("conviction", 3.0)
                        _kelly_mult = round(max(0.50, min(1.50, 0.5 + (_conv - 3.0) * 0.25)), 2)
                        # SECOND buy_log writer (see the 13F path at ~line 862
                        # for the measurement behind this field). Both entry
                        # sites now persist the broker id, for the same reason
                        # and with the same caveat: the ledger is clean today,
                        # so this is not a fix — it is what makes a future check
                        # exact instead of a ±4-day ticker guess.
                        state["buy_log"].append({
                            "date":                    datetime.now().strftime("%Y-%m-%d"),
                            "ticker":                  d["ticker"],
                            "conviction":              _conv,
                            "source":                  _classify_signal_source(d.get("source_agent", "")),
                            "conviction_size_multiplier": _kelly_mult,
                            "order_id":                ex.get("order_id", ""),
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

    # ── Dead-man's switch heartbeat ───────────────────────────────────────
    # main.py calls evaluate_recommendation() directly (not manager_agent.run()),
    # so manager_decisions.json was never written here. This caused the DMS to
    # fire an email on every pipeline run since April 27. Fixed: write the file
    # after every manager agent block, regardless of how many trades were approved.
    try:
        _md_path = Path(__file__).parent / "data" / "manager_decisions.json"
        _md_hist: list = []
        if _md_path.exists():
            try:
                _md_hist = json.load(open(_md_path))
                if not isinstance(_md_hist, list):
                    _md_hist = [_md_hist]
            except Exception:
                _md_hist = []
        _md_hist.append({
            "generated_at": datetime.now().isoformat(),
            "timestamp":    datetime.now().isoformat(),
            "dry_run":      False,
            "decisions":    manager_decisions,
            "executions":   executions,
        })
        with open(_md_path, "w") as _md_f:
            json.dump(_md_hist[-50:], _md_f, indent=2, default=str)
        logger.info("  ✓ manager_decisions.json updated")
    except Exception as _md_e:
        logger.debug(f"  manager_decisions.json write failed (non-fatal): {_md_e}")

    # ── ML Live Picks Logging ─────────────────────────────────────────────
    # Record today's ST model picks for 20-day live performance tracking.
    try:
        import ml_live_test_runner as _mltr
        _mltr.enter_new_picks(model="shortterm", top_n=10)
        _mltr.evaluate_expired_picks()
        logger.info("  ✅ ML live picks logged → ml_live_picks_log.json")
    except Exception as _mltr_e:
        logger.debug(f"  ML live picks logging skipped: {_mltr_e}")

    # -----------------------------------------------------------------------
    # Step 4d: ETF Pod 3 orders — execute etf_pod_orders.json
    # -----------------------------------------------------------------------
    # The ETF Pod Board writes orders to data/etf_pod_orders.json.
    # Each order with action BUY/SELL and status != "executed" is submitted
    # here through the pod_ledger budget gate.  EMERGENCY_EXIT sells all
    # Pod 3 positions at market.
    # -----------------------------------------------------------------------
    logger.info("\nStep 4d: ETF Pod 3 orders — processing etf_pod_orders.json...")
    try:
        import pod_ledger as _etf_pl
        _etf_pl_ok = True
    except Exception as _etf_pl_import_e:
        logger.warning(f"  pod_ledger unavailable for ETF pod: {_etf_pl_import_e}")
        _etf_pl_ok = False

    _etf_orders_path = Path(__file__).parent / "data" / "etf_pod_orders.json"
    if _etf_orders_path.exists() and _etf_pl_ok and _alpaca_ok:
        try:
            with open(_etf_orders_path) as _etf_f:
                _etf_orders = json.load(_etf_f)
            if not isinstance(_etf_orders, list):
                _etf_orders = _etf_orders.get("orders", [])

            # Check for EMERGENCY_EXIT first
            _emergency = any(o.get("action") == "EMERGENCY_EXIT" for o in _etf_orders)
            if _emergency:
                logger.warning("  [ETF POD 3] EMERGENCY_EXIT — selling ALL pod3 positions at market")
                try:
                    if not _alpaca_ok:
                        raise Exception("Alpaca unavailable")
                    _p3_positions = trader.get_positions()
                    if _p3_positions is not None and not _p3_positions.empty:
                        from alpaca.trading.requests import MarketOrderRequest as _ETFMOR
                        from alpaca.trading.enums import OrderSide as _ETFOS, TimeInForce as _ETFTIF
                        for _, _p3row in _p3_positions.iterrows():
                            _p3tk  = _p3row["ticker"]
                            _p3qty = abs(int(float(_p3row["qty"])))
                            if _p3qty > 0:
                                try:
                                    _etf_req = _ETFMOR(
                                        symbol=_p3tk, qty=_p3qty,
                                        side=_ETFOS.SELL, time_in_force=_ETFTIF.DAY
                                    )
                                    trader.client.submit_order(_etf_req)
                                    logger.warning(f"  [EMERGENCY_EXIT] Sold {_p3qty} {_p3tk}")
                                except Exception as _p3e:
                                    logger.warning(f"  [EMERGENCY_EXIT] {_p3tk} sell failed: {_p3e}")
                    else:
                        logger.info("  [EMERGENCY_EXIT] No open positions to close")
                except Exception as _em_e:
                    logger.warning(f"  [EMERGENCY_EXIT] Error: {_em_e}")
                # Mark all emergency orders done and skip normal processing
                for _eo in _etf_orders:
                    if _eo.get("action") == "EMERGENCY_EXIT":
                        _eo["status"] = "executed"
                with open(_etf_orders_path, "w") as _etf_wf:
                    json.dump(_etf_orders, _etf_wf, indent=2, default=str)
            else:
                # Normal BUY/SELL processing
                from alpaca.trading.requests import MarketOrderRequest as _ETFMOR2
                from alpaca.trading.enums import OrderSide as _ETFOS2, TimeInForce as _ETFTIF2
                _etf_changed = False
                for _etf_ord in _etf_orders:
                    if _etf_ord.get("action") not in ("BUY", "SELL"):
                        continue
                    if _etf_ord.get("status") == "executed":
                        continue
                    _etf_tk    = _etf_ord.get("ticker", "")
                    _etf_qty   = int(_etf_ord.get("qty", 0))
                    _etf_price = float(_etf_ord.get("price", 0) or 0)
                    _etf_side  = "long" if _etf_ord["action"] == "BUY" else "short"
                    if not _etf_tk or _etf_qty <= 0:
                        continue
                    # Budget check
                    try:
                        _etf_can, _etf_why = _etf_pl.can_trade(
                            "pod3_macro", _etf_tk, _etf_qty, _etf_price, _etf_side
                        )
                    except Exception as _etf_ct_e:
                        logger.warning(f"  [ETF POD 3] can_trade error for {_etf_tk}: {_etf_ct_e}")
                        _etf_can, _etf_why = False, f"Budget check error: {_etf_ct_e}"
                    if not _etf_can:
                        logger.warning(
                            f"  [ETF POD 3] Budget check failed for {_etf_tk}: {_etf_why}"
                        )
                        _etf_ord["status"] = "budget_rejected"
                        _etf_ord["reject_reason"] = _etf_why
                        _etf_changed = True
                        continue
                    # Submit order
                    try:
                        _alpaca_side = _ETFOS2.BUY if _etf_ord["action"] == "BUY" else _ETFOS2.SELL
                        _etf_req = _ETFMOR2(
                            symbol=_etf_tk, qty=_etf_qty,
                            side=_alpaca_side, time_in_force=_ETFTIF2.DAY
                        )
                        _etf_filled = trader.client.submit_order(_etf_req)
                        _etf_fill_price = (
                            float(_etf_filled.filled_avg_price)
                            if getattr(_etf_filled, "filled_avg_price", None)
                            else _etf_price
                        )
                        _etf_ord["status"] = "executed"
                        _etf_ord["fill_price"] = _etf_fill_price
                        _etf_ord["executed_at"] = datetime.now().isoformat()
                        _etf_changed = True
                        logger.info(
                            f"  [ETF POD 3] Executed {_etf_ord['action']} {_etf_qty} {_etf_tk} "
                            f"@ ${_etf_fill_price:.2f}"
                        )
                        # Record in pod ledger
                        try:
                            _etf_pl.record_trade(
                                "pod3_macro", _etf_tk, _etf_qty,
                                _etf_fill_price, _etf_side, "etf_pod_board"
                            )
                        except Exception as _etf_rt_e:
                            logger.warning(f"  [ETF POD 3] record_trade error: {_etf_rt_e}")
                    except Exception as _etf_sub_e:
                        logger.warning(
                            f"  [ETF POD 3] Order submit failed for {_etf_tk}: {_etf_sub_e}"
                        )
                        _etf_ord["status"] = "submit_failed"
                        _etf_ord["error"] = str(_etf_sub_e)
                        _etf_changed = True
                # Write back updated orders
                if _etf_changed:
                    with open(_etf_orders_path, "w") as _etf_wf2:
                        json.dump(_etf_orders, _etf_wf2, indent=2, default=str)
                    logger.info("  [ETF POD 3] etf_pod_orders.json updated")
                else:
                    logger.info("  [ETF POD 3] No pending ETF pod orders")
        except Exception as _etf_e:
            logger.warning(f"  ETF Pod 3 processing error: {_etf_e}")
    elif not _etf_orders_path.exists():
        logger.info("  Step 4d: etf_pod_orders.json not found — skipping ETF pod")

    # -----------------------------------------------------------------------
    # Step 5: Get Portfolio Snapshot
    # -----------------------------------------------------------------------
    logger.info("\nStep 5: Capturing portfolio snapshot...")
    if _alpaca_ok:
        snapshot = trader.get_portfolio_snapshot()
    else:
        snapshot = {"portfolio_value": 0, "positions": [], "cash": 0, "equity": 0}
        logger.info("  Step 5: SKIPPED (Alpaca unavailable) — using empty snapshot")

    logger.info(f"  Portfolio Value:  ${snapshot['portfolio_value']:,.2f}")
    logger.info(f"  Cash:             ${snapshot['cash']:,.2f}")
    logger.info(f"  Today's P&L:      ${snapshot['pnl_today']:+,.2f} "
                f"({snapshot['pnl_today_pct']:+.2f}%)")
    logger.info(f"  Return vs Start:  {snapshot['return_vs_start']:+.2f}%")
    logger.info(f"  Open Positions:   {snapshot['n_positions']}")

    # ── Pod Ledger daily report ────────────────────────────────────────────
    try:
        import pod_ledger as _pl_daily
        _pl_report = _pl_daily.generate_daily_report()
        logger.info(
            f"  Pod Ledger report → data/pod_pnl_report.json | "
            f"pods: {list(_pl_report.keys()) if isinstance(_pl_report, dict) else '?'}"
        )
    except Exception as _pl_rep_e:
        logger.warning(f"  Pod ledger daily report error (non-fatal): {_pl_rep_e}")

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
    # -----------------------------------------------------------------------
    # Step 10b: Logic Officer — whole-picture coherence check
    # -----------------------------------------------------------------------
    # Runs BEFORE the IRM so any anomalies it finds can be picked up by the
    # IRM board and included in the Chairman report.
    # -----------------------------------------------------------------------
    logger.info("\nStep 10b: Logic Team — whole-picture coherence check...")
    try:
        from logic_team import run_logic_team
        lt_result    = run_logic_team()
        lt_findings  = lt_result.get("findings", 0)
        lt_critical  = lt_result.get("critical_high", 0)
        lt_proposals = lt_result.get("proposals", 0)
        if lt_findings:
            logger.warning(
                f"  Logic Team: {lt_findings} anomaly(ies) "
                f"({lt_critical} HIGH/CRITICAL) — {lt_proposals} board proposal(s)"
            )
            if lt_critical:
                _board_meeting_needed = True
                _board_trigger_sources.append(f"LogicTeam:{lt_critical}_critical")
        else:
            logger.info("  Logic Team: no anomalies — system logic checks out ✓")
        # Also run the single-agent Logic Officer for briefing continuity
        try:
            from logic_officer import run_logic_officer
            run_logic_officer()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"  Logic Team error: {e}")

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
    # Board issue ledger — advance the IRM -> board -> "Fixed issues" loop
    # -----------------------------------------------------------------------
    # This call is what makes the loop a loop. `morning_briefing` OBSERVES and
    # `email_buffer` RENDERS, and both were already wired — but every state
    # transition after the relay (a board ruling becoming ONGOING, a follow-up
    # date coming due, a verified fix becoming FIXED) happens only here. Without
    # it every issue would sit at RELAYED forever while the digest faithfully
    # reported that nothing was ever fixed, and `wiring_audit` would stay green
    # throughout, because the MODULE is reachable — it is this FUNCTION that
    # would not be. That is the orphan bug class one level down from where the
    # audit looks (memory `orphan_agents_blindness`), so it is pinned by
    # `TestBoardIssueLedger::test_the_state_machine_is_driven_by_the_pipeline`.
    #
    # It belongs immediately after Step 12: the board has just ruled, so fresh
    # rulings are picked up in the same cycle rather than waiting for the next.
    try:
        import board_issue_ledger
        _bil = board_issue_ledger.run()
        if any(_bil.get(k) for k in ("relayed", "ruled", "fixed", "struck", "escalated")):
            logger.info(
                f"  Board issue ledger: {len(_bil['relayed'])} relayed, "
                f"{len(_bil['ruled'])} ruled, {len(_bil['fixed'])} verified fixed, "
                f"{len(_bil['struck'])} failed to hold, "
                f"{len(_bil['escalated'])} escalated ({_bil['tracked']} tracked)")
    except Exception as e:
        logger.warning(f"  Board issue ledger error: {e}")

    # -----------------------------------------------------------------------
    # Predictive Model — collect snapshot + attempt retraining
    # -----------------------------------------------------------------------
    # Runs every cycle: logs current signals + fills in price outcomes for old
    # snapshots. Retraining only fires if new data materially improves the model.
    try:
        from predictive_model.data_collector import collect_snapshot as _collect_snap
        # Hand over the signals we already hold. Calling this with no argument
        # made it read data/signals.json, which nothing in this repo writes, so
        # the live snapshot path never recorded a single row — every row in
        # pipeline_snapshots comes from the historical backfill.
        _sigs = signals_df.to_dict("records") if not signals_df.empty else []
        _snap_n = _collect_snap(_sigs)
        if _snap_n:
            logger.info(f"  Predictive model: {_snap_n} snapshot(s) logged")
        elif _sigs:
            # Signals existed and none were recorded. Previously invisible.
            logger.warning(f"  Predictive model: {len(_sigs)} signal(s) produced but "
                           f"0 snapshot(s) logged — snapshot path may be broken")

        # Weekly retrain attempt (every 7 days) — only trains if data is ready
        _last_train = state.get("last_model_train", "")
        _train_now  = False
        if not _last_train:
            _train_now = True
        else:
            try:
                from datetime import date as _dt_train
                _train_now = (_dt_train.fromisoformat(today_str) -
                              _dt_train.fromisoformat(_last_train)).days >= 7
            except Exception:
                pass

        if _train_now:
            from predictive_model.ml_model import train as _ml_train
            _train_result = _ml_train(force=False)
            _train_status = _train_result.get("status", "?")
            if _train_status == "TRAINED":
                state["last_model_train"] = today_str
                logger.info(
                    f"  Predictive model: retrained — "
                    f"ensemble AUC={_train_result.get('ensemble_auc_val','?')}, "
                    f"n={_train_result.get('n_samples','?')}"
                )
            elif _train_status == "SKIPPED":
                logger.info(f"  Predictive model: {_train_result.get('reason','not enough data yet')}")
    except Exception as _pm_e:
        logger.debug(f"  Predictive model pipeline error (non-fatal): {_pm_e}")

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
    # Coherence Laws — the conservation layer
    # -----------------------------------------------------------------------
    # Runs LAST, on purpose: state has been saved, orders are in, so every law
    # reads the day as it finally stands rather than mid-flight.
    #
    # This is not another code review. CIT reads code and the Red Team argues
    # with conclusions; both take an input and critique it, so neither can see
    # anything that was never in the input — which is every escape the firm has
    # paid for. Each law here holds two INDEPENDENTLY-SOURCED numbers against
    # each other, so a violation names its own bug with nobody having predicted
    # it. Never fatal to the pipeline: a detector that can take down the thing
    # it watches gets removed the first time it does.
    logger.info("\nCoherence laws — two-source conservation checks...")
    try:
        import coherence_laws
        _cl_results = coherence_laws.run_all()
        coherence_laws._write_report(_cl_results)
        _cl_bad = [r for r in _cl_results if r.verdict == coherence_laws.VIOLATED]
        _cl_unk = [r for r in _cl_results if r.verdict == coherence_laws.CANNOT_TELL]
        for _r in _cl_bad:
            logger.error(f"  ✗ VIOLATED {_r.name}: {_r.detail}")
        for _r in _cl_unk:
            logger.warning(f"  ? CANNOT_TELL {_r.name}: {_r.detail}")
        if _cl_bad:
            coherence_laws.escalate_violations(_cl_results)
            # The queue is per-host and Jack reads the Mac's. main.py runs on the
            # VM, so without this the two laws that can only run there report
            # into a file nobody opens. Deduped on the violation set.
            coherence_laws.notify_violations(_cl_results)
        logger.info(f"  {len(_cl_results)} law(s): "
                    f"{len(_cl_results) - len(_cl_bad) - len(_cl_unk)} hold, "
                    f"{len(_cl_bad)} violated, {len(_cl_unk)} cannot tell")
    except Exception as _cle:
        logger.warning(f"  Coherence laws error: {_cle}")

    # -- Internal audit: the third line ---------------------------------------
    # Runs immediately after the laws, and on purpose. Everything above this
    # point is the firm checking itself; this is the only thing that checks the
    # checking. It poison-pills every law to prove the alarm can still fire,
    # refuses any HOLDS that cannot say how many things it examined, and holds
    # the census of failure classes against the detectors that name them.
    # A BLIND control means a whole bug class is unwatched while the dashboard
    # says otherwise — which is what every expensive escape here looked like
    # from the inside.
    logger.info("\nInternal audit — can our controls still fail?")
    try:
        import internal_audit_agent
        _ia = internal_audit_agent.run_audit()
        for _b in _ia["blind"]:
            logger.error(f"  ✗ BLIND {_b} — it passed an input built to violate it")
        for _e in _ia["empty_success"]:
            logger.error(f"  ✗ EMPTY SUCCESS {_e} — HOLDS on zero examined items")
        for _u in _ia["uncovered"]:
            logger.warning(f"  ? UNCOVERED failure class: {_u}")
        internal_audit_agent.escalate(_ia)
        logger.info(f"  {len(_ia['falsifiability'])} control(s) tested, "
                    f"{len(_ia['blind'])} blind, {len(_ia['unproven'])} unproven; "
                    f"{len(_ia['census'])} failure class(es), "
                    f"{len(_ia['uncovered'])} uncovered")
    except Exception as _iae:
        logger.warning(f"  Internal audit error: {_iae}")

    # -----------------------------------------------------------------------
    # Note: CIT (Continuous Improvement Team) runs on its own cron schedule
    # (6:00 AM ET + 6:00 PM ET) independent of the main pipeline.
    # Its proposals queue up in data/cit_pending_proposals.json and are voted
    # on at the next board meeting (Step 12 above).
    # -----------------------------------------------------------------------

    logger.info("\n" + "=" * 65)
    logger.info(f"  DAILY RUN COMPLETE — Day {paper_day}")
    logger.info(f"  Portfolio: ${snapshot['portfolio_value']:,.2f} "
                f"({snapshot['return_vs_start']:+.2f}% from start)")
    if paper_day == 30:
        logger.info("  TRADING MILESTONE COMPLETE")
    logger.info("=" * 65)

    # ── Flush log health stats ────────────────────────────────────────────
    try:
        _flush_log_health()
    except Exception:
        pass

    # ── Pipeline completion heartbeat ────────────────────────────────────
    try:
        import json as _chb_json
        _chb_path = Path(__file__).parent / "data" / "pipeline_heartbeat.json"
        # Carry `starts` forward. This block rewrites the whole file, so
        # omitting the key would delete the run history the START block just
        # appended to — a completion handler quietly erasing the evidence of
        # how often the pipeline ran. That is the shape of memory
        # `fix_that_erased_its_detector`, and it would have made
        # coherence_laws.law_runs_match_schedule read 1 start every day
        # forever, no matter how many times main.py actually fired.
        try:
            _chb_starts = _chb_json.loads(_chb_path.read_text()).get("starts") or []
        except Exception:
            _chb_starts = [_pipeline_start_iso]
        with open(_chb_path, "w") as _chb_f:
            _chb_json.dump({
                "last_started":   _pipeline_start_iso,   # local var, always set at run start
                "last_completed": datetime.now().isoformat(),
                "status":         "completed",
                "paper_day":      state.get("paper_day", 0),
                "pid":            os.getpid() if hasattr(os, "getpid") else None,
                "starts":         _chb_starts[-400:],
            }, _chb_f, indent=2)
    except Exception:
        pass

    # ── External heartbeat ping (CIT-2026-217) ────────────────────────────
    # Pings healthchecks.io (or similar) at end of each successful run.
    # Two ways to configure:
    #   1. Set APEX_HEALTHCHECK_URL env var (recommended for GCP)
    #   2. Set HEALTHCHECK_PING_URL in config.py (fallback for local)
    # Free account at https://healthchecks.io — paste your UUID ping URL.
    # If the scheduler misses two consecutive windows, you get an email immediately.
    try:
        import subprocess as _sp
        _hc_url = (os.environ.get("APEX_HEALTHCHECK_URL", "")
                   or getattr(__import__("config"), "HEALTHCHECK_PING_URL", "")
                   or "")
        if _hc_url:
            _sp.run(
                ["curl", "-fsS", "--max-time", "10", "--retry", "3", _hc_url],
                capture_output=True, timeout=15,
            )
            logger.info(f"  Heartbeat pinged: {_hc_url[:40]}...")
        else:
            logger.debug("  Heartbeat: no URL configured (set HEALTHCHECK_PING_URL in config.py)")
    except Exception:
        pass  # heartbeat is best-effort — never block the pipeline


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ── Single-instance guard ─────────────────────────────────────────────────
    # main.py runs 4× daily — if a run takes longer than 2 hrs, the next cron
    # would otherwise stack on top of it.
    import tempfile as _tempfile
    _lock_file = open(Path(_tempfile.gettempdir()) / "main_pipeline.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.getLogger(__name__).warning("  main.py already running — skipping this cron tick.")
        sys.exit(0)

    # ── Hang watchdog ─────────────────────────────────────────────────────────
    # A normal run is minutes. On 2026-06-19 a run hung (stuck network/IO) and,
    # because flock is held for the life of the process, it blocked EVERY cron tick
    # for ~13 days (state.json went stale, no trades executed) with nothing noticing.
    # This daemon thread force-exits after 90 min so the process dies, the flock
    # releases, and the next cron tick runs normally.
    import threading as _th
    def _hang_watchdog():
        import time as _t
        _t.sleep(90 * 60)
        logging.getLogger(__name__).error(
            "main.py watchdog: run exceeded 90 min — force-exiting to release the lock.")
        os._exit(2)
    _th.Thread(target=_hang_watchdog, daemon=True).start()

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
