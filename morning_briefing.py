#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — MORNING BRIEFING
# =============================================================================
# Runs at 9:15 AM EDT (13:15 UTC) Mon-Fri — 30 minutes before the pipeline.
#
# Collects the latest state from every agent and writes a shared
# data/morning_briefing.json that every agent reads at the start of their run.
# This ensures the Manager, CRO, CoS, IRM, and all research agents start each
# day with exactly the same shared context — no one is working from stale data.
#
# Also sends a brief "what to watch today" summary to the digest buffer.
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


def run_morning_briefing() -> dict:
    """
    Collect state from all agents and write the shared morning briefing.
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
    logger.info("\n  [1/7] Canary — market conditions...")
    canary = _safe_load("data/canary_alert.json")
    canary_state = _safe_load("data/canary_state.json")
    market_status = canary_state.get("market_status", "unknown")
    canary_alerts = canary.get("alerts", [])
    high_alerts = [a for a in canary_alerts if a.get("severity") in ("HIGH", "CRITICAL")]
    briefing["sections"]["market"] = {
        "status":        market_status,
        "alert_count":   len(canary_alerts),
        "high_alerts":   high_alerts[:5],
        "last_updated":  canary.get("generated_at", ""),
    }
    if high_alerts:
        briefing["alerts"].append(f"⚠ Canary: {len(high_alerts)} high-severity alert(s) pre-market")
    logger.info(f"    Market: {market_status} | Alerts: {len(canary_alerts)} ({len(high_alerts)} high)")

    # ── 2. Risk environment (CRO) ─────────────────────────────────────────────
    logger.info("\n  [2/7] CRO — risk environment...")
    risk_bulletin = _safe_load("data/risk_bulletin.json")
    risk_level  = risk_bulletin.get("risk_level", "UNKNOWN")
    cvar        = risk_bulletin.get("cvar_95", 0)
    directives  = risk_bulletin.get("directives", [])
    pdo_signal  = risk_bulletin.get("pdo_signal", "NEUTRAL")
    briefing["sections"]["risk"] = {
        "risk_level":  risk_level,
        "cvar_95":     cvar,
        "directives":  directives,
        "pdo_signal":  pdo_signal,
        "last_updated": risk_bulletin.get("generated_at", ""),
    }
    if risk_level in ("HIGH", "CRITICAL"):
        briefing["alerts"].append(f"🔴 CRO: Risk level is {risk_level} — tightened buy thresholds active")
    logger.info(f"    Risk: {risk_level} | CVaR: {cvar:.2f}% | PDO: {pdo_signal}")

    # ── 3. Portfolio snapshot (Alpaca) ────────────────────────────────────────
    logger.info("\n  [3/7] Portfolio — current positions...")
    try:
        from alpaca_trader import AlpacaTrader
        trader  = AlpacaTrader()
        acct    = trader.get_account()
        port_val = acct.get("portfolio_value", 0)
        cash     = acct.get("cash", 0)
        deployed = ((port_val - cash) / port_val * 100) if port_val > 0 else 0
        positions_df = trader.get_positions()
        positions_list = []
        if hasattr(positions_df, 'empty') and not positions_df.empty:
            for _, row in positions_df.iterrows():
                positions_list.append({
                    "ticker":   row.get("ticker", "?"),
                    "qty":      row.get("qty", 0),
                    "pnl_pct":  round(float(row.get("unrealized_pct", 0)), 2),
                    "side":     str(row.get("side", "")),
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

    # ── 4. Latest R&D findings ────────────────────────────────────────────────
    logger.info("\n  [4/7] R&D — latest research...")
    rd_report = _safe_load("data/research_team_report.json")
    rd_age_h  = _age_hours(rd_report.get("generated_at", ""))
    picks     = rd_report.get("picks", rd_report.get("recommendations", []))
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

    # ── 5. CoS briefing summary ───────────────────────────────────────────────
    logger.info("\n  [5/7] Chief of Staff — briefing summary...")
    cos_report = _safe_load("data/chief_of_staff_report.json")
    cos_age_h  = _age_hours(cos_report.get("generated_at", ""))
    attention  = cos_report.get("attention_items", [])
    briefing["sections"]["cos"] = {
        "report_age_hours": round(cos_age_h, 1),
        "attention_items":  attention[:5],
        "registry_size":    cos_report.get("registry_size", 0),
        "last_updated":     cos_report.get("generated_at", ""),
    }
    for item in attention[:3]:
        if isinstance(item, dict) and item.get("priority") in ("HIGH", "CRITICAL"):
            briefing["alerts"].append(f"📌 CoS: {item.get('item', '')[:80]}")
    logger.info(f"    CoS: {len(attention)} attention items | Age: {cos_age_h:.1f}h")

    # ── 6. IRM status ─────────────────────────────────────────────────────────
    logger.info("\n  [6/7] IRM — overnight risk check...")
    irm_check = _safe_load("data/irm_daily_check.json")
    irm_age_h = _age_hours(irm_check.get("generated_at", ""))
    panel     = irm_check.get("board_panel", {})
    irm_flags = panel.get("combined_red_flags", [])
    irm_score = panel.get("highest_risk_score", 0)
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

    # ── 7. HR agent health ────────────────────────────────────────────────────
    logger.info("\n  [7/7] HR — agent health check...")
    hr_report = _safe_load("data/hr_report.json")
    action_required = hr_report.get("action_required", [])
    firm_health     = hr_report.get("firm_health_score", 0)
    briefing["sections"]["hr"] = {
        "firm_health_score": firm_health,
        "action_required":   action_required[:5],
        "last_updated":      hr_report.get("generated_at", ""),
    }
    if action_required:
        briefing["alerts"].append(f"👥 HR: {len(action_required)} agent(s) flagged for performance review")
    logger.info(f"    HR: Health {firm_health}/10 | Action required: {len(action_required)}")

    # ── Compile agent status summary ──────────────────────────────────────────
    briefing["agent_status"] = {
        "canary":         {"fresh": _age_hours(canary.get("generated_at","")) < 1},
        "cro":            {"fresh": _age_hours(risk_bulletin.get("generated_at","")) < 12,
                           "risk_level": risk_level},
        "rd_team":        {"fresh": rd_age_h < 12, "picks": len(picks) if picks else 0},
        "cos":            {"fresh": cos_age_h < 24},
        "irm":            {"fresh": irm_age_h < 30, "score": irm_score},
        "hr":             {"fresh": bool(hr_report), "health": firm_health},
    }

    # ── Save briefing ─────────────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    with open(BRIEFING_PATH, "w") as f:
        json.dump(briefing, f, indent=2, default=str)
    logger.info(f"\n  ✅ Morning briefing saved → {BRIEFING_PATH}")

    # ── Log summary ───────────────────────────────────────────────────────────
    logger.info(f"\n  📋 BRIEFING SUMMARY — {today}")
    logger.info(f"  Market: {market_status} | Risk: {risk_level} | IRM: {irm_score}/10")
    if briefing["alerts"]:
        logger.info(f"  ⚠ {len(briefing['alerts'])} alert(s):")
        for alert in briefing["alerts"]:
            logger.info(f"    {alert}")
    else:
        logger.info("  ✅ No alerts — clean bill of health")
    logger.info(f"  Pipeline fires in ~30 minutes. All agents are briefed.")
    logger.info("=" * 60)

    # ── Buffer a brief summary email (not urgent, goes to digest) ─────────────
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

        html = f"""
<div style="font-family:Arial,sans-serif;background:#0d1117;color:#c9d1d9;padding:20px;max-width:700px;">
  <h2 style="color:#58a6ff;border-bottom:2px solid #21262d;padding-bottom:8px;">
    🌅 Morning Briefing — {today}
  </h2>
  <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;">
    <span style="background:#1a2a1a;padding:6px 12px;border-radius:4px;">
      Market: <strong style="color:#3fb950;">{market_status}</strong>
    </span>
    <span style="background:#2a1a1a;padding:6px 12px;border-radius:4px;">
      Risk: <strong style="color:{'#f85149' if risk_level in ('HIGH','CRITICAL') else '#f0883e' if risk_level == 'ELEVATED' else '#3fb950'};">{risk_level}</strong>
    </span>
    <span style="background:#1a1a2a;padding:6px 12px;border-radius:4px;">
      IRM Score: <strong style="color:#58a6ff;">{irm_score}/10</strong>
    </span>
    <span style="background:#1a2a1a;padding:6px 12px;border-radius:4px;">
      Deployed: <strong style="color:#3fb950;">{briefing['sections'].get('portfolio',{}).get('deployed_pct',0):.1f}%</strong>
    </span>
  </div>
  <h3 style="color:#f0883e;margin-bottom:8px;">Pre-Market Alerts</h3>
  <ul style="margin:0 0 16px 0;padding-left:20px;">{alert_rows}</ul>
  {'<h3 style="color:#8b949e;margin-bottom:8px;">Open Positions</h3><table style="width:100%;border-collapse:collapse;font-size:13px;">' + pos_rows + '</table>' if pos_rows else ''}
  <p style="color:#484f58;font-size:11px;margin-top:20px;border-top:1px solid #21262d;padding-top:8px;">
    Pipeline fires at 9:45 AM EDT · All agents have been briefed with this shared context
  </p>
</div>"""

        from email_sender import send_daily_report
        send_daily_report(
            html_body   = html,
            subject     = f"🌅 Morning Briefing — {today} | {risk_level} risk | {len(briefing['alerts'])} alert(s)",
            sender_name = "morning_briefing",
            key_metrics = {
                "Risk Level":  risk_level,
                "IRM Score":   f"{irm_score}/10",
                "Deployed":    f"{briefing['sections'].get('portfolio',{}).get('deployed_pct',0):.1f}%",
                "Alerts":      len(briefing["alerts"]),
            },
        )
    except Exception as e:
        logger.debug(f"Morning briefing email error (non-fatal): {e}")

    return briefing


if __name__ == "__main__":
    run_morning_briefing()
