#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — PRE-MARKET CHECK
# =============================================================================
# Lightweight scan designed to run at ~8:30 AM ET, before market open.
# Board voted unanimous: the daily 4:15 PM pipeline runs AFTER the damage is
# done — a morning scan catches overnight moves before they become stop-loss
# executions.
#
# What it does:
#   1. Fetches overnight / pre-market price moves for every open position.
#   2. Flags positions that are approaching or have already breached
#      stop-loss levels based on after-hours / pre-market data.
#   3. Flags positions with significant overnight gap-down (> ALERT_GAP_PCT).
#   4. Checks global risk proxies (VIX futures, S&P futures via yfinance).
#   5. Saves a structured alert file to data/premarket_alerts.json.
#   6. Emails the Chairman if any CRITICAL or HIGH alerts are found.
#
# Usage:
#   python3 premarket_check.py            # full check + email if alerts found
#   python3 premarket_check.py --no-email # check only, no email
#   python3 premarket_check.py --quiet    # minimal console output
#
# Scheduler: add a 8:30 AM ET cron alongside the existing 4:15 PM run.
# =============================================================================

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    STOP_LOSS_PCT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("premarket_check")

ALERT_OUTPUT_PATH   = "data/premarket_alerts.json"

# Thresholds
ALERT_GAP_PCT       = 3.0   # Flag any overnight gap-down >= 3%
STOP_PROXIMITY_PCT  = 2.0   # Flag position within 2pp of stop-loss trigger
# Risk proxy tickers (pre-market available via yfinance)
VIX_PROXY_TICKER    = "^VIX"
SPY_TICKER          = "SPY"
VIX_ALERT_THRESHOLD = 25.0  # Flag if VIX > 25


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _fetch_overnight_move(ticker: str) -> dict:
    """
    Compare yesterday's close with the latest pre-market price.
    Returns a dict with: last_close, premarket_price, gap_pct, data_available.
    """
    result = {
        "ticker":          ticker,
        "last_close":      None,
        "premarket_price": None,
        "gap_pct":         None,
        "data_available":  False,
    }
    try:
        t   = yf.Ticker(ticker)
        # 5-day 1m data covers pre-market with pre_post=True
        df  = t.history(period="5d", interval="1m", prepost=True)
        if df is None or df.empty:
            return result

        # Identify yesterday's official close (last bar at or before 16:00 ET)
        # yfinance returns UTC; close is around 20:00 UTC for 4 PM ET
        regular_hours = df[~df.index.tz_localize(None).normalize().duplicated(keep=False)]
        # Simpler: last_close = second-to-last day's close price using daily data
        daily = t.history(period="5d", interval="1d", prepost=False)
        if not daily.empty:
            last_close = float(daily["Close"].iloc[-1])
        else:
            return result

        # Latest available price (includes pre-market)
        latest_price = float(df["Close"].iloc[-1])
        gap_pct      = (latest_price - last_close) / last_close * 100

        result.update({
            "last_close":      round(last_close,   2),
            "premarket_price": round(latest_price, 2),
            "gap_pct":         round(gap_pct,      2),
            "data_available":  True,
        })
    except Exception as e:
        logger.debug(f"  Overnight move fetch failed for {ticker}: {e}")
    return result


def _check_vix(threshold: float = VIX_ALERT_THRESHOLD) -> dict:
    """Fetch latest VIX and flag if above threshold."""
    result = {"vix": None, "elevated": False, "data_available": False}
    try:
        df = yf.Ticker(VIX_PROXY_TICKER).history(period="2d", interval="1d")
        if df is not None and not df.empty:
            vix = float(df["Close"].iloc[-1])
            result.update({
                "vix":            round(vix, 2),
                "elevated":       vix > threshold,
                "data_available": True,
            })
    except Exception as e:
        logger.debug(f"  VIX fetch failed: {e}")
    return result


def _check_spy_futures() -> dict:
    """
    Use SPY pre-market price as a proxy for S&P futures direction.
    Returns direction and approximate gap.
    """
    result = {"spy_gap_pct": None, "data_available": False, "direction": "unknown"}
    try:
        spy_move = _fetch_overnight_move(SPY_TICKER)
        if spy_move["data_available"]:
            gap = spy_move["gap_pct"]
            result.update({
                "spy_gap_pct":    gap,
                "data_available": True,
                "direction":      "up" if gap and gap > 0 else "down" if gap and gap < 0 else "flat",
            })
    except Exception as e:
        logger.debug(f"  SPY futures proxy failed: {e}")
    return result


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def run_premarket_check(send_email: bool = True, quiet: bool = False) -> dict:
    """
    Full pre-market scan. Returns the alerts report dict.
    Saves results to data/premarket_alerts.json.
    """
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — PRE-MARKET CHECK")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    from alpaca_trader import AlpacaTrader
    trader = AlpacaTrader()
    snap   = trader.get_portfolio_snapshot()
    acct   = trader.get_account()

    portfolio_value = acct["portfolio_value"]
    positions       = snap["positions"]
    n_positions     = snap["n_positions"]

    logger.info(f"  Portfolio: ${portfolio_value:,.2f}  |  Positions: {n_positions}")

    alerts: list[dict] = []

    # ── Global risk proxies ────────────────────────────────────────────────
    logger.info("  Checking global risk proxies (VIX, SPY futures)...")
    vix_check = _check_vix()
    spy_check = _check_spy_futures()

    if vix_check["elevated"]:
        alerts.append({
            "severity":  "HIGH",
            "category":  "MACRO",
            "ticker":    "VIX",
            "message":   f"VIX elevated at {vix_check['vix']:.1f} (threshold: {VIX_ALERT_THRESHOLD}). "
                         f"Consider defensive posture.",
        })
        logger.warning(f"  ⚠️  VIX elevated: {vix_check['vix']:.1f}")

    if spy_check["data_available"] and spy_check["spy_gap_pct"] is not None:
        spy_gap = spy_check["spy_gap_pct"]
        if spy_gap < -1.5:
            alerts.append({
                "severity":  "HIGH",
                "category":  "MACRO",
                "ticker":    "SPY",
                "message":   f"S&P futures proxy gapping DOWN {spy_gap:+.2f}% pre-market. "
                             f"Risk-off environment — monitor all longs.",
            })
            logger.warning(f"  ⚠️  SPY pre-market gap: {spy_gap:+.2f}%")

    # ── Per-position stop-loss proximity and gap checks ────────────────────
    logger.info(f"  Scanning {n_positions} open position(s) for stop-loss proximity...")

    for pos in positions:
        ticker    = pos["ticker"]
        pnl_pct   = pos["unrealized_pct"]
        avg_entry = pos["avg_entry"]
        side      = pos.get("side", "long")
        is_short  = (pos.get("qty", 0) < 0) or (side == "short")

        stop_loss_threshold = -(STOP_LOSS_PCT * 100)  # e.g. -7.0

        # Check if already below stop (missed by last cycle — EOD race condition)
        if not is_short and pnl_pct <= stop_loss_threshold:
            alerts.append({
                "severity":  "CRITICAL",
                "category":  "STOP_LOSS",
                "ticker":    ticker,
                "message":   f"STOP-LOSS BREACHED: current P&L {pnl_pct:+.1f}% "
                             f"(limit: {stop_loss_threshold:+.1f}%). "
                             f"Pending execution at market open.",
            })
            logger.warning(f"  🔴 STOP BREACHED: {ticker} at {pnl_pct:+.1f}%")
            continue

        # Check stop-loss proximity using overnight move
        overnight = _fetch_overnight_move(ticker)
        if overnight["data_available"] and overnight["gap_pct"] is not None:
            gap = overnight["gap_pct"]

            # If gap-down is large, estimate where P&L will be at open
            projected_pnl = pnl_pct + gap

            if not is_short and projected_pnl <= stop_loss_threshold:
                alerts.append({
                    "severity":  "CRITICAL",
                    "category":  "STOP_LOSS_PROJECTED",
                    "ticker":    ticker,
                    "message":   f"PROJECTED STOP-LOSS BREACH at open: overnight gap {gap:+.2f}% "
                                 f"projects P&L to {projected_pnl:+.1f}% "
                                 f"(limit: {stop_loss_threshold:+.1f}%). Consider pre-open hedge.",
                })
                logger.warning(f"  🔴 PROJECTED STOP: {ticker} — gap {gap:+.2f}%, projected P&L {projected_pnl:+.1f}%")

            elif not is_short and projected_pnl <= (stop_loss_threshold + STOP_PROXIMITY_PCT):
                alerts.append({
                    "severity":  "MEDIUM",
                    "category":  "STOP_PROXIMITY",
                    "ticker":    ticker,
                    "message":   f"Near stop-loss: overnight gap {gap:+.2f}%, "
                                 f"projected P&L {projected_pnl:+.1f}% "
                                 f"(stop at {stop_loss_threshold:+.1f}%, "
                                 f"margin: {projected_pnl - stop_loss_threshold:+.1f}pp).",
                })
                logger.info(f"  ⚠️  NEAR STOP: {ticker} — projected {projected_pnl:+.1f}%, margin {projected_pnl - stop_loss_threshold:+.1f}pp")

            # Flag large gap-downs even if stop not breached
            if gap <= -ALERT_GAP_PCT:
                # Avoid double-alerting tickers already flagged above
                already_alerted = any(a["ticker"] == ticker for a in alerts)
                if not already_alerted:
                    alerts.append({
                        "severity":  "MEDIUM",
                        "category":  "GAP_DOWN",
                        "ticker":    ticker,
                        "message":   f"Large overnight gap-down: {gap:+.2f}% "
                                     f"(threshold: -{ALERT_GAP_PCT:.0f}%). Review thesis.",
                    })
                    logger.info(f"  ⚠️  GAP DOWN: {ticker} {gap:+.2f}%")

    # ── Summary ────────────────────────────────────────────────────────────
    severity_counts = {}
    for a in alerts:
        sev = a["severity"]
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    critical_count = severity_counts.get("CRITICAL", 0)
    high_count     = severity_counts.get("HIGH", 0)
    overall_status = (
        "CRITICAL" if critical_count > 0 else
        "HIGH"     if high_count > 0 else
        "MEDIUM"   if severity_counts.get("MEDIUM", 0) > 0 else
        "CLEAR"
    )

    # ── Fable 5 pre-open game plan — gated to CRITICAL/HIGH mornings ──────
    llm_gameplan = None
    if overall_status in ("CRITICAL", "HIGH"):
        try:
            from apex_llm import reason as _r, is_available
            if is_available("premarket"):
                top_alerts = "; ".join(
                    f"[{a['severity']}] {a['ticker']}: {a['message'][:70]}"
                    for a in alerts[:4])
                llm_gameplan = _r(
                    "You are Apex Capital's pre-market strategist. Markets open in ~1 hour.",
                    f"Status {overall_status} | VIX {vix_check.get('vix','?')} | "
                    f"SPY gap {spy_check.get('spy_gap_pct','?')}%\n"
                    f"Alerts: {top_alerts}\n"
                    "2-3 sentences: the game plan for the open — what to do first and what to avoid.",
                    agent_name="premarket", max_tokens=140,
                )
        except Exception:
            pass

    report = {
        "generated_at":    datetime.now().isoformat(),
        "agent":           "Pre-Market Check",
        **({"llm_gameplan": llm_gameplan.strip()} if llm_gameplan else {}),
        "overall_status":  overall_status,
        "portfolio_value": portfolio_value,
        "n_positions":     n_positions,
        "vix":             vix_check,
        "spy_proxy":       spy_check,
        "severity_counts": severity_counts,
        "alerts":          alerts,
    }

    # Persist
    os.makedirs("data", exist_ok=True)
    with open(ALERT_OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info(f"\n  Status: {overall_status} | {len(alerts)} alert(s) total")
    logger.info(f"  Saved → {ALERT_OUTPUT_PATH}")

    if not quiet:
        _print_alerts(report)

    # ── Email Chairman if there are any HIGH / CRITICAL alerts ─────────────
    if send_email and (critical_count > 0 or high_count > 0):
        _email_premarket_alerts(report)

    return report


def _print_alerts(report: dict) -> None:
    status_icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "CLEAR": "🟢"}.get(
        report["overall_status"], "⚪"
    )
    print(f"\n{'═'*60}")
    print(f"  PRE-MARKET CHECK — {report['generated_at'][:10]}")
    print(f"  Status: {status_icon} {report['overall_status']}")
    if report["vix"]["data_available"]:
        flag = " ⚠️" if report["vix"]["elevated"] else ""
        print(f"  VIX: {report['vix']['vix']:.1f}{flag}")
    if report["spy_proxy"]["data_available"]:
        print(f"  SPY pre-market: {report['spy_proxy']['spy_gap_pct']:+.2f}%")
    print(f"{'─'*60}")
    if not report["alerts"]:
        print("  ✅ No alerts — all positions within parameters.")
    else:
        for a in report["alerts"]:
            icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡"}.get(a["severity"], "⚪")
            print(f"  {icon} [{a['severity']:<8}] {a['ticker']:<6}  {a['message'][:60]}")
    print(f"{'═'*60}\n")


def _email_premarket_alerts(report: dict) -> None:
    """Build and email a pre-market alert digest to the Chairman."""
    try:
        alerts     = report["alerts"]
        status     = report["overall_status"]
        n_crit     = report["severity_counts"].get("CRITICAL", 0)
        n_high     = report["severity_counts"].get("HIGH", 0)

        subject = (
            f"⚠️ Apex Capital — Pre-Market Alert | {status} | "
            f"{n_crit} CRITICAL / {n_high} HIGH | "
            f"{datetime.now().strftime('%b %d, %Y')}"
        )

        severity_color = {
            "CRITICAL": "#c0392b",
            "HIGH":     "#e67e22",
            "MEDIUM":   "#f39c12",
        }

        rows_html = ""
        for a in alerts:
            color = severity_color.get(a["severity"], "#7f8c8d")
            rows_html += f"""
<tr>
  <td style="padding:8px 12px;border-bottom:1px solid #ecf0f1;">
    <span style="background:{color};color:#fff;padding:2px 7px;border-radius:3px;
                 font-size:11px;font-weight:bold;">{a['severity']}</span>
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #ecf0f1;font-weight:bold;">
    {a['ticker']}
  </td>
  <td style="padding:8px 12px;border-bottom:1px solid #ecf0f1;font-size:13px;color:#2c3e50;">
    {a['message']}
  </td>
</tr>"""

        vix_line = (
            f"VIX: <strong>{report['vix']['vix']:.1f}</strong>"
            if report["vix"]["data_available"] else "VIX: N/A"
        )
        spy_line = (
            f"SPY pre-market: <strong>{report['spy_proxy']['spy_gap_pct']:+.2f}%</strong>"
            if report["spy_proxy"]["data_available"] else "SPY: N/A"
        )

        html = f"""
<div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;">
  <h2 style="color:#c0392b;border-bottom:3px solid #c0392b;padding-bottom:8px;">
    ⚠️ Pre-Market Alert — {datetime.now().strftime('%B %d, %Y %H:%M')}
  </h2>
  <p style="font-size:14px;color:#2c3e50;">
    {vix_line} &nbsp;·&nbsp; {spy_line} &nbsp;·&nbsp;
    Portfolio: <strong>${report['portfolio_value']:,.2f}</strong> &nbsp;·&nbsp;
    Positions: <strong>{report['n_positions']}</strong>
  </p>
  <table style="width:100%;border-collapse:collapse;margin-top:16px;">
    <thead>
      <tr style="background:#2c3e50;color:#fff;">
        <th style="padding:8px 12px;text-align:left;">Severity</th>
        <th style="padding:8px 12px;text-align:left;">Ticker</th>
        <th style="padding:8px 12px;text-align:left;">Detail</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="font-size:11px;color:#95a5a6;margin-top:20px;border-top:1px solid #ecf0f1;
            padding-top:8px;">
    Apex Capital Pre-Market Check · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ET<br>
    This alert was generated automatically before market open.
    The full daily pipeline runs at 4:15 PM ET.
  </p>
</div>"""

        from creative_agent import polish_email
        polished = polish_email(
            html, subject, "risk_manager",
            {
                "Status":   status,
                "CRITICAL": n_crit,
                "HIGH":     n_high,
                "Alerts":   len(alerts),
            },
            priority="urgent",
        )
        from email_sender import send_daily_report
        sent = send_daily_report(
            html_body   = polished,
            subject     = subject,
            paper_day   = 0,
            sender_name = "premarket_check",
            key_metrics = {
                "Status":   status,
                "CRITICAL": n_crit,
                "HIGH":     n_high,
                "Alerts":   len(alerts),
            },
        )
        if sent:
            logger.info("  ✅ Pre-market alert email sent (or buffered) to Chairman")
        else:
            logger.warning("  ⚠️  Pre-market alert email failed")

    except Exception as e:
        logger.warning(f"  Pre-market email error: {e}")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('PremarketCheck'):
        raise SystemExit(0)
    import argparse
    parser = argparse.ArgumentParser(description="Apex Capital — Pre-Market Check")
    parser.add_argument("--no-email", action="store_true", help="Skip email even if alerts found")
    parser.add_argument("--quiet",    action="store_true", help="Minimal console output")
    args = parser.parse_args()

    run_premarket_check(
        send_email=not args.no_email,
        quiet=args.quiet,
    )
