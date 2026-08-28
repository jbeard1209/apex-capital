#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — MORNING BRIEFING
# =============================================================================
# Runs at 9:15 AM EDT (13:15 UTC) Mon-Fri — 15 min before board meeting,
# 30 minutes before the first pipeline run.
#
# Collects the latest state from EVERY agent and writes a shared
# data/morning_briefing.json that every agent reads at the start of their run.
# This ensures all agents start each day with exactly the same shared context.
#
# Sections collected:
#   1.  Market context        — Canary alert + state
#   2.  Risk environment      — CRO risk bulletin
#   3.  Portfolio snapshot    — Alpaca live positions
#   4.  R&D findings          — Research team picks
#   5.  Energy research       — Energy sub-sector report
#   6.  Context agents        — Historical, Geopolitical, MacroPolicy, SystemPerf
#   7.  Chief of Staff        — CoS briefing + attention items
#   8.  CIT                   — Continuous Improvement Team proposals
#   9.  IRM                   — Overnight board check
#   10. HR                    — Agent health + action items
#   11. Board                 — Last meeting minutes summary
#   12. Manager               — Last cycle decisions
#   13. Review agent          — Latest recommendations
#   14. Trade review          — Post-mortem findings
#   15. Capital efficiency    — Deployment signal
#
# Also sends a "what to watch today" summary to the digest buffer.
# =============================================================================

import os
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s  %(levelname)-8s %(message)s",
    datefmt= "%H:%M:%S",
)

BRIEFING_PATH = "data/morning_briefing.json"


def _safe_load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _age_hours(ts_str: str) -> float:
    """Return how many hours ago a timestamp was.

    The old body was `datetime.now() - fromisoformat(ts[:19])`. The `[:19]` slice
    throws away the offset, so a UTC-stamped artifact was compared against LOCAL
    now: on the Mac that reported it exactly 4h younger than it is (5h in EST —
    the error even changes with DST). 15 of the firm's ~75 artifacts carry a
    tz-aware stamp, including fi_pipeline_report and macro_regime_state, and the
    error runs in the dangerous direction: ages read low, so a producer dead for
    15.9h still clears a 12h freshness gate. The VM is UTC, so this was green
    there and wrong here — the host-divergence shape again.

    Compare like with like: an aware stamp against aware now, a naive stamp
    against local now (correct, since the naive writers all use datetime.now()).
    """
    if not ts_str:
        return 999
    s = str(ts_str).strip().replace("Z", "+00:00")
    for candidate in (s, s[:19]):      # [:19] still rescues odd trailing junk
        try:
            dt = datetime.fromisoformat(candidate)
        except Exception:
            continue
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return (now - dt).total_seconds() / 3600
    return 999


def _fresh(ts_str: str, ttl_hours: float) -> bool:
    return _age_hours(ts_str) < ttl_hours


def run_morning_briefing() -> dict:
    """
    Collect state from ALL agents and write the shared morning briefing.
    Returns the briefing dict.
    """
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — MORNING BRIEFING")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    today = datetime.now().strftime("%Y-%m-%d")
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "date":         today,
        "sections":     {},
        "alerts":       [],
        "watchlist":    [],
        "agent_status": {},
    }

    # ── 1. Market context (Canary) ────────────────────────────────────────────
    logger.info("\n  [1/15] Canary — market conditions...")
    # Three separate reads of canary_alert.json were wrong here until
    # 2026-08-27, and each was silent:
    #   · `level` was read without `active`, so a cleared ORANGE put "🚨 Canary
    #     ORANGE — emergency cascade active" at the top of Jack's briefing for
    #     three days after the market had gone quiet again;
    #   · `alerts` and `generated_at` are keys the canary has NEVER written
    #     (it writes `triggers` and `triggered_at`), so alert_count was
    #     permanently 0, high_alerts permanently empty, and last_updated "".
    # Liveness now comes from the one accessor; the keys below are the ones
    # canary_agent._write_alert_file actually writes.
    from canary_agent import load_active_alert
    canary       = load_active_alert("data/canary_alert.json")
    canary_state = _safe_load("data/canary_state.json")
    # `market_status` is a key NOTHING in this firm has ever written, so this
    # line returned "unknown" every day the panel has existed — and rendered it
    # in hardcoded green (see the HTML below).
    #
    # The near-miss fix was `canary_state["regime"]`, which does exist. It is
    # still the wrong quantity: canary_agent:421 derives it as a pure relabel of
    # the canary ALERT LEVEL (GREEN→BULL, ORANGE→DEFENSIVE), so it is a
    # statement about the firm's own plumbing, and printing it here would
    # restate the Canary field on the next line under a market heading — a
    # panel carrying zero information, which is a quieter version of the same
    # defect. The firm does measure the market: macro_regime_model.py scores
    # SPY vs its 200MA, 12-1 momentum, VIX, the 10-2 curve, credit spreads and
    # breadth into data/macro_regime_state.json with a confidence.
    #
    # It runs 12:50 UTC Mon-Fri, so the longest LEGITIMATE gap is Friday's run
    # read on the Tuesday after a Monday holiday (~91h) — hence 96, not 24. A
    # 24h threshold here would report the model dead every Monday. If it really
    # is stale we say UNKNOWN rather than falling back to the canary relabel:
    # substituting a different quantity is how this line broke in the first place.
    _macro       = _safe_load("data/macro_regime_state.json")
    _macro_age_h = _age_hours(_macro.get("computed_at", ""))
    _macro_conf  = _macro.get("confidence")
    market_status = (_macro.get("regime", "UNKNOWN") if _macro_age_h < 96
                     else "UNKNOWN")
    canary_level  = canary.get("level", "GREEN")
    triggers      = canary.get("triggers", []) or []
    briefing["sections"]["market"] = {
        "status":        market_status,
        "regime_confidence": _macro_conf,
        "regime_age_hours":  round(_macro_age_h, 1),
        "canary_level":  canary_level,
        "alert_count":   len(triggers),
        "triggers":      triggers[:5],
        "last_updated":  canary.get("triggered_at", ""),
    }
    if canary_level in ("RED", "ORANGE"):
        briefing["alerts"].append(
            f"🚨 Canary {canary_level} — emergency cascade active: "
            + "; ".join(triggers[:3])
        )
    logger.info(f"    Market: {market_status} | Canary: {canary_level} | Triggers: {len(triggers)}")

    # ── 2. Risk environment (CRO) ─────────────────────────────────────────────
    logger.info("\n  [2/15] CRO — risk environment...")
    risk_bulletin = _safe_load("data/risk_bulletin.json")
    risk_level    = risk_bulletin.get("risk_level", "UNKNOWN")
    # The bulletin has never carried `cvar_95` or `pdo_signal`. CVaR is computed
    # by risk_manager_agent and lives under var_metrics there; the bulletin's own
    # deployment stance is `capital_guidance`. Both reads returned their defaults
    # every day, so the CRO panel reported "CVaR 0.00%" against a real $2,064.
    # Note the UNIT: cvar_95 is DOLLARS, not a percentage — the old log line
    # formatted it with a % sign, which would have been wrong even if the key
    # had resolved.
    _rm_var       = _safe_load("data/risk_manager_report.json").get("var_metrics", {})
    cvar          = _rm_var.get("cvar_95", 0) if isinstance(_rm_var, dict) else 0
    directives    = risk_bulletin.get("directives", [])
    pdo_signal    = risk_bulletin.get("capital_guidance", "NEUTRAL")
    briefing["sections"]["risk"] = {
        "risk_level":   risk_level,
        "cvar_95":      cvar,
        "directives":   directives,
        "pdo_signal":   pdo_signal,
        "last_updated": risk_bulletin.get("generated_at", ""),
    }
    if risk_level in ("HIGH", "CRITICAL"):
        briefing["alerts"].append(f"🔴 CRO: Risk level is {risk_level} — tightened buy thresholds active")
    logger.info(f"    Risk: {risk_level} | CVaR 95% 1d: ${cvar:,.0f} | Guidance: {pdo_signal}")

    # ── 3. Portfolio snapshot (Alpaca) ────────────────────────────────────────
    logger.info("\n  [3/15] Portfolio — current positions...")
    try:
        from alpaca_trader import AlpacaTrader
        trader       = AlpacaTrader()
        acct         = trader.get_account()
        port_val     = acct.get("portfolio_value", 0)
        cash         = acct.get("cash", 0)
        deployed     = ((port_val - cash) / port_val * 100) if port_val > 0 else 0
        positions_df = trader.get_positions()
        positions_list = []
        if hasattr(positions_df, 'empty') and not positions_df.empty:
            for _, row in positions_df.iterrows():
                positions_list.append({
                    "ticker":  row.get("ticker", "?"),
                    "qty":     row.get("qty", 0),
                    "pnl_pct": round(float(row.get("unrealized_pct", 0)), 2),
                    "side":    str(row.get("side", "")),
                })
                pnl = float(row.get("unrealized_pct", 0))
                if pnl < -5:
                    briefing["alerts"].append(
                        f"⚠ {row.get('ticker','?')} is down {pnl:.1f}% — monitor stop-loss"
                    )
        briefing["sections"]["portfolio"] = {
            "portfolio_value": port_val,
            "cash":            cash,
            "deployed_pct":    round(deployed, 1),
            "n_positions":     len(positions_list),
            "positions":       positions_list,
        }
        logger.info(f"    Portfolio: ${port_val:,.0f} | Deployed: {deployed:.1f}% | Positions: {len(positions_list)}")
    except Exception as e:
        logger.warning(f"    Portfolio snapshot failed: {e}")
        briefing["sections"]["portfolio"] = {"error": str(e)}

    # ── 4. R&D findings ───────────────────────────────────────────────────────
    logger.info("\n  [4/15] R&D — latest research...")
    rd_report  = _safe_load("data/research_team_report.json")
    rd_age_h   = _age_hours(rd_report.get("generated_at", ""))
    picks      = rd_report.get("picks", rd_report.get("recommendations", []))
    if isinstance(picks, dict):
        picks = list(picks.values())
    top_picks = [p for p in (picks[:10] if picks else []) if isinstance(p, dict)]
    briefing["sections"]["rd"] = {
        "report_age_hours": round(rd_age_h, 1),
        "pick_count":       len(picks) if picks else 0,
        "top_picks":        top_picks[:5],
        "last_updated":     rd_report.get("generated_at", ""),
    }
    if rd_age_h > 24:
        briefing["alerts"].append(f"⚠ R&D report is {rd_age_h:.0f}h old — research may be stale")
    logger.info(f"    R&D: {len(picks) if picks else 0} picks | Age: {rd_age_h:.1f}h")

    # ── 5. Energy research ────────────────────────────────────────────────────
    logger.info("\n  [5/15] Energy R&D — sector report...")
    energy_report = _safe_load("data/energy_research_report.json")
    energy_picks  = energy_report.get("picks", energy_report.get("recommendations", []))
    if isinstance(energy_picks, dict):
        energy_picks = list(energy_picks.values())
    briefing["sections"]["energy_rd"] = {
        "pick_count":   len(energy_picks) if energy_picks else 0,
        "top_picks":    [p for p in (energy_picks[:5] if energy_picks else []) if isinstance(p, dict)],
        "last_updated": energy_report.get("generated_at", ""),
        "fresh":        _fresh(energy_report.get("generated_at", ""), 24),
    }
    logger.info(f"    Energy R&D: {len(energy_picks) if energy_picks else 0} picks")

    # ── 6. Context agents ─────────────────────────────────────────────────────
    logger.info("\n  [6/15] Context agents — Historical, Geopolitical, MacroPolicy, SystemPerf...")
    hist_ctx   = _safe_load("data/rd_historical_context.json")
    geo_ctx    = _safe_load("data/rd_geopolitical_context.json")
    sysperf    = _safe_load("data/rd_system_performance.json")

    # All three of these panels rendered as an empty string, every day, because
    # `summary` / `analysis` are keys none of these producers writes. The
    # macro_policy panel was worse than a wrong key — it pointed at the wrong
    # FILE. risk_manager_report.json is the Risk Manager's output (account,
    # var_metrics, risk_assessment); the MacroPolicyHistorian's work is
    # `policy_analysis` inside rd_geopolitical_context.json, which is where the
    # policy analogue actually lives.
    _hist_syn  = hist_ctx.get("synthesis", {}) or {}
    _geo_an    = geo_ctx.get("geopolitical_analysis", {}) or {}
    _pol_an    = geo_ctx.get("policy_analysis", {}) or {}

    briefing["sections"]["context"] = {
        "historical": {
            "summary":      " — ".join(x for x in (
                                _hist_syn.get("regime_summary", ""),
                                _hist_syn.get("stance_rationale", "")) if x)[:300],
            "last_updated": hist_ctx.get("generated_at", ""),
            "fresh":        _fresh(hist_ctx.get("generated_at", ""), 48),
        },
        "geopolitical": {
            "summary":      geo_ctx.get("team_summary", "")[:300],
            # active_risk_events is a list of dicts; the panel wants readable
            # lines, so take the event name rather than dumping the object.
            "key_risks":    [e.get("event", "") if isinstance(e, dict) else str(e)
                             for e in (_geo_an.get("active_risk_events") or [])][:3],
            "last_updated": geo_ctx.get("generated_at", ""),
            "fresh":        _fresh(geo_ctx.get("generated_at", ""), 48),
        },
        "macro_policy": {
            "summary":      " — ".join(x for x in (
                                _pol_an.get("current_policy_analogue", ""),
                                _pol_an.get("current_analogue_lesson", "")) if x)[:300],
            "last_updated": geo_ctx.get("generated_at", ""),
            "fresh":        _fresh(geo_ctx.get("generated_at", ""), 48),
        },
        "system_performance": {
            "health_score": sysperf.get("health_score", sysperf.get("score", 0)),
            "issues":       sysperf.get("issues", sysperf.get("findings", []))[:3],
            "last_updated": sysperf.get("generated_at", ""),
            "fresh":        _fresh(sysperf.get("generated_at", ""), 24),
        },
    }
    sys_issues = briefing["sections"]["context"]["system_performance"]["issues"]
    if sys_issues:
        briefing["alerts"].append(f"⚙ System: {len(sys_issues)} operational issue(s) flagged")
    logger.info(f"    Historical: {'✓' if briefing['sections']['context']['historical']['fresh'] else '⚠ stale'} | "
                f"Geo: {'✓' if briefing['sections']['context']['geopolitical']['fresh'] else '⚠ stale'} | "
                f"SysPerf: {len(sys_issues)} issues")

    # ── 7. Chief of Staff ─────────────────────────────────────────────────────
    logger.info("\n  [7/15] Chief of Staff — briefing summary...")
    cos_report = _safe_load("data/chief_of_staff_report.json")
    # chief_of_staff_agent stamps its report `timestamp`, not `generated_at`, so
    # _age_hours("") returned its 999 sentinel and this panel reported the CoS
    # report as ~41 days stale every single morning. The registry count lives
    # under factory_result (risk_manager_agent:663 reads it from exactly there).
    cos_ts     = cos_report.get("timestamp", "")
    cos_age_h  = _age_hours(cos_ts)
    attention  = cos_report.get("attention_items", [])
    briefing["sections"]["cos"] = {
        "report_age_hours": round(cos_age_h, 1),
        "attention_items":  attention[:5],
        "registry_size":    (cos_report.get("factory_result") or {}).get("registry_size", 0),
        "last_updated":     cos_ts,
    }
    _cos_seen: set = set()
    for item in attention[:5]:
        if isinstance(item, dict) and item.get("priority") in ("HIGH", "CRITICAL"):
            _msg = f"📌 CoS: {item.get('item', '')[:80]}"
            if _msg not in _cos_seen:
                _cos_seen.add(_msg)
                briefing["alerts"].append(_msg)
    logger.info(f"    CoS: {len(attention)} attention items | Age: {cos_age_h:.1f}h")

    # ── 8. CIT (Continuous Improvement Team) — PROMINENT SECTION ─────────────
    logger.info("\n  [8/15] CIT — improvement proposals & ThinkTank...")
    cit_morning  = _safe_load("data/cit_morning_report.json")
    cit_evening  = _safe_load("data/cit_evening_report.json")
    cit_pending  = _safe_load("data/cit_pending_proposals.json")
    cit_approved = _safe_load("data/cit_approved_proposals.json")
    cit_backlog  = _safe_load("data/cit_backlog.json")
    cit_think_tank = []
    try:
        import json as _json
        if os.path.exists("data/cit_think_tank.json"):
            with open("data/cit_think_tank.json") as _ttf:
                _tt_data = _json.load(_ttf)
                cit_think_tank = _tt_data if isinstance(_tt_data, list) else []
    except Exception:
        pass

    pending_list  = cit_pending  if isinstance(cit_pending,  list) else cit_pending.get("proposals",  [])
    approved_list = cit_approved if isinstance(cit_approved, list) else cit_approved.get("proposals", [])
    backlog_list  = cit_backlog  if isinstance(cit_backlog,  list) else cit_backlog.get("proposals",  [])

    # Latest session is whichever ran most recently; if both are missing timestamps, use empty dict
    _eve_age = _age_hours(cit_evening.get("generated_at", ""))
    _mrn_age = _age_hours(cit_morning.get("generated_at", ""))
    if _eve_age == float("inf") and _mrn_age == float("inf"):
        cit_latest = {}
    else:
        cit_latest = cit_evening if _eve_age < _mrn_age else cit_morning
    cit_proposals = cit_latest.get("proposals", [])

    # All pending proposals with full detail — CIT gets full visibility in morning briefing
    briefing["sections"]["cit"] = {
        "pending_count":      len(pending_list),
        "approved_count":     len(approved_list),
        "backlog_count":      len(backlog_list),
        "latest_session":     cit_latest.get("session", ""),
        "top_proposals":      cit_proposals[:6],          # show top 6 (was 3)
        "all_pending":        pending_list[:10],           # full pending queue for agents to see
        "think_tank":         cit_think_tank[:5],          # ThinkTank VISION proposals
        "quick_wins":         cit_latest.get("quick_win_items", [])[:4],
        "last_updated":       cit_latest.get("generated_at", ""),
        "morning_fresh":      _fresh(cit_morning.get("generated_at", ""), 14),
        "evening_fresh":      _fresh(cit_evening.get("generated_at", ""), 14),
    }
    if len(pending_list) > 20:
        briefing["alerts"].append(f"📋 CIT: {len(pending_list)} proposals pending board vote — queue getting long")
    if cit_think_tank:
        briefing["alerts"].append(f"💡 CIT ThinkTank: {len(cit_think_tank)} VISION proposal(s) awaiting board unanimous vote")
    logger.info(f"    CIT: {len(cit_proposals)} new proposals | {len(pending_list)} pending | {len(approved_list)} approved | {len(cit_think_tank)} ThinkTank")

    # ── 9. IRM — overnight board check ───────────────────────────────────────
    logger.info("\n  [9/15] IRM — overnight risk check...")
    irm_check   = _safe_load("data/irm_daily_check.json")
    irm_age_h   = _age_hours(irm_check.get("generated_at", ""))
    panel       = irm_check.get("board_panel", {})
    irm_flags   = panel.get("combined_red_flags", [])
    irm_score   = panel.get("highest_risk_score", 0)
    irm_verdict = panel.get("board_verdict", "")
    briefing["sections"]["irm"] = {
        "report_age_hours": round(irm_age_h, 1),
        "risk_score":       irm_score,
        "red_flags":        irm_flags[:3],
        "verdict":          irm_verdict,
        "last_updated":     irm_check.get("generated_at", ""),
    }
    if irm_flags:
        briefing["alerts"].append(f"🔍 IRM: {len(irm_flags)} red flag(s) from overnight board check")
    logger.info(f"    IRM: Score {irm_score}/10 | Flags: {len(irm_flags)} | Age: {irm_age_h:.1f}h")

    # ── 10. HR — agent health ──────────────────────────────────────────────────
    logger.info("\n  [10/15] HR — agent health check...")
    hr_report       = _safe_load("data/hr_report.json")
    action_required = hr_report.get("action_required", [])
    firm_health     = hr_report.get("firm_health_score", 0)
    hr_history      = _safe_load("data/hr_history.json")
    briefing["sections"]["hr"] = {
        "firm_health_score": firm_health,
        "action_required":   action_required[:5],
        # hr_agent:1942 writes `agents_reviewed` = len(agents). Reading
        # `total_agents_evaluated` reported 0 agents for a 155-agent firm.
        "total_agents":      hr_report.get("agents_reviewed", 0),
        "last_updated":      hr_report.get("generated_at", ""),
    }
    if action_required:
        briefing["alerts"].append(f"👥 HR: {len(action_required)} agent(s) flagged for performance review")
    logger.info(f"    HR: Health {firm_health}/10 | Action required: {len(action_required)}")

    # ── 11. Board — last meeting summary ──────────────────────────────────────
    logger.info("\n  [11/15] Board — last meeting summary...")
    board_report = _safe_load("data/board_meeting_report.json")
    # board_meeting:2878 writes `timestamp`, `consensus` and `implementation` —
    # never `generated_at`, never `decisions`, and `implementation` holds
    # `executed` (a list), not `executed_count`. So this panel reported the last
    # board meeting as 999h old with zero decisions, on a report written the
    # same afternoon. The consensus counts were the only reads here that worked.
    board_ts     = board_report.get("timestamp", "")
    board_age_h  = _age_hours(board_ts)
    consensus    = board_report.get("consensus", {})
    last_actions = board_report.get("implementation", {})
    last_decisions = consensus.get("actions", []) or []
    # Recounted from the ACTIONS, not read from the stored counts. The counts on
    # disk were written by whatever version of board_meeting ran last, and every
    # version before 2026-08-27 reported three of five classifications — so a
    # report written this morning still carries no split_count at all, and
    # reading `.get("split_count", 0)` would render "0 split" for a meeting that
    # was six-sevenths split. The actions each carry their own classification,
    # so recomputing is both backward-compatible and self-checking.
    from board_meeting import count_by_classification, consensus_tally
    _bc = count_by_classification(consensus)
    briefing["sections"]["board"] = {
        "meeting_age_hours": round(board_age_h, 1),
        "unanimous_count":   _bc["UNANIMOUS"],
        "majority_count":    _bc["MAJORITY"],
        "vetoed_count":      _bc["VETOED"],
        # The two the board decided and reported to nobody.
        "split_count":       _bc["SPLIT"],
        "unvoted_count":     _bc["UNVOTED"],
        "unclassified_count": _bc["unclassified"],
        "action_count":      _bc["total"],
        "top_decisions":     last_decisions[:3],
        "trades_executed":   len(last_actions.get("executed", []) or []),
        "last_updated":      board_ts,
    }
    logger.info(f"    Board: {consensus_tally(consensus)} | Age: {board_age_h:.1f}h")

    # ── 12. Manager — last cycle decisions ────────────────────────────────────
    logger.info("\n  [12/15] Manager — last cycle decisions...")
    mgr_raw = _safe_load("data/manager_decisions.json")
    mgr_data = mgr_raw if isinstance(mgr_raw, dict) else (mgr_raw[-1] if isinstance(mgr_raw, list) and mgr_raw else {})
    # Age via the shared policy (deadmans_switch.py). This file used to compute
    # its own, on data[-1] and with no weekend adjustment, against a threshold
    # of 30h — one of three different implementations of one alarm.
    import deadmans_switch as _dms
    mgr_age_h = _dms.decision_age_hours(mgr_raw)
    mgr_eff_h = _dms.effective_age_hours(mgr_raw)
    mgr_buys   = [d for d in mgr_data.get("decisions", []) if str(d.get("action","")).upper() in ("BUY","ADD")]
    mgr_sells  = [d for d in mgr_data.get("decisions", []) if str(d.get("action","")).upper() in ("SELL","TRIM","CLOSE")]
    mgr_holds  = [d for d in mgr_data.get("decisions", []) if str(d.get("action","")).upper() == "HOLD"]
    briefing["sections"]["manager"] = {
        "age_hours":     round(mgr_age_h, 1),
        "total_decisions": len(mgr_data.get("decisions", [])),
        "buys":          len(mgr_buys),
        "sells":         len(mgr_sells),
        "holds":         len(mgr_holds),
        "top_buys":      mgr_buys[:3],
        "top_sells":     mgr_sells[:3],
        "last_updated":  mgr_data.get("generated_at", mgr_data.get("timestamp", "")),
    }
    # Report BOTH ages. Printing only the weekend-adjusted one turns a real
    # 65h Friday-to-Monday gap into "17h", which reads like nothing.
    if mgr_eff_h > _dms.STALE_H:
        briefing["alerts"].append(
            f"🚨 Manager: no decision for {mgr_age_h:.0f}h "
            f"({mgr_eff_h:.0f}h excl. weekends, threshold {_dms.STALE_H:.0f}h) "
            f"— DEAD-MAN'S SWITCH")
    logger.info(f"    Manager: {len(mgr_buys)} buys | {len(mgr_sells)} sells | {len(mgr_holds)} holds | Age: {mgr_age_h:.1f}h")

    # ── 13. Review agent — recommendations ────────────────────────────────────
    logger.info("\n  [13/15] Review agent — recommendations...")
    review_rec  = _safe_load("data/review_recommendations.json")
    rev_age_h   = _age_hours(review_rec.get("generated_at", ""))
    buy_recs    = [r for r in review_rec.get("recommendations", []) if str(r.get("action","")).upper() == "BUY"]
    sell_recs   = [r for r in review_rec.get("recommendations", []) if str(r.get("action","")).upper() in ("SELL","CLOSE")]
    briefing["sections"]["review"] = {
        "age_hours":    round(rev_age_h, 1),
        "buy_count":    len(buy_recs),
        "sell_count":   len(sell_recs),
        "top_buys":     buy_recs[:3],
        "top_sells":    sell_recs[:3],
        "last_updated": review_rec.get("generated_at", ""),
    }
    logger.info(f"    Review: {len(buy_recs)} buy recs | {len(sell_recs)} sell recs | Age: {rev_age_h:.1f}h")

    # ── 14. Trade review — post-mortem ────────────────────────────────────────
    logger.info("\n  [14/15] Trade review — post-mortem findings...")
    trade_review  = _safe_load("data/trade_review_report.json")
    # trade_review_team:558-564 writes `analytics`, `risk_insights`, `cos_summary`
    # — none of `findings`, `lessons`, `win_rate` or `accuracy` at top level. The
    # panel therefore showed "0 findings | Win rate: 0" against a real 23.7% over
    # 38 trades, which is exactly the number the Chairman needs to see.
    _tr_analytics = trade_review.get("analytics", {}) or {}
    _tr_insights  = trade_review.get("risk_insights", {}) or {}
    trade_findings = _tr_insights.get("notes", []) or []
    _tr_win_rate  = _tr_analytics.get("win_rate", 0)
    briefing["sections"]["trade_review"] = {
        "finding_count": len(trade_findings),
        "top_findings":  trade_findings[:3],
        "win_rate":      _tr_win_rate,
        "total_trades":  _tr_analytics.get("total_trades", 0),
        "last_updated":  trade_review.get("generated_at", ""),
        "fresh":         _fresh(trade_review.get("generated_at",""), 168),  # weekly
    }
    logger.info(f"    Trade review: {len(trade_findings)} findings | Win rate: {_tr_win_rate}% "
                f"over {_tr_analytics.get('total_trades', 0)} trades")

    # ── 15. Capital efficiency ────────────────────────────────────────────────
    logger.info("\n  [15/15] Capital efficiency — deployment signal...")
    cap_report  = _safe_load("data/capital_efficiency_report.json")
    # capital_efficiency_agent:413-428 writes `recommendation`, `uncapped_signal`,
    # `deployment.current_pct` and `manager_directives`. Neither `signal` nor
    # `pdo_signal` has ever existed, so deploy_signal was the literal "NEUTRAL"
    # on every run — which made the DEPLOY_MORE alert below unreachable: the
    # only other way into it is deploy_pct < 50, and that came from the
    # portfolio fallback. An alarm whose trigger value cannot occur is not an
    # alarm. (deploy_pct itself DID resolve, via that fallback — leaving it.)
    #
    # This artifact has TWO writers. capital_efficiency_agent writes the routine
    # `recommendation`; board_meeting:2834 additionally stamps `pdo_signal` +
    # `board_override` when it issues a formal DEPLOY_MORE directive, which the
    # Manager Agent acts on next cycle. The board's directive outranks the
    # routine read, so check it first — this is the one case where the old
    # `pdo_signal` read was reaching a key that really does get written.
    if cap_report.get("board_override"):
        deploy_signal = cap_report.get("pdo_signal", "NEUTRAL")
    else:
        deploy_signal = cap_report.get("recommendation", "NEUTRAL")
    # `deployed_pct` is not a key of this artifact either; the agent's own figure
    # is deployment.current_pct. The old line fell through to the PORTFOLIO
    # section's number, which is a different measurement taken at a different
    # time — so the panel could pair this agent's signal with someone else's
    # percentage and print "DEPLOY_MORE — 83.5% deployed" off two clocks that
    # never agreed. Signal and level now come from the same reading; the
    # portfolio number stays only as a last resort when the agent hasn't run.
    _cap_deploy   = cap_report.get("deployment", {}) or {}
    deploy_pct    = _cap_deploy.get("current_pct",
                        briefing["sections"].get("portfolio", {}).get("deployed_pct", 0))
    _cap_directives = cap_report.get("manager_directives", []) or []
    briefing["sections"]["capital"] = {
        "signal":       deploy_signal,
        "deployed_pct": deploy_pct,
        "uncapped_signal": cap_report.get("uncapped_signal", ""),
        "directive":    "; ".join(str(d) for d in _cap_directives)[:300],
        "last_updated": cap_report.get("generated_at", ""),
    }
    if deploy_signal == "DEPLOY_MORE" or (isinstance(deploy_pct, (int,float)) and deploy_pct < 50):
        briefing["alerts"].append(f"💰 Capital: only {deploy_pct:.1f}% deployed — DEPLOY_MORE directive active")
    logger.info(f"    Capital: {deploy_pct}% deployed | Signal: {deploy_signal}")

    # ── 16. Reconciliation Engine — last compliance check ────────────────────
    logger.info("\n  [16/18] Reconciliation Engine — last compliance audit...")
    recon_report = _safe_load("data/reconciliation_report.json")
    recon_age_h  = _age_hours(recon_report.get("generated_at", ""))
    recon_status = recon_report.get("status", "UNKNOWN")
    recon_flags  = recon_report.get("flags", [])
    briefing["sections"]["reconciliation"] = {
        "age_hours":   round(recon_age_h, 1),
        "status":      recon_status,
        "n_flags":     recon_report.get("n_flags", 0),
        "n_critical":  recon_report.get("n_critical", 0),
        "n_high":      recon_report.get("n_high", 0),
        "n_medium":    recon_report.get("n_medium", 0),
        "top_flags":   recon_flags[:3],
        "last_updated": recon_report.get("generated_at", ""),
    }
    if recon_report.get("n_critical", 0) > 0:
        briefing["alerts"].append(
            f"🚨 Reconciliation: {recon_report['n_critical']} CRITICAL flag(s) — "
            + ", ".join(f['check'] for f in recon_flags if f.get('severity') == 'CRITICAL')[:80]
        )
    elif recon_report.get("n_high", 0) > 0:
        briefing["alerts"].append(
            f"⚠️ Reconciliation: {recon_report['n_high']} HIGH flag(s) — "
            + ", ".join(f['check'] for f in recon_flags if f.get('severity') == 'HIGH')[:80]
        )
    logger.info(f"    Reconciliation: {recon_status} | {recon_report.get('n_flags', 0)} flag(s) | Age: {recon_age_h:.1f}h")

    # ── 17. Operations Director — floor supervision summary ───────────────────
    logger.info("\n  [17/18] Operations Director — floor supervision summary...")
    ops_report  = _safe_load("data/ops_director_report.json")
    ops_age_h   = _age_hours(ops_report.get("generated_at", ""))
    # This panel has NEVER worked. All ten reads below named keys
    # operations_director.py has never written — it emits `supervised_agents`,
    # `agent_status`, `flags` and `summary` (build_floor_brief, ~line 52) — so
    # every field took its fallback and the Chairman's floor panel has read
    # "UNKNOWN / 0 agents / 0 issues / no directive" every day since it shipped,
    # against a real 6 agents and a live stale-report flag.
    #
    # floor_status is DERIVED, because the producer never states one: it records
    # a per-agent status in the vocabulary OK / STALE / NO_REPORT / ERROR. Note
    # the empty case stays UNKNOWN — an absent artifact must not render as a
    # healthy floor, which is what `all(... == "OK")` over an empty dict does.
    _ops_status  = ops_report.get("agent_status", {}) or {}
    _ops_levels  = {str(v.get("status", "")).upper()
                    for v in _ops_status.values() if isinstance(v, dict)}
    _ops_flags   = ops_report.get("flags", []) or []
    if not _ops_levels:
        floor_status = "UNKNOWN"
    elif _ops_levels & {"NO_REPORT", "ERROR"}:
        floor_status = "DEGRADED"
    elif "STALE" in _ops_levels:
        floor_status = "STALE"
    else:
        floor_status = "OK"
    briefing["sections"]["ops_director"] = {
        "age_hours":       round(ops_age_h, 1),
        "floor_status":    floor_status,
        "agents_reviewed": len(ops_report.get("supervised_agents", []) or []),
        "issues_found":    len(_ops_flags),
        "top_issues":      [str(f) for f in _ops_flags][:3],
        "directive":       ops_report.get("summary", ""),
        "last_updated":    ops_report.get("generated_at", ""),
    }
    if _ops_flags:
        briefing["alerts"].append(
            f"🏢 Ops Director: {len(_ops_flags)} issue(s) on the floor"
        )
    logger.info(f"    Ops Director: {floor_status} | {len(_ops_flags)} flag(s) | "
                f"Age: {ops_age_h:.1f}h")

    # ── 18. Firm Channel — ideas from the floor (overnight + this morning) ────
    logger.info("\n  [18/18] Firm Channel — overnight idea sharing...")
    try:
        from firm_channel import summary_for_briefing as _fc_summary
        _channel = _fc_summary(hours=20)   # last 20h covers yesterday evening + this morning
        briefing["sections"]["firm_channel"] = _channel
        _urgent = _channel.get("urgent", [])
        if _urgent:
            # Dedupe BEFORE the [:3] cap, not after, and that ordering is the
            # whole fix. The Foreign Order Detector posts on every run, so the
            # same "2 unexplained account order(s)" message appeared three times
            # in a 20h window and took all three slots — Jack saw one warning
            # printed three times, and every OTHER agent's urgent post was
            # silently pushed out of the briefing entirely. The repetition was
            # the visible symptom; the suppression was the damage.
            _seen, _uniq = set(), []
            for _u in _urgent:
                _key = (_u.get("agent", ""), _u.get("content", "")[:80])
                if _key in _seen:
                    continue
                _seen.add(_key)
                _uniq.append(_u)
            _dropped = len(_urgent) - len(_uniq)
            for _u in _uniq[:3]:
                briefing["alerts"].append(
                    f"📢 {_u['agent']}: {_u['content'][:80]}"
                )
            if len(_uniq) > 3:
                # Never let the cap hide the fact that it is capping.
                briefing["alerts"].append(
                    f"📢 Firm Channel: {len(_uniq) - 3} further urgent post(s) not shown"
                )
        logger.info(
            f"    Firm Channel: {_channel['total']} posts | "
            f"{len(_channel.get('by_agent', {}))} agent(s) active | "
            f"{len(_urgent)} urgent"
        )
    except Exception as _fce:
        briefing["sections"]["firm_channel"] = {"total": 0, "error": str(_fce)}
        logger.warning(f"    Firm Channel: unavailable ({_fce})")

    # ── Compile full agent status ──────────────────────────────────────────────
    briefing["agent_status"] = {
        # Liveness comes from canary_state.json, not canary_alert.json. This
        # read `canary.get("generated_at")` — a key the canary has never
        # written — so _age_hours returned its 999 sentinel and this flag was
        # False every single day since it was added. The firm's most
        # safety-critical agent had a liveness indicator hardwired to "stale",
        # which means a canary that ACTUALLY died would have looked identical.
        # canary_alert.json is the wrong source anyway: it is only written when
        # an alert fires or clears, so on a normal day it is correctly old.
        # canary_state["last_level_time"] is written on every run (see
        # canary_agent, unconditional before _save_state), and the canary is a
        # 7-day producer — */5 in market hours, */30 otherwise, weekends
        # included — so an hour of slack is two missed runs, not a weekend.
        "canary":            {"fresh": _fresh(canary_state.get("last_level_time",""), 1),
                              "level": canary_level},
        "cro":               {"fresh": _fresh(risk_bulletin.get("generated_at",""), 12), "risk_level": risk_level},
        "rd_team":           {"fresh": rd_age_h < 12,                                 "picks": len(picks) if picks else 0},
        "energy_rd":         {"fresh": _fresh(energy_report.get("generated_at",""), 24)},
        "historical":        {"fresh": briefing["sections"]["context"]["historical"]["fresh"]},
        "geopolitical":      {"fresh": briefing["sections"]["context"]["geopolitical"]["fresh"]},
        "macro_policy":      {"fresh": briefing["sections"]["context"]["macro_policy"]["fresh"]},
        "system_performance":{"fresh": briefing["sections"]["context"]["system_performance"]["fresh"],
                              "issues": len(sys_issues)},
        "cos":               {"fresh": cos_age_h < 24},
        "cit":               {"morning_fresh": briefing["sections"]["cit"]["morning_fresh"],
                              "evening_fresh": briefing["sections"]["cit"]["evening_fresh"],
                              "pending": len(pending_list)},
        "irm":               {"fresh": irm_age_h < 30,  "score": irm_score},
        "hr":                {"fresh": bool(hr_report),  "health": firm_health},
        "board":             {"fresh": board_age_h < 24, "last_meeting": board_ts},
        "manager":           {"fresh": mgr_age_h < 12,  "decisions": len(mgr_data.get("decisions",[]))},
        "review":            {"fresh": rev_age_h < 12,  "buy_recs": len(buy_recs)},
        "trade_review":      {"fresh": briefing["sections"]["trade_review"]["fresh"]},
        "capital":           {"signal": deploy_signal},
        "reconciliation":    {"fresh": recon_age_h < 1, "status": recon_status,
                              "n_critical": recon_report.get("n_critical", 0)},
        "ops_director":      {"fresh": ops_age_h < 12,
                              "floor_status": floor_status},
        "deputy_board":      {"fresh": _fresh(_safe_load("data/deputy_board_report.json").get("generated_at",""), 24),
                              "approved": _safe_load("data/deputy_board_report.json").get("approved_count", 0)},
    }

    # ── Save briefing ─────────────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    with open(BRIEFING_PATH, "w") as f:
        json.dump(briefing, f, indent=2, default=str)
    logger.info(f"\n  ✅ Morning briefing saved → {BRIEFING_PATH}")

    # ── Log summary ───────────────────────────────────────────────────────────
    stale_agents = [k for k, v in briefing["agent_status"].items() if not v.get("fresh", True)]
    logger.info(f"\n  📋 BRIEFING SUMMARY — {today}")
    logger.info(f"  Market: {market_status} | Canary: {canary_level} | Risk: {risk_level} | IRM: {irm_score}/10")
    logger.info(f"  Portfolio: ${briefing['sections'].get('portfolio',{}).get('portfolio_value',0):,.0f} | "
                f"Deployed: {briefing['sections'].get('portfolio',{}).get('deployed_pct',0):.1f}%")
    if stale_agents:
        logger.info(f"  ⚠ Stale agents: {', '.join(stale_agents)}")
    if briefing["alerts"]:
        logger.info(f"  ⚠ {len(briefing['alerts'])} alert(s):")
        for alert in briefing["alerts"]:
            logger.info(f"    {alert}")
    else:
        logger.info("  ✅ No alerts — clean bill of health")
    logger.info(f"  Board meeting in ~15 min. Pipeline fires in ~30 min.")
    logger.info("=" * 60)

    # ── Late alert injections — MOVED UP 2026-08-17, and the move is a fix ────
    # These two blocks used to sit ~120 lines below, AFTER the briefing email
    # had already been composed and sent. Both exist to put something in front
    # of the Chairman — one is captioned "inject Logic Team open findings into
    # morning briefing", the other "surface stale-data issues prominently" —
    # and neither had ever appeared in a briefing email. They appended to
    # `briefing["alerts"]` after `alert_rows` was built from it, so their output
    # reached data/morning_briefing.json (re-saved at the end) and nothing else.
    # A CRITICAL Logic Team finding and a STALE DATA sentinel alert were both
    # being written to a file nobody opens. They must also run before the ledger
    # call below, or the relay would silently cover only some of the alerts.
    try:
        from logic_team import get_open_findings_summary
        lt_open = get_open_findings_summary()
        if lt_open:
            briefing["logic_team_open"] = lt_open
            critical_high = [f for f in lt_open if f["severity"] in ("CRITICAL", "HIGH")]
            for cf in critical_high:
                briefing["alerts"].append({
                    "source": "Logic Team",
                    "severity": cf["severity"],
                    "message": cf["title"],
                    "days_open": cf.get("days_open", 0),
                })
        else:
            briefing["logic_team_open"] = []
    except Exception as _lt_e:
        logger.debug(f"Logic Team injection skipped: {_lt_e}")

    try:
        fresh = _safe_load("data/freshness_report.json")
        if fresh:
            # Carry the denominator, not just the verdict. The sentinel only
            # measures artifacts THIS host owns (no state sync between hosts),
            # so a Mac report reading "HEALTHY, 0 stale" can be a sweep of 6
            # items out of 32 — true about what it looked at, and a firm-wide
            # all-clear to anyone who reads only `overall`.
            briefing["freshness"] = {
                "overall": fresh.get("overall", "?"),
                "stale":   fresh.get("stale", 0),
                "checked": fresh.get("checked"),
                "in_manifest": fresh.get("in_manifest"),
                "deferred_to_other_host": fresh.get("deferred_n", 0),
                "measured_on": fresh.get("host", "?"),
                "auto_fixed": fresh.get("auto_fixed", []),
            }
            for i in fresh.get("issues", []):
                if i.get("critical"):
                    briefing["alerts"].append({
                        "source": "Freshness Sentinel", "severity": "HIGH",
                        "message": f"STALE DATA: {i.get('item','?')} — {i.get('detail','')}",
                    })
    except Exception as _fs_e:
        logger.debug(f"Freshness injection skipped: {_fs_e}")

    # ── Route the alerts to the board, and say so in the email ────────────────
    # Chairman directive 2026-08-17: a warning Jack cannot personally fix must
    # reach whoever can. `process()` records each alert against a normalized
    # signature, relays repeat offenders to the board anonymously, and hands
    # back the same lines with their board status appended — so
    # "⚠ R&D report is 62h old" becomes "... : this information has been
    # relayed to the board", then "... : ongoing fix by board (...) (date)".
    #
    # It runs unconditionally, INCLUDING on a clean day with zero alerts. The
    # empty call is the heartbeat that proves this observer was listening, and
    # every promotion to "fixed by board" rests on it: silence from a detector
    # that stopped running is not evidence of anything.
    _alert_lines = [str(a) for a in briefing["alerts"]]
    try:
        import board_issue_ledger
        _alert_lines = board_issue_ledger.process(briefing["alerts"], "morning_briefing")
    except Exception as _bil_e:
        logger.warning(f"  ⚠ Board issue ledger unavailable — alerts not routed: {_bil_e}")

    # ── Buffer summary email (goes to digest) ─────────────────────────────────
    try:
        alert_rows = "".join(
            f"<li style='color:#f0883e;margin-bottom:4px;'>{a}</li>"
            for a in _alert_lines
        ) or "<li style='color:#3fb950;'>No alerts — clean bill of health.</li>"

        pos_rows = ""
        for p in briefing["sections"].get("portfolio", {}).get("positions", []):
            color = "#3fb950" if p.get("pnl_pct", 0) >= 0 else "#f85149"
            pos_rows += (
                f"<tr><td style='padding:4px 8px;'>{p['ticker']}</td>"
                f"<td style='padding:4px 8px;color:{color};'>{p['pnl_pct']:+.1f}%</td>"
                f"<td style='padding:4px 8px;color:#8b949e;'>{p['side']}</td></tr>"
            )

        # Agent status grid
        status_cells = ""
        status_icons = {"fresh": "✅", "stale": "⚠️"}
        for agent, status in briefing["agent_status"].items():
            is_fresh = status.get("fresh", True)
            icon     = "✅" if is_fresh else "⚠️"
            status_cells += (
                f"<span style='background:#161b22;padding:4px 10px;border-radius:3px;"
                f"margin:2px;display:inline-block;font-size:12px;border:1px solid "
                f"{'#238636' if is_fresh else '#f0883e'};'>"
                f"{icon} {agent}</span>"
            )

        # CIT proposals
        cit_section = briefing["sections"].get("cit", {})
        cit_html = (
            f"<p style='margin:4px 0;font-size:13px;'>"
            f"CIT: <strong>{cit_section.get('pending_count',0)}</strong> pending · "
            f"<strong>{cit_section.get('approved_count',0)}</strong> approved · "
            f"Latest: <em>{', '.join(str(p.get('title','?'))[:40] for p in cit_section.get('top_proposals',[])[:2])}</em>"
            f"</p>"
            if cit_section.get("pending_count", 0) > 0 else ""
        )

        html = f"""
<div style="font-family:Arial,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px;max-width:750px;">
  <h2 style="color:#58a6ff;border-bottom:2px solid #21262d;padding-bottom:8px;">
    🌅 Morning Briefing — {today}
  </h2>
  <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
    <span style="background:#1a2a1a;padding:5px 10px;border-radius:4px;">
      Market: <strong style="color:{'#3fb950' if market_status in ('BULL','RECOVERY') else '#f0c000' if market_status == 'LATE_CYCLE' else '#f85149' if market_status == 'CONTRACTION' else '#8b949e'};">{market_status}</strong>
    </span>
    <span style="background:#{'2a1a1a' if canary_level in ('RED','ORANGE') else '1a1a2a'};padding:5px 10px;border-radius:4px;">
      Canary: <strong style="color:{'#f85149' if canary_level == 'RED' else '#f0883e' if canary_level == 'ORANGE' else '#f0c000' if canary_level == 'YELLOW' else '#3fb950'};">{canary_level}</strong>
    </span>
    <span style="background:#2a1a1a;padding:5px 10px;border-radius:4px;">
      Risk: <strong style="color:{'#f85149' if risk_level in ('HIGH','CRITICAL') else '#f0883e' if risk_level == 'ELEVATED' else '#3fb950'};">{risk_level}</strong>
    </span>
    <span style="background:#1a1a2a;padding:5px 10px;border-radius:4px;">
      IRM: <strong style="color:#58a6ff;">{irm_score}/10</strong>
    </span>
    <span style="background:#1a2a1a;padding:5px 10px;border-radius:4px;">
      Deployed: <strong style="color:#3fb950;">{briefing['sections'].get('portfolio',{}).get('deployed_pct',0):.1f}%</strong>
    </span>
    <span style="background:#1a1a2a;padding:5px 10px;border-radius:4px;">
      R&D: <strong style="color:#e6edf3;">{briefing['sections']['rd']['pick_count']} picks</strong>
    </span>
  </div>
  <h3 style="color:#f0883e;margin:0 0 6px 0;font-size:14px;">Pre-Market Alerts</h3>
  <ul style="margin:0 0 14px 0;padding-left:18px;">{alert_rows}</ul>
  {cit_html}
  {'<h3 style="color:#8b949e;margin:12px 0 6px 0;font-size:13px;">Open Positions</h3><table style="width:100%;border-collapse:collapse;font-size:13px;">' + pos_rows + '</table>' if pos_rows else ''}
  <h3 style="color:#8b949e;margin:14px 0 6px 0;font-size:13px;">Agent Status</h3>
  <div style="margin-bottom:8px;">{status_cells}</div>
  {'<p style="color:#f0883e;font-size:12px;margin:4px 0;">⚠ Stale: ' + ', '.join(stale_agents) + '</p>' if stale_agents else ''}
  <p style="color:#484f58;font-size:11px;margin-top:16px;border-top:1px solid #21262d;padding-top:8px;">
    Board meets at 9:30 AM EDT · Pipeline fires at 9:45 AM EDT ·
    All {len(briefing['agent_status'])} agents briefed with this shared context
  </p>
</div>"""

        from email_sender import send_daily_report
        send_daily_report(
            html_body   = html,
            subject     = f"🌅 Morning Briefing — {today} | {canary_level} canary | {risk_level} risk | {len(briefing['alerts'])} alert(s)",
            sender_name = "morning_briefing",
            key_metrics = {
                "Canary":     canary_level,
                "Risk Level": risk_level,
                "IRM Score":  f"{irm_score}/10",
                "Deployed":   f"{briefing['sections'].get('portfolio',{}).get('deployed_pct',0):.1f}%",
                "Alerts":     len(briefing["alerts"]),
                "R&D Picks":  briefing["sections"]["rd"]["pick_count"],
            },
        )
    except Exception as e:
        logger.debug(f"Morning briefing email error (non-fatal): {e}")


    # ── Fable 5 LLM executive summary ─────────────────────────────────────
    # Generates a crisp "what to watch today" narrative for the Chairman.
    try:
        from apex_llm import reason as _llm_r, is_available as _llm_ok
        if _llm_ok("morning_briefing"):
            _port = briefing["sections"].get("portfolio", {})
            _rd   = briefing["sections"].get("rd", {})
            _alerts_text = "; ".join(
                a.get("message", str(a)) if isinstance(a, dict) else str(a)
                for a in briefing.get("alerts", [])[:5]
            ) or "None"
            _positions = ", ".join(
                f"{p['ticker']} {p.get('pnl_pct',0):+.1f}%"
                for p in _port.get("positions", [])[:8]
            ) or "none"
            _ctx = (
                f"Date: {today}\n"
                f"Market: {market_status} | Canary: {canary_level} | CRO: {risk_level} | IRM score: {irm_score}/10\n"
                f"Portfolio: ${_port.get('portfolio_value',0):,.0f} | "
                f"Deployed: {_port.get('deployed_pct',0):.1f}% | "
                f"P&L today: {_port.get('pnl_today_pct',0):+.2f}%\n"
                f"Open positions: {_positions}\n"
                f"R&D picks available: {_rd.get('pick_count', 0)}\n"
                f"Alerts: {_alerts_text}\n\n"
                "Write a crisp 3-sentence 'what to watch today' executive summary for the Chairman. "
                "Lead with the most important thing, note any risk or opportunity, end with today's priority action."
            )
            _summary = _llm_r(
                "You are the Apex Capital Morning Briefing Agent. You write sharp, "
                "specific morning summaries for the firm's Chairman. Be direct, concrete, "
                "and actionable — no filler.",
                _ctx,
                agent_name="morning_briefing",
                max_tokens=220,
            )
            if _summary:
                briefing["llm_executive_summary"] = _summary.strip()
                with open(BRIEFING_PATH, "w") as f:
                    json.dump(briefing, f, indent=2, default=str)
                logger.info(f"  LLM executive summary generated ({len(_summary)} chars)")
    except Exception as _llm_e:
        logger.debug(f"LLM morning summary skipped: {_llm_e}")

    return briefing


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('MorningBriefing'):
        raise SystemExit(0)
    run_morning_briefing()
