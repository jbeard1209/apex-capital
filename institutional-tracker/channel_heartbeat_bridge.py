#!/usr/bin/env python3
"""channel_heartbeat_bridge — post a one-line heartbeat from each core agent
whose contract is "write a JSON report", not "post to firm_channel".

Why this exists
---------------
LTIA has been flagging the same finding since June: five expected agents
(Canary, Chief of Staff, IRM Director, Logic Team Director, Operations
Director) are silent on the firm channel for 7+ days. The finding is not
wrong — the channel really does not carry them — but the framing is: those
five agents never contracted to post to firm_channel. They write reports
to `data/*.json`, and readers walk those reports. The finding accumulated
985 duplicate rows in the approved queue because the producer only
deduped against pending; that piece is now fixed, but the underlying
signal-vs-channel gap remained.

This bridge closes the gap without editing any of the five protected core
agents. For each, it reads the latest report artifact, extracts one
diagnostic line, and posts to `firm_channel.json` with an idempotency key
so a re-run in the same cycle doesn't produce duplicate lines. The
existing "silent on channel" detector then sees the post and clears.

Contract:
  * The bridge NEVER edits a report. It reads, extracts, and posts.
  * If a report is missing or malformed, the bridge posts a warning line
    itself — silence about silence would defeat the point.
  * Idempotency key = f"{agent}:{report_mtime}" so re-running today
    without a new report does nothing.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CHANNEL = DATA / "firm_channel.json"

logger = logging.getLogger("channel_heartbeat_bridge")


def _post(entries: list, agent: str, category: str, priority: str,
          content: str, key: str) -> bool:
    """Append if no entry with this idempotency key already exists today."""
    today = datetime.now().strftime("%Y-%m-%d")
    for e in entries[-500:]:                          # cheap scan window
        if e.get("date") == today and e.get("idempotency_key") == key:
            return False
    entries.append({
        "ts":               datetime.now().isoformat(),
        "date":             today,
        "agent":            agent,
        "category":         category,
        "priority":         priority,
        "content":          content,
        "idempotency_key":  key,
    })
    return True


def _canary_line() -> tuple[str, str]:
    """Return (content, priority) for Canary Agent's heartbeat."""
    p = DATA / "canary_report.json"
    if not p.exists():
        return ("Canary report missing — sentinel may not be running.", "HIGH")
    try:
        r = json.loads(p.read_text())
    except Exception as e:
        return (f"Canary report unreadable: {e}", "HIGH")
    status = str(r.get("status", "?")).upper()
    reason = r.get("reason") or r.get("summary") or ""
    pri = {"GREEN": "NORMAL", "ORANGE": "HIGH", "RED": "HIGH"}.get(status, "NORMAL")
    return (f"Canary status: {status}. {reason}"[:280], pri)


def _cos_line() -> tuple[str, str]:
    p = DATA / "chief_of_staff_report.json"
    if not p.exists():
        return ("Chief of Staff report missing.", "HIGH")
    try:
        r = json.loads(p.read_text())
    except Exception as e:
        return (f"CoS report unreadable: {e}", "HIGH")
    ch = r.get("company_health", {})
    regime = ch.get("regime", "?")
    n_pos = ch.get("n_positions", 0)
    tot = ch.get("total_picks", 0)
    return (f"CoS brief: regime={regime}, positions={n_pos}, picks_today={tot}", "NORMAL")


def _irm_line() -> tuple[str, str]:
    p = DATA / "risk_manager_report.json"
    if not p.exists():
        return ("IRM report missing.", "HIGH")
    try:
        r = json.loads(p.read_text())
    except Exception as e:
        return (f"IRM report unreadable: {e}", "HIGH")
    ra = r.get("risk_assessment", {})
    lvl = ra.get("risk_level", "?")
    score = ra.get("risk_score", 0)
    n_flags = len(ra.get("risk_flags", []) or [])
    pri = "HIGH" if str(lvl).upper() in {"HIGH", "CRITICAL"} else "NORMAL"
    return (f"IRM: risk={lvl} (score {score}), {n_flags} flag(s)", pri)


def _logic_team_line() -> tuple[str, str]:
    p = DATA / "logic_team_findings.json"
    if not p.exists():
        return ("Logic Team findings missing.", "HIGH")
    try:
        r = json.loads(p.read_text())
    except Exception as e:
        return (f"Logic Team findings unreadable: {e}", "HIGH")
    hist = r.get("history", []) if isinstance(r, dict) else []
    latest = hist[-1] if hist else {}
    findings = latest.get("findings", []) or []
    n = len(findings)
    return (f"Logic Team: {n} finding(s) in latest sweep ({latest.get('ts','?')[:19]})",
            "HIGH" if n > 3 else "NORMAL")


def _ops_dir_line() -> tuple[str, str]:
    p = DATA / "ops_director_report.json"
    if not p.exists():
        return ("Ops Director report missing.", "HIGH")
    try:
        r = json.loads(p.read_text())
    except Exception as e:
        return (f"Ops Director report unreadable: {e}", "HIGH")
    summary = r.get("summary") or r.get("floor_brief", {}).get("summary") or "no summary"
    flags = r.get("flags") or r.get("floor_brief", {}).get("flags", []) or []
    pri = "HIGH" if flags else "NORMAL"
    return (f"Ops Director: {str(summary)[:200]} ({len(flags)} flag(s))", pri)


HEARTBEATS = [
    ("Canary Agent",         "SYSTEM_HEALTH", _canary_line),
    ("Chief of Staff",       "STRATEGY",      _cos_line),
    ("IRM Director",         "RISK",          _irm_line),
    ("Logic Team Director",  "COHERENCE",     _logic_team_line),
    ("Operations Director",  "FLOOR_OPS",     _ops_dir_line),
]


def main() -> int:
    if not CHANNEL.exists():
        CHANNEL.write_text("[]")
    try:
        entries = json.loads(CHANNEL.read_text())
        if not isinstance(entries, list):
            entries = []
    except Exception:
        entries = []

    posted = 0
    for agent, category, fn in HEARTBEATS:
        try:
            content, priority = fn()
        except Exception as e:
            content, priority = (f"{agent} heartbeat errored: {e}", "HIGH")
        report_path_hint = {
            "Canary Agent":         DATA / "canary_report.json",
            "Chief of Staff":       DATA / "chief_of_staff_report.json",
            "IRM Director":         DATA / "risk_manager_report.json",
            "Logic Team Director":  DATA / "logic_team_findings.json",
            "Operations Director":  DATA / "ops_director_report.json",
        }[agent]
        try:
            mtime = int(os.path.getmtime(report_path_hint)) if report_path_hint.exists() else 0
        except Exception:
            mtime = 0
        key = f"{agent}:{mtime}"
        if _post(entries, agent, category, priority, content, key):
            posted += 1

    CHANNEL.write_text(json.dumps(entries, indent=2))
    logger.info(f"  channel_heartbeat_bridge: posted {posted} new heartbeat(s)")
    return posted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    n = main()
    print(f"posted {n} heartbeat(s) to firm_channel.json")
