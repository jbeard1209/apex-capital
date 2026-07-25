#!/usr/bin/env python3
"""
APEX CAPITAL -- FIXED INCOME CIT AGENT
========================================
Daily pipeline integrity and improvement agent for the FI pod.
Checks: did fi_pipeline.py run? Are data files fresh? fi_picks_queue.json valid?
Writes improvement proposals to chairman_action_required.json.

Run: python3 fi_cit_agent.py
"""

from __future__ import annotations

import json
import logging
import smtplib
import sys
from datetime import datetime, timedelta, timezone
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

CHAIRMAN_FILE = BASE_DIR / "chairman_action_required.json"
STALE_HOURS   = 25   # file is stale if older than this

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fi_cit_agent")


def _file_age_hours(path: Path) -> float:
    """Return file age in hours, or 999 if not found."""
    if not path.exists():
        return 999.0
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime).total_seconds() / 3600


def run_fi_cit_agent() -> dict:
    logger.info("FI CIT Agent starting...")
    now     = datetime.now(timezone.utc)
    issues  = []
    improvements = []

    # 1. Did fi_pipeline.py run today?
    pipeline_log = DATA_DIR / "fi_pipeline_log.json"
    age_h = _file_age_hours(pipeline_log)
    if age_h > STALE_HOURS:
        issues.append({
            "severity": "HIGH",
            "type":     "PIPELINE_NOT_RUN",
            "message":  f"fi_pipeline.py has not run in {age_h:.1f} hours (threshold {STALE_HOURS}h). "
                        "Check cron at 0 11 * * *.",
        })
    else:
        logger.info("fi_pipeline.py ran %.1f hours ago -- OK", age_h)

    # 2. Are credit spread data files fresh?
    # FI pipeline runs once daily at 11am UTC; FRED cache has a 4h TTL.
    # By 6pm UTC the cache is ~7h old -- that is expected behaviour, not a bug.
    # Only flag MEDIUM if the cache is older than 26 hours (pipeline missed today entirely).
    spread_cache = DATA_DIR / "fi_spread_cache.json"
    cache_age = _file_age_hours(spread_cache)
    if cache_age > 26:
        issues.append({
            "severity": "MEDIUM",
            "type":     "STALE_SPREAD_DATA",
            "message":  f"fi_spread_cache.json is {cache_age:.1f} hours old (threshold 26h). "
                        "FI pipeline may not have run today. Check cron at 0 11 * * *.",
        })

    # 3. fi_picks_queue.json valid?
    picks_file = DATA_DIR / "fi_picks_queue.json"
    picks_age  = _file_age_hours(picks_file)
    if picks_age > STALE_HOURS:
        issues.append({
            "severity": "MEDIUM",
            "type":     "STALE_PICKS_QUEUE",
            "message":  f"fi_picks_queue.json is {picks_age:.1f} hours old. No fresh picks.",
        })
    elif picks_file.exists():
        try:
            with open(picks_file) as f:
                picks_data = json.load(f)
            n_picks = picks_data.get("total_picks", 0)
            regime  = picks_data.get("regime", "UNKNOWN")
            logger.info("fi_picks_queue.json: %d picks, regime=%s", n_picks, regime)
            if n_picks == 0 and regime in ("WIDE", "DISTRESSED"):
                issues.append({
                    "severity": "MEDIUM",
                    "type":     "NO_PICKS_IN_WIDE_REGIME",
                    "message":  f"Zero picks in fi_picks_queue.json despite {regime} regime. "
                                "Agents may be misconfigured or thresholds too high.",
                })
        except Exception as exc:
            issues.append({
                "severity": "HIGH",
                "type":     "CORRUPT_PICKS_FILE",
                "message":  f"fi_picks_queue.json parse error: {exc}",
            })

    # 4. Any FI-specific email loops (check logs for repeated errors)
    for log_name in ["fi_pipeline", "fi_board", "fi_risk_monitor"]:
        log_file = LOGS_DIR / f"{log_name}.log"
        if log_file.exists():
            try:
                text = log_file.read_text(errors="replace")[-5000:]  # last 5KB
                error_count = text.count("ERROR")
                if error_count > 20:
                    issues.append({
                        "severity": "HIGH",
                        "type":     "ERROR_LOOP",
                        "message":  f"{log_name}.log has {error_count} ERROR lines in last 5KB. "
                                    "Possible error loop. Investigate immediately.",
                    })
            except Exception as _e:
                logger.debug("non-fatal: %s", _e)

    # 5. Improvement proposals
    spread_cache_path = DATA_DIR / "fi_spread_cache.json"
    if spread_cache_path.exists():
        try:
            with open(spread_cache_path) as f:
                spreads = json.load(f)
            regime  = spreads.get("spread_regime", "NORMAL")
            hy      = spreads.get("hy_spread", 350)
            ig      = spreads.get("ig_spread", 120)
            t10     = spreads.get("treasury_10yr", 4.0)
            slope   = spreads.get("yield_curve_slope", 0)

            if regime == "WIDE" and hy > 550:
                improvements.append({
                    "type":        "OPPORTUNITY",
                    "priority":    "HIGH",
                    "description": (
                        f"HY spread at {hy:.0f}bps (WIDE regime). "
                        "Historical buying opportunity. Consider increasing HY allocation "
                        "beyond default targets if approved picks are available."
                    ),
                })
            if regime == "DISTRESSED":
                improvements.append({
                    "type":        "OPPORTUNITY",
                    "priority":    "CRITICAL",
                    "description": (
                        f"HY spread at {hy:.0f}bps -- DISTRESSED regime. "
                        "Maximum deployment in HY and IG credit warranted. "
                        "Consider emergency board meeting."
                    ),
                })
            if slope < -0.5 and t10 > 4.5:
                improvements.append({
                    "type":        "DURATION",
                    "priority":    "HIGH",
                    "description": (
                        f"Yield curve steeply inverted (slope={slope:.2f}%) with 10yr={t10:.2f}%. "
                        "Consider maximizing TLT/IEF allocation (duration target: 8yr)."
                    ),
                })
        except Exception as exc:
            logger.warning("Improvement analysis failed: %s", exc)

    # Write to chairman_action_required.json
    _update_chairman_file(issues, improvements)

    result = {
        "run_at":            now.isoformat(),
        "issues":            issues,
        "issue_count":       len(issues),
        "improvements":      improvements,
        "improvement_count": len(improvements),
    }

    cit_file = DATA_DIR / "fi_cit_report.json"
    try:
        with open(cit_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
    except Exception as exc:
        logger.warning("Could not write CIT report: %s", exc)

    logger.info(
        "FI CIT Agent complete. Issues: %d | Improvements: %d",
        len(issues), len(improvements),
    )
    return result


def _update_chairman_file(issues: list, improvements: list):
    """Append FI pod issues and improvements to chairman_action_required.json."""
    existing = {}
    if CHAIRMAN_FILE.exists():
        try:
            with open(CHAIRMAN_FILE) as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    fi_section = existing.get("fi_pod", {})
    fi_section["last_updated"]  = datetime.now(timezone.utc).isoformat()
    fi_section["issues"]        = issues
    fi_section["improvements"]  = improvements
    existing["fi_pod"] = fi_section

    try:
        with open(CHAIRMAN_FILE, "w") as f:
            json.dump(existing, f, indent=2, default=str)
        logger.info("chairman_action_required.json updated with FI pod status")
    except Exception as exc:
        logger.error("Could not update chairman file: %s", exc)


if __name__ == "__main__":
    result = run_fi_cit_agent()
    logger.info(json.dumps(result, indent=2, default=str))
