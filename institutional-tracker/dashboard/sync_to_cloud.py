#!/usr/bin/env python3
"""
APEX CAPITAL — Mac → Cloud Data Sync Script
Runs every 6 hours (via cron) and pushes all JSON data files to the cloud server.
Zero dependencies — uses only Python built-ins.

Usage:
  python3 sync_to_cloud.py

Or add to crontab to run every 6 hours:
  0 */6 * * * python3 "/Users/jackbeard/Claude investment strategy/cloud/sync_to_cloud.py" >> "/Users/jackbeard/Claude investment strategy/cloud/sync.log" 2>&1
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ============================================================
#  ★ CONFIGURATION — update these after deploying to Railway
# ============================================================

# Real deployment URL (was the YOUR-APP-NAME placeholder since creation, so this
# script had NEVER synced — the cloud dashboard showed frozen data). Key comes from
# .env (SYNC_API_KEY) and must match the Railway env var API_KEY; both sides default
# to the same dev value, so sync works out-of-the-box until a custom key is set.
import os as _os
try:
    from dotenv import load_dotenv as _ld
    _ld(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except Exception:
    pass
CLOUD_URL = _os.environ.get("CLOUD_URL", "https://apex-capital-production-17b6.up.railway.app")
API_KEY   = _os.environ.get("SYNC_API_KEY", "change-this-secret-key")

# ============================================================

# __file__ lives at institutional-tracker/dashboard/, so parent.parent IS
# institutional-tracker — the old extra "institutional-tracker" segment double-
# nested the path and every data file read as "missing" (synced 0 files).
BASE_DIR = Path(__file__).parent.parent   # institutional-tracker/
DATA_DIR = BASE_DIR / "data"

DATA_FILES = {
    "state":             "state.json",
    "canary":            "canary_state.json",
    "canary_alert":      "canary_alert.json",
    "portfolio":         "review_recommendations.json",
    "cos":               "chief_of_staff_report.json",
    "pods":              "pod_allocations.json",
    "pod_pnl":           "pod_pnl_report.json",
    "agents":            "agent_registry.json",
    "morning":           "morning_briefing.json",
    "trade_review":      "trade_review_report.json",
    "manager_decisions": "manager_decisions.json",
    "research":          "research_team_report.json",
    "pipeline":          "pipeline_heartbeat.json",
    "etf_board":         "etf_board_meeting.json",
    "issue_tracker":     "issue_tracker.json",
    "hr_report":         "hr_report.json",
    "board_meeting":     "board_meeting_report.json",
    "premarket":         "premarket_alerts.json",
    "execution_quality": "execution_quality.json",
}

def load_json(path: Path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Starting data sync to {CLOUD_URL}")

    if "YOUR-APP-NAME" in CLOUD_URL:
        print("ERROR: Update CLOUD_URL in sync_to_cloud.py with your Railway URL first.")
        sys.exit(1)

    # Collect all data files
    payload = {"data": {}, "synced_at": ts, "source": "mac_cron"}
    loaded, missing = 0, 0
    for key, filename in DATA_FILES.items():
        data = load_json(DATA_DIR / filename)
        if data is not None:
            payload["data"][key] = data
            loaded += 1
        else:
            missing += 1

    print(f"  Loaded {loaded} files, {missing} missing/unreadable")

    # POST to cloud server
    body  = json.dumps(payload).encode()
    req   = urllib.request.Request(
        f"{CLOUD_URL.rstrip('/')}/api/push",
        data    = body,
        headers = {
            "Content-Type": "application/json",
            "X-API-Key":    API_KEY,
        },
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            print(f"  ✓ Push successful: {result.get('keys', '?')} keys pushed at {result.get('pushed_at', '?')}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:200]
        print(f"  ✗ Push failed: HTTP {e.code} — {body_text}")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Push failed: {e}")
        sys.exit(1)

    print(f"  Done.")

if __name__ == "__main__":
    main()
