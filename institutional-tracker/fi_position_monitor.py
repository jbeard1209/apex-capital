#!/usr/bin/env python3
"""
APEX CAPITAL -- FIXED INCOME POSITION MONITOR
===============================================
Quarterly review of FI positions.
Checks: credit quality drift, spread compression, duration rebalancing.
Runs quarterly: Jan/Apr/Jul/Oct 1st at 10am UTC.

Run: python3 fi_position_monitor.py
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

FI_CAPITAL   = 30000.00
HY_TIGHT_BPS = 300   # sell signal: HY spread back below this
IG_TIGHT_BPS = 100   # sell signal: IG spread back below this

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fi_position_monitor")


def run_fi_position_monitor() -> dict:
    from fi_data_layer import get_credit_spreads, get_issuer_credit_quality

    logger.info("FI Position Monitor (Quarterly Review) starting...")
    spreads = get_credit_spreads()
    regime  = spreads["spread_regime"]
    hy      = spreads["hy_spread"]
    ig      = spreads["ig_spread"]

    # Load approved positions
    approved_file = DATA_DIR / "fi_approved_picks.json"
    positions = []
    if approved_file.exists():
        try:
            with open(approved_file) as f:
                data = json.load(f)
            positions = data.get("picks", [])
        except Exception as exc:
            logger.warning("Could not load positions: %s", exc)

    logger.info("Reviewing %d positions. Regime: %s", len(positions), regime)

    actions = []
    for pos in positions:
        ticker   = pos.get("ticker", "?")
        category = pos.get("category", "etf").lower()
        pos_size = pos.get("position_size_usd", 0)
        is_hy    = ticker in {"HYG", "JNK", "USHY"}
        is_ig    = ticker in {"LQD", "VCIT", "USIG"}
        is_dur   = ticker in {"TLT", "IEF", "SHY", "SGOV", "BIL"}

        action = {"ticker": ticker, "position_usd": pos_size, "actions": []}

        # 1. Spread compression (take profit)
        if is_hy and hy < HY_TIGHT_BPS:
            action["actions"].append({
                "type":   "TAKE_PROFIT",
                "reason": f"HY spread compressed to {hy:.0f}bps (< {HY_TIGHT_BPS}bps TIGHT threshold). "
                          "Spread-based thesis has played out. Consider trimming.",
            })
        if is_ig and ig < IG_TIGHT_BPS:
            action["actions"].append({
                "type":   "TAKE_PROFIT",
                "reason": f"IG spread compressed to {ig:.0f}bps (< {IG_TIGHT_BPS}bps threshold). "
                          "Risk/reward now unfavorable for IG corporates.",
            })

        # 2. Credit quality re-check for individual issuers
        if category not in ("etf", "treasury") and ticker not in {
            "HYG", "JNK", "LQD", "TLT", "IEF", "AGG", "BND",
            "CWB", "PFF", "PFFD", "SHY", "SGOV", "BIL",
            "VCIT", "USIG", "USHY", "ICVT", "PGX",
        }:
            try:
                cq = get_issuer_credit_quality(ticker)
                score = cq.get("credit_score", 50)
                tier  = cq.get("credit_tier", "CROSSOVER")
                trend = cq.get("leverage_trend", "UNKNOWN")
                if score < 30 or tier == "DISTRESSED":
                    action["actions"].append({
                        "type":   "SELL_SIGNAL",
                        "reason": (
                            f"Credit quality deteriorated: score={score}, tier={tier}, "
                            f"leverage_trend={trend}. Consider selling to avoid permanent loss."
                        ),
                    })
                elif trend == "WORSENING":
                    action["actions"].append({
                        "type":   "WATCH",
                        "reason": f"Leverage trend WORSENING (score={score}). Monitor closely.",
                    })
            except Exception as exc:
                logger.warning("Credit re-check %s: %s", ticker, exc)

        # 3. Duration rebalancing
        if is_dur:
            curve_inverted = spreads["yield_curve_slope"] < 0
            t10 = spreads["treasury_10yr"]
            if not curve_inverted and t10 < 4.0:
                action["actions"].append({
                    "type":   "DURATION_TRIM",
                    "reason": (
                        f"Curve not inverted and 10yr={t10:.2f}% < 4.0% minimum. "
                        "Duration thesis weakened. Consider reducing TLT/IEF allocation."
                    ),
                })

        if action["actions"]:
            actions.append(action)
            for a in action["actions"]:
                logger.info("%s: [%s] %s", ticker, a["type"], a["reason"])

    result = {
        "run_at":     datetime.now(timezone.utc).isoformat(),
        "quarter":    f"Q{(datetime.now().month-1)//3 + 1} {datetime.now().year}",
        "regime":     regime,
        "spreads":    spreads,
        "positions_reviewed": len(positions),
        "positions_with_actions": len(actions),
        "actions":    actions,
    }

    monitor_file = DATA_DIR / "fi_position_monitor.json"
    try:
        with open(monitor_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("Could not write position monitor file: %s", exc)

    _send_quarterly_email(result, actions)
    logger.info("Quarterly review complete. Actions: %d", len(actions))
    return result


def _send_quarterly_email(result, actions):
    subject = f"Apex Capital -- Fixed Income Quarterly Review | {result['quarter']}"

    lines = [
        "APEX CAPITAL -- FIXED INCOME QUARTERLY REVIEW",
        "=" * 55,
        f"Quarter: {result['quarter']}",
        f"Regime:  {result['regime']}",
        f"HY Spread: {result['spreads']['hy_spread']:.0f}bps",
        f"IG Spread: {result['spreads']['ig_spread']:.0f}bps",
        f"10yr Treasury: {result['spreads']['treasury_10yr']:.2f}%",
        "",
        f"Positions reviewed: {result['positions_reviewed']}",
        f"Positions with actions: {result['positions_with_actions']}",
        "",
    ]

    if actions:
        lines += ["RECOMMENDED ACTIONS", "-" * 40]
        for action in actions:
            lines.append(f"  {action['ticker']} (${action['position_usd']:,.0f})")
            for a in action["actions"]:
                lines.append(f"    [{a['type']}] {a['reason']}")
            lines.append("")
    else:
        lines.append("No actions required. Portfolio in good standing.")
        lines.append("")

    lines.append("Apex Capital Fixed Income Position Monitor")
    body = "\n".join(lines)
    try:
        msg = MIMEText(body, "plain", "us-ascii")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_RECIPIENT
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(EMAIL_SENDER, APP_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        logger.info("Quarterly email sent.")
    except Exception as exc:
        logger.error("Quarterly email failed: %s", exc)


if __name__ == "__main__":
    result = run_fi_position_monitor()
    logger.info(json.dumps(result, indent=2, default=str))
