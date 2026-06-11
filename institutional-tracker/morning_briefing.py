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
    """Return how many hours ago a timestamp was."""
    if not ts_str:
        return 999
    try:
        return (datetime.now() - datetime.fromisoformat(str(ts_str)[:19])).total_seconds() / 3600
    except Exception:
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
    canary       = _safe_load("data/canary_alert.json")
    canary_state = _safe_load("data/canary_state.json")
    market_status = canary_state.get("market_status", "unknown")
    canary_level  = canary.get("level", "GREEN")
    canary_alerts = canary.get("alerts", [])
    high_alerts   = [a for a in canary_alerts if a.get("severity") in ("HIGH", "CRITICAL")]
    briefing["sections"]["market"] = {
        "status":        market_status,
        "canary_level":  canary_level,
        "alert_count":   len(canary_alerts),
        "high_alerts":   high_alerts[:5],
        "triggers":      canary.get("triggers", [])[:5],
        "last_updated":  canary.get("generated_at", ""),
    }
    if canary_level in ("RED", "ORANGE"):
        briefing["alerts"].append(
            f"🚨 Canary {canary_level} — emergency cascade active: "
            + "; ".join(canary.get("triggers", [])[:3])
        )
    elif high_alerts:
        briefing["alerts"].append(f"⚠ Canary: {len(high_alerts)} high-severity alert(s) pre-market")
    logger.info(f"    Market: {market_status} | Canary: {canary_level} | Alerts: {len(canary_alerts)}")

    # ── 2. Risk environment (CRO) ─────────────────────────────────────────────
    logger.info("\n  [2/15] CRO — risk environment...")
    risk_bulletin = _safe_load("data/risk_bulletin.json")
    risk_level    = risk_bulletin.get("risk_level", "UNKNOWN")
    cvar          = risk_bulletin.get("cvar_95", 0)
    directives    = risk_bulletin.get("directives", [])
    pdo_signal    = risk_bulletin.get("pdo_signal", "NEUTRAL")
    briefing["sections"]["risk"] = {
        "risk_level":   risk_level,
        "cvar_95":      cvar,
        "directives":   directives,
        "pdo_signal":   pdo_signal,
        "last_updated": risk_bulletin.get("generated_at", ""),
    }
    if risk_level in ("HIGH", "CRITICAL"):
        briefing["alerts"].append(f"🔴 CRO: Risk level is {risk_level} — tightened buy thresholds active")
    logger.info(f"    Risk: {risk_level} | CVaR: {cvar:.2f}% | PDO: {pdo_signal}")

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
    macro_ctx  = _safe_load("data/risk_manager_report.json")   # MacroPolicyHistorian writes here
    sysperf    = _safe_load("data/rd_system_performance.json")

    briefing["sections"]["context"] = {
        "historical": {
            "summary":      hist_ctx.get("summary", hist_ctx.get("analysis", ""))[:300],
            "last_updated": hist_ctx.get("generated_at", ""),
            "fresh":        _fresh(hist_ctx.get("generated_at", ""), 48),
        },
        "geopolitical": {
            "summary":      geo_ctx.get("summary", geo_ctx.get("analysis", ""))[:300],
            "key_risks":    geo_ctx.get("key_risks", geo_ctx.get("risks", []))[:3],
            "last_updated": geo_ctx.get("generated_at", ""),
            "fresh":        _fresh(geo_ctx.get("generated_at", ""), 48),
        },
        "macro_policy": {
            "summary":      macro_ctx.get("summary", "")[:300],
            "last_updated": macro_ctx.get("generated_at", ""),
            "fresh":        _fresh(macro_ctx.get("generated_at", ""), 48),
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
    cos_age_h  = _age_hours(cos_report.get("generated_at", ""))
    attention  = cos_report.get("attention_items", [])
    briefing["sections"]["cos"] = {
        "report_age_hours": round(cos_age_h, 1),
        "attention_items":  attention[:5],
        "registry_size":    cos_report.get("registry_size", 0),
        "last_updated":     cos_report.get("generated_at", ""),
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
        "total_agents":      hr_report.get("total_agents_evaluated", 0),
        "last_updated":      hr_report.get("generated_at", ""),
    }
    if action_required:
        briefing["alerts"].append(f"👥 HR: {len(action_required)} agent(s) flagged for performance review")
    logger.info(f"    HR: Health {firm_health}/10 | Action required: {len(action_required)}")

    # ── 11. Board — last meeting summary ──────────────────────────────────────
    logger.info("\n  [11/15] Board — last meeting summary...")
    board_report = _safe_load("data/board_meeting_report.json")
    board_age_h  = _age_hours(board_report.get("generated_at", ""))
    consensus    = board_report.get("consensus", {})
    last_actions = board_report.get("implementation", {})
    last_decisions = board_report.get("decisions", [])
    briefing["sections"]["board"] = {
        "meeting_age_hours": round(board_age_h, 1),
        "unanimous_count":   consensus.get("unanimous_count", 0),
        "majority_count":    consensus.get("majority_count", 0),
        "vetoed_count":      consensus.get("vetoed_count", 0),
        "top_decisions":     last_decisions[:3],
        "trades_executed":   last_actions.get("executed_count", 0),
        "last_updated":      board_report.get("generated_at", ""),
    }
    logger.info(f"    Board: {consensus.get('unanimous_count',0)} unanimous | {consensus.get('majority_count',0)} majority | Age: {board_age_h:.1f}h")

    # ── 12. Manager — last cycle decisions ────────────────────────────────────
    logger.info("\n  [12/15] Manager — last cycle decisions...")
    mgr_raw = _safe_load("data/manager_decisions.json")
    mgr_data = mgr_raw if isinstance(mgr_raw, dict) else (mgr_raw[-1] if isinstance(mgr_raw, list) and mgr_raw else {})
    mgr_age_h  = _age_hours(mgr_data.get("generated_at", mgr_data.get("timestamp", "")))
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
    if mgr_age_h > 30:
        briefing["alerts"].append(f"🚨 Manager: decisions are {mgr_age_h:.0f}h old — DEAD-MAN'S SWITCH risk")
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
    trade_findings = trade_review.get("findings", trade_review.get("lessons", []))
    briefing["sections"]["trade_review"] = {
        "finding_count": len(trade_findings),
        "top_findings":  trade_findings[:3],
        "win_rate":      trade_review.get("win_rate", trade_review.get("accuracy", 0)),
        "last_updated":  trade_review.get("generated_at", ""),
        "fresh":         _fresh(trade_review.get("generated_at",""), 168),  # weekly
    }
    logger.info(f"    Trade review: {len(trade_findings)} findings | Win rate: {trade_review.get('win_rate', '—')}")

    # ── 15. Capital efficiency ────────────────────────────────────────────────
    logger.info("\n  [15/15] Capital efficiency — deployment signal...")
    cap_report  = _safe_load("data/capital_efficiency_report.json")
    deploy_signal = cap_report.get("signal", cap_report.get("pdo_signal", "NEUTRAL"))
    deploy_pct    = cap_report.get("deployed_pct", briefing["sections"].get("portfolio", {}).get("deployed_pct", 0))
    briefing["sections"]["capital"] = {
        "signal":       deploy_signal,
        "deployed_pct": deploy_pct,
        "directive":    cap_report.get("directive", ""),
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
    briefing["sections"]["ops_director"] = {
        "age_hours":       round(ops_age_h, 1),
        "floor_status":    ops_report.get("floor_status", ops_report.get("status", "UNKNOWN")),
        "agents_reviewed": ops_report.get("agents_reviewed", ops_report.get("total_agents_supervised", 0)),
        "issues_found":    ops_report.get("issues_found", ops_report.get("total_issues", 0)),
        "top_issues":      ops_report.get("issues", ops_report.get("agent_issues", []))[:3],
        "directive":       ops_report.get("directive", ops_report.get("floor_directive", "")),
        "last_updated":    ops_report.get("generated_at", ""),
    }
    if ops_report.get("issues_found", ops_report.get("total_issues", 0)) > 0:
        briefing["alerts"].append(
            f"🏢 Ops Director: {ops_report.get('issues_found', ops_report.get('total_issues', '?'))} issue(s) on the floor"
        )
    logger.info(f"    Ops Director: {ops_report.get('floor_status', ops_report.get('status', '—'))} | "
                f"Age: {ops_age_h:.1f}h")

    # ── 18. Firm Channel — ideas from the floor (overnight + this morning) ────
    logger.info("\n  [18/18] Firm Channel — overnight idea sharing...")
    try:
        from firm_channel import summary_for_briefing as _fc_summary
        _channel = _fc_summary(hours=20)   # last 20h covers yesterday evening + this morning
        briefing["sections"]["firm_channel"] = _channel
        _urgent = _channel.get("urgent", [])
        if _urgent:
            for _u in _urgent[:3]:
                briefing["alerts"].append(
                    f"📢 {_u['agent']}: {_u['content'][:80]}"
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
        "canary":            {"fresh": _fresh(canary.get("generated_at",""), 1),      "level": canary_level},
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
        "board":             {"fresh": board_age_h < 24, "last_meeting": board_report.get("generated_at","")},
        "manager":           {"fresh": mgr_age_h < 12,  "decisions": len(mgr_data.get("decisions",[]))},
        "review":            {"fresh": rev_age_h < 12,  "buy_recs": len(buy_recs)},
        "trade_review":      {"fresh": briefing["sections"]["trade_review"]["fresh"]},
        "capital":           {"signal": deploy_signal},
        "reconciliation":    {"fresh": recon_age_h < 1, "status": recon_status,
                              "n_critical": recon_report.get("n_critical", 0)},
        "ops_director":      {"fresh": ops_age_h < 12,
                              "floor_status": ops_report.get("floor_status", ops_report.get("status", "—"))},
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

    # ── Buffer summary email (goes to digest) ─────────────────────────────────
    try:
        alert_rows = "".join(
            f"<li style='color:#f0883e;margin-bottom:4px;'>{a}</li>"
            for a in briefing["alerts"]
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
      Market: <strong style="color:#3fb950;">{market_status}</strong>
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

    # ── Inject Logic Team open findings into morning briefing ─────────────
    try:
        from logic_team import get_open_findings_summary
        lt_open = get_open_findings_summary()
        if lt_open:
            briefing["logic_team_open"] = lt_open
            critical_high = [f for f in lt_open if f["severity"] in ("CRITICAL","HIGH")]
            if critical_high:
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
                logger.info(f"  LLM executive summary generated ({len(_summary)} chars)")
    except Exception as _llm_e:
        logger.debug(f"LLM morning summary skipped: {_llm_e}")

    return briefing


if __name__ == "__main__":
    run_morning_briefing()
