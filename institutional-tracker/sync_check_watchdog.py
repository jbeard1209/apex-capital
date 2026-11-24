#!/usr/bin/env python3
"""
APEX CAPITAL — WATCHDOG FOR THE MAC/GITHUB/VM SYNC CHECK (VM, hourly)
=========================================================================
tools/sync_drift_check.py runs on the Mac (launchd, hourly) and is the only
thing that compares the Mac tree, GitHub main and the VM's deployed files. It
raises drift to the Chairman queue and the digest. But nothing watched IT: if
the Mac job stops (unloaded agent, broken python, Mac off for days), drift can
build up again and the only witness is silent.

The job publishes sync_status.json to the private apex-reports repo, and
re-pushes it at least every 3 hours even when nothing changed (HEARTBEAT_S).
So its `generated_at` is a liveness signal readable from a host that is
always on. This runs on the VM, reads that file, and:

    FRESH        generated_at younger than STALE_H        -> resolve any alarm
    STALE        older than STALE_H                       -> Chairman queue + digest
    CANNOT_TELL  clone failed / file missing / unreadable -> alarm only after
                 CANNOT_TELL_RUNS in a row (one GitHub blip is not an outage),
                 and never resolves anything

STALE_H is 18h, not 4h: the Mac is a laptop, and a closed lid overnight stops
launchd. 18h = a long night plus the 3h heartbeat with margin; a job that is
really dead is reported the next morning, not a week later.

The status also carries the Mac job's own verdict. If it says DRIFT, the Mac
already raised it on its own queue; this repeats it on the VM's queue only
when the drift involves the VM (vm_differs / vm_missing) and is past grace, so
the host that runs the firm carries the warning too.

Scheduled by riding kalshi_report_publish.py (hourly at :55 on the VM), not a
cron line of its own. Read-only apart from data/sync_watchdog_state.json, data/sync_watchdog_report.json
and the Chairman queue. Uses the same config and deploy key as
kalshi_report_publish.py (data/kalshi_report_publish.json).

Usage:
    ~/venv/bin/python3 sync_check_watchdog.py            # check + alert
    ~/venv/bin/python3 sync_check_watchdog.py --no-alert # check, print only
"""

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
CONFIG_FILE = DATA / "kalshi_report_publish.json"
STATE_FILE = DATA / "sync_watchdog_state.json"
REPORT_FILE = DATA / "sync_watchdog_report.json"
QUEUE_FILE = DATA / "chairman_action_required.json"
STATUS_NAME = "sync_status.json"
SOURCE = "sync_check_watchdog"

STALE_H = 18.0
CANNOT_TELL_RUNS = 3
VM_KINDS = ("vm_differs", "vm_missing")


def _now():
    return datetime.now(timezone.utc)


def evaluate(status_text, now, stale_h=STALE_H):
    """Pure verdict on the published status text (None = could not fetch)."""
    if status_text is None:
        return {"verdict": "CANNOT_TELL", "why": "sync_status.json could not be fetched"}
    try:
        doc = json.loads(status_text)
        gen = datetime.fromisoformat(doc["generated_at"])
        if gen.tzinfo is None:
            raise ValueError("generated_at has no timezone")
    except (ValueError, KeyError, TypeError) as e:
        return {"verdict": "CANNOT_TELL",
                "why": "sync_status.json unreadable: %s: %s" % (type(e).__name__, e)}
    age_h = (now - gen).total_seconds() / 3600.0
    vm_drift = [i for i in doc.get("issues") or []
                if i.get("kind") in VM_KINDS and i.get("due")]
    out = {"generated_at": doc["generated_at"], "age_h": round(age_h, 1),
           "mac_verdict": doc.get("verdict"), "vm_drift": vm_drift}
    if age_h >= stale_h:
        out.update(verdict="STALE",
                   why="the Mac sync check last published %.0fh ago (limit %.0fh)"
                       % (age_h, stale_h))
    else:
        out.update(verdict="FRESH", why="published %.1fh ago" % age_h)
    return out


def fetch_status(cfg):
    """Shallow-clone apex-reports with the VM's deploy key; return the status
    text, or None on any failure (the caller decides what None means)."""
    import kalshi_report_publish as krp
    remote = cfg.get("remote")
    if not remote:
        return None
    tmp = tempfile.mkdtemp(prefix="sync-watchdog-")
    try:
        r = krp._run(["git", "clone", "--quiet", "--depth", "1", remote, tmp],
                     env=krp._git_env(cfg.get("ssh_key")))
        if r.returncode != 0:
            return None
        p = Path(tmp) / STATUS_NAME
        return p.read_text() if p.exists() else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _entry(key, now, title, body):
    return {
        "id": "sync-watchdog-%s-%s" % (now.strftime("%Y%m%d"), key),
        "dedup_key": "sync_watchdog:%s" % key,
        "severity": "MEDIUM",
        "category": "INFRASTRUCTURE",
        "status": "awaiting_chairman",
        "raised_at": now.isoformat(timespec="seconds"),
        "description": "%s\n\n%s\n\nRaised by sync_check_watchdog.py on the VM; it "
                       "resolves itself once the check is fresh again." % (title, body),
    }


WHAT_TO_DO_STALE = (
    "WHAT TO DO: open the Mac. If it was simply off, this clears within the hour. "
    "If not, ask a session: 'the sync drift check stopped; fix it' "
    "(launchctl list | grep syncdrift; tail institutional-tracker/logs/"
    "syncdrift_launchd.log).")


def decide(result, state, now):
    """Return (raise_keys -> entry, resolve_keys, new_state). Pure."""
    state = dict(state or {})
    raise_, resolve = {}, []
    v = result["verdict"]
    if v == "CANNOT_TELL":
        state["cannot_tell_runs"] = int(state.get("cannot_tell_runs", 0)) + 1
        if state["cannot_tell_runs"] >= CANNOT_TELL_RUNS:
            raise_["cannot_tell"] = _entry(
                "cannot_tell", now,
                "Sync watchdog cannot read the Mac's sync status (%d runs in a row)"
                % state["cannot_tell_runs"],
                result["why"] + "\n\n" + WHAT_TO_DO_STALE)
        return raise_, resolve, state           # silence never resolves anything
    state["cannot_tell_runs"] = 0
    resolve.append("sync_watchdog:cannot_tell")
    if v == "STALE":
        raise_["stale"] = _entry(
            "stale", now, "The Mac/VM sync check has stopped running",
            result["why"] + ". Until it runs, nobody checks that the Mac, GitHub "
            "and the VM hold the same code.\n\n" + WHAT_TO_DO_STALE)
    else:
        resolve.append("sync_watchdog:stale")
    if v == "FRESH" and result.get("vm_drift"):
        kinds = ", ".join(sorted(i["kind"] for i in result["vm_drift"]))
        raise_["vm_drift"] = _entry(
            "vm_drift", now, "The VM runs different code from GitHub main (%s)" % kinds,
            "Reported by the Mac's sync check at %s. WHAT TO DO: tell a session "
            "'deploy' (tools/ship.sh --deploy-only)." % result.get("generated_at"))
    elif v == "FRESH":
        resolve.append("sync_watchdog:vm_drift")
    return raise_, resolve, state


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    now = _now()
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        cfg = {}
    result = evaluate(fetch_status(cfg) if isinstance(cfg, dict) else None, now)
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    raise_, resolve, new_state = decide(result, state, now)
    result["checked_at"] = now.isoformat(timespec="seconds")
    alert = {"raised": [], "resolved": [], "email": None}
    if "--no-alert" not in argv:
        import chairman_queue
        for key, entry in raise_.items():
            added, _ = chairman_queue.submit([entry], SOURCE, path=str(QUEUE_FILE))
            if added:
                alert["raised"].append(key)
        done = chairman_queue.resolve(resolve, SOURCE, path=str(QUEUE_FILE),
                                      note="sync_check_watchdog: check is fresh again")
        alert["resolved"] = [e.get("dedup_key") for e in (done or [])]
        if alert["raised"]:
            try:
                from email_sender import send_alert_email
                body = "".join("<p><b>%s</b></p><pre>%s</pre>"
                               % (raise_[k]["description"].split("\n")[0],
                                  raise_[k]["description"]) for k in alert["raised"])
                send_alert_email("Sync check: %s" % ", ".join(alert["raised"]), body)
                alert["email"] = "sent/buffered"
            except Exception as e:
                alert["email"] = "FAILED (queue entry still raised): %s: %s" % (
                    type(e).__name__, e)
        STATE_FILE.write_text(json.dumps(new_state, indent=1))
    result["alert"] = alert
    REPORT_FILE.write_text(json.dumps(result, indent=1, default=str))
    print("%s sync watchdog: %s (%s) alert=%s" % (
        result["checked_at"], result["verdict"], result["why"], alert))
    return {"FRESH": 0, "STALE": 1}.get(result["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
