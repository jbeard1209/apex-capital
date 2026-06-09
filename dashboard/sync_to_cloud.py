#!/usr/bin/env python3
"""
APEX CAPITAL — Mac → Cloud Data Sync Script
Runs every 6 hours (via cron) and pushes all JSON data files + historical
snapshots to the cloud server. History is stored locally in history.json
and grows over time — the cloud always receives the full history on each push.

Usage:
  python3 sync_to_cloud.py

Crontab (every 6 hours):
  0 */6 * * * python3 "/Users/jackbeard/Claude investment strategy/dashboard/sync_to_cloud.py" >> "/Users/jackbeard/Claude investment strategy/dashboard/sync.log" 2>&1
"""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
CLOUD_URL = "https://apex-capital-production-17b6.up.railway.app"
API_KEY   = "apex-2026-secret"
# ============================================================

BASE_DIR     = Path(__file__).parent.parent   # Claude investment strategy/
DATA_DIR     = BASE_DIR / "institutional-tracker" / "data"
HISTORY_FILE = Path(__file__).parent / "history.json"
MAX_SNAPSHOTS = 1500  # ~1 year at 4× daily

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

def build_snapshot(data: dict, ts: str) -> dict:
    """Extract key metrics from current data into a compact historical snapshot."""
    snap = {"ts": ts}

    # Portfolio
    acct = (data.get("portfolio") or {}).get("account") or {}
    snap["portfolio_value"] = acct.get("portfolio_value")
    snap["cash"]            = acct.get("cash")
    snap["n_positions"]     = acct.get("n_positions")
    snap["pnl_today"]       = acct.get("pnl_today")
    snap["pnl_today_pct"]   = acct.get("pnl_today_pct")

    # State
    state = data.get("state") or {}
    snap["paper_day"]       = state.get("paper_day")
    snap["high_water_mark"] = state.get("high_water_mark")

    # CoS company health
    health = (data.get("cos") or {}).get("company_health") or {}
    snap["risk_level"]      = health.get("risk_level")
    snap["manager_grade"]   = health.get("manager_grade")
    snap["manager_score"]   = health.get("manager_score")
    snap["win_rate"]        = health.get("trade_win_rate")
    snap["spy_alpha"]       = health.get("trade_benchmark_delta")
    snap["deployed_pct"]    = health.get("deployed_pct")
    snap["pdo_signal"]      = health.get("pdo_signal")
    snap["trades_exec"]     = health.get("trades_executed")
    snap["trades_rej"]      = health.get("trades_rejected")

    # Canary
    canary = data.get("canary") or {}
    snap["canary_level"]    = canary.get("last_level")
    snap["canary_regime"]   = canary.get("regime")

    # Pods
    pod_pnl = (data.get("pod_pnl") or {}).get("pods") or {}
    for pod_key, pod_label in [("pod1_rd","p1"),("pod2_ml","p2"),("pod3_macro","p3")]:
        p = pod_pnl.get(pod_key) or {}
        snap[f"{pod_label}_value"]      = p.get("total_value")
        snap[f"{pod_label}_positions"]  = p.get("n_positions")
        snap[f"{pod_label}_pnl_today"]  = p.get("pnl_today")
        snap[f"{pod_label}_deployed"]   = p.get("positions_pct")
        snap[f"{pod_label}_cash"]       = p.get("cash_balance")

    return snap

def load_history() -> list:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_history(history: list):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f)

def main():
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ts_display = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts_display}] Starting data sync to {CLOUD_URL}")

    if "YOUR-APP-NAME" in CLOUD_URL:
        print("ERROR: Update CLOUD_URL in sync_to_cloud.py first.")
        sys.exit(1)

    # Collect all current data files
    current_data = {}
    loaded, missing = 0, 0
    for key, filename in DATA_FILES.items():
        d = load_json(DATA_DIR / filename)
        if d is not None:
            current_data[key] = d
            loaded += 1
        else:
            missing += 1
    print(f"  Loaded {loaded} files, {missing} missing/unreadable")

    # Build and append snapshot to local history
    snapshot = build_snapshot(current_data, ts)
    history  = load_history()

    # Only add a new snapshot if it's meaningfully different (avoid duplicates
    # if the script runs twice in quick succession)
    should_append = True
    if history:
        last_snap = history[-1]
        last_val  = last_snap.get("portfolio_value")
        curr_val  = snapshot.get("portfolio_value")
        if last_val == curr_val and last_snap.get("ts", "")[:13] == ts[:13]:
            should_append = False  # same hour, same value — skip

    if should_append:
        history.append(snapshot)
        # Trim to max
        if len(history) > MAX_SNAPSHOTS:
            history = history[-MAX_SNAPSHOTS:]
        save_history(history)
        print(f"  History: {len(history)} snapshots saved locally")

    # Push current data + full history to cloud
    payload = {
        "data":      current_data,
        "history":   history,
        "synced_at": ts,
        "source":    "mac_cron",
    }
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"{CLOUD_URL.rstrip('/')}/api/push",
        data    = body,
        headers = {"Content-Type": "application/json", "X-API-Key": API_KEY},
        method  = "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            print(f"  ✓ Push successful — {result.get('keys','?')} data keys, "
                  f"{result.get('history_snapshots','?')} history snapshots")
    except urllib.error.HTTPError as e:
        print(f"  ✗ Push failed: HTTP {e.code} — {e.read().decode()[:200]}")
        sys.exit(1)
    except Exception as e:
        print(f"  ✗ Push failed: {e}")
        sys.exit(1)

    print(f"  Done.")

if __name__ == "__main__":
    main()
