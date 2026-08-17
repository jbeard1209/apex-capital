#!/usr/bin/env python3
"""
APEX CAPITAL -- FIXED INCOME RISK MONITOR
==========================================
Daily risk monitoring for the FI pod.
Checks: duration risk, credit concentration, spread duration,
liquidity, correlation with equity pods.

Run: python3 fi_risk_monitor.py
"""

from __future__ import annotations

import json
import logging
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

from config import EMAIL_SENDER, GMAIL_APP_PASSWORD
EMAIL_RECIPIENT = "jack.beard07@gmail.com"
APP_PASSWORD    = GMAIL_APP_PASSWORD
SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 465

FI_CAPITAL              = 30000.00
MAX_ISSUER_CONCENTRATION = 0.10   # 10% per issuer
MAX_POSITION_PCT         = 0.20   # 20% per position
DURATION_TARGET_NORMAL   = 5.0
DURATION_TARGET_INVERTED = 8.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fi_risk_monitor")


def run_fi_risk_monitor() -> dict:
    from fi_data_layer import get_credit_spreads

    logger.info("FI Risk Monitor starting...")
    spreads = get_credit_spreads()
    regime  = spreads["spread_regime"]
    curve_slope = spreads["yield_curve_slope"]
    curve_inverted = curve_slope < 0

    duration_target = DURATION_TARGET_INVERTED if curve_inverted else DURATION_TARGET_NORMAL
    risk_flags = []

    # Load approved picks for portfolio view. Through the shared loader — the
    # old data["picks"] read returned [] against a file keyed "approved", so
    # every risk metric below was computed over an empty portfolio and every
    # threshold came back clean. A risk monitor that sees nothing reports no risk.
    from fi_data_layer import load_approved_picks
    approved_file = DATA_DIR / "fi_approved_picks.json"
    try:
        approved_picks = load_approved_picks(approved_file)
    except Exception as exc:
        logger.error("Could not load approved picks: %s", exc)
        approved_picks = []

    total_deployed = sum(p.get("position_size_usd", 0) for p in approved_picks)

    # 1. Duration risk
    weighted_duration = 0.0
    DURATION_MAP = {
        "TLT": 16.5, "IEF": 7.5, "SHY": 1.9,
        "LQD": 8.4,  "HYG": 4.2, "JNK": 4.0,
        "AGG": 6.0,  "BND": 6.0,
    }
    if total_deployed > 0:
        for p in approved_picks:
            ticker = p.get("ticker", "")
            dur = DURATION_MAP.get(ticker, 5.0)
            weight = p.get("position_size_usd", 0) / total_deployed
            weighted_duration += dur * weight

    duration_diff = abs(weighted_duration - duration_target)
    if duration_diff > 3.0:
        risk_flags.append({
            "type":    "DURATION_MISMATCH",
            "message": (
                f"Portfolio weighted duration {weighted_duration:.1f}yr vs "
                f"target {duration_target:.1f}yr (diff={duration_diff:.1f}yr). "
                "Rebalancing recommended."
            ),
        })

    # 2. Credit concentration
    issuer_exposure = {}
    for p in approved_picks:
        ticker = p.get("ticker", "")
        pos    = p.get("position_size_usd", 0)
        issuer_exposure[ticker] = issuer_exposure.get(ticker, 0) + pos

    for issuer, exposure in issuer_exposure.items():
        pct = exposure / FI_CAPITAL
        if pct > MAX_ISSUER_CONCENTRATION:
            risk_flags.append({
                "type":    "CONCENTRATION_BREACH",
                "message": (
                    f"Issuer {issuer}: ${exposure:,.0f} = {pct*100:.1f}% "
                    f"of FI capital (max {MAX_ISSUER_CONCENTRATION*100:.0f}%). "
                    "Reduce position."
                ),
            })

    # 3. Position size check
    for p in approved_picks:
        pos_pct = p.get("position_size_usd", 0) / FI_CAPITAL
        if pos_pct > MAX_POSITION_PCT:
            risk_flags.append({
                "type":    "OVERSIZED_POSITION",
                "message": (
                    f"{p.get('ticker','?')} position {pos_pct*100:.1f}% "
                    f"exceeds max {MAX_POSITION_PCT*100:.0f}%"
                ),
            })

    # 4. Liquidity check
    illiquid_picks = [
        p for p in approved_picks
        if p.get("category", "etf").lower() not in ("etf", "treasury")
        and p.get("ticker") not in {
            "HYG", "JNK", "LQD", "TLT", "IEF", "AGG", "BND",
            "CWB", "PFF", "PFFD", "SHY", "SGOV", "BIL",
            "VCIT", "USIG", "USHY", "ICVT", "PGX",
        }
    ]
    if illiquid_picks:
        risk_flags.append({
            "type":    "LIQUIDITY_WARNING",
            "message": (
                f"{len(illiquid_picks)} potentially illiquid positions detected. "
                "FI pod policy: ETF-only for now. Review: "
                + ", ".join(p.get("ticker","?") for p in illiquid_picks)
            ),
        })

    # 5. Correlation with equity pods
    # Check if equity pod also broadly long risk
    equity_approved_file = DATA_DIR / "fe_approved_picks.json"
    if equity_approved_file.exists():
        try:
            eq_picks = load_approved_picks(equity_approved_file)
            if len(eq_picks) > 5 and len(approved_picks) > 3 and regime in ("WIDE", "DISTRESSED"):
                risk_flags.append({
                    "type":    "CORRELATION_WARNING",
                    "message": (
                        f"Both FI pod ({len(approved_picks)} picks) and equity pod "
                        f"({len(eq_picks)} picks) are broadly long risk in {regime} regime. "
                        "Correlated drawdown risk. Monitor closely."
                    ),
                })
        except Exception as _e:
            logger.debug("non-fatal: %s", _e)

    result = {
        "run_at":             datetime.now(timezone.utc).isoformat(),
        "regime":             regime,
        "curve_inverted":     curve_inverted,
        "yield_curve_slope":  curve_slope,
        "duration_target":    duration_target,
        "weighted_duration":  round(weighted_duration, 2),
        "total_deployed_usd": round(total_deployed, 2),
        "deployment_pct":     round(total_deployed / FI_CAPITAL * 100, 1),
        "approved_picks":     len(approved_picks),
        "risk_flags":         risk_flags,
        "risk_flag_count":    len(risk_flags),
    }

    risk_file = DATA_DIR / "fi_risk_monitor.json"
    try:
        with open(risk_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info("Risk monitor written -> %s", risk_file)
    except Exception as exc:
        logger.warning("Could not write risk file: %s", exc)

    if risk_flags:
        _send_risk_email(result, risk_flags)

    logger.info(
        "Risk monitor complete. Flags: %d | Duration: %.1fyr (target %.1fyr) | "
        "Deployed: %.1f%%",
        len(risk_flags), weighted_duration, duration_target,
        result["deployment_pct"],
    )
    return result


def _send_risk_email(result, risk_flags):
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject  = f"Apex Capital -- Fixed Income Risk Alert | {date_str} | {len(risk_flags)} Flag(s)"

    lines = [
        "APEX CAPITAL -- FIXED INCOME RISK MONITOR ALERT",
        "=" * 55,
        f"Date:              {date_str}",
        f"Regime:            {result['regime']}",
        f"Curve:             {'INVERTED' if result['curve_inverted'] else 'NORMAL'} "
        f"(slope={result['yield_curve_slope']:.3f}%)",
        f"Duration (actual): {result['weighted_duration']:.1f}yr",
        f"Duration (target): {result['duration_target']:.1f}yr",
        f"Deployed:          ${result['total_deployed_usd']:,.0f} ({result['deployment_pct']:.1f}%)",
        "",
        f"RISK FLAGS ({len(risk_flags)} total)",
        "-" * 40,
    ]
    for flag in risk_flags:
        lines.append(f"  [{flag['type']}] {flag['message']}")
    lines.append("")
    lines.append("Apex Capital Fixed Income Risk Monitor")

    body = "\n".join(lines)
    try:
        msg = MIMEText(body, "plain", "us-ascii")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_RECIPIENT
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, APP_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        logger.info("Risk alert email sent.")
    except Exception as exc:
        logger.error("Risk email failed: %s", exc)


if __name__ == "__main__":
    result = run_fi_risk_monitor()
    logger.info(json.dumps(result, indent=2, default=str))
