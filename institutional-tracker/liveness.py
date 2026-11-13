#!/usr/bin/env python3
"""
APEX CAPITAL -- OUTCOME LIVENESS (audit module M00)
===================================================
Why this exists: July-November 2026 failures all had one shape -- a feature ran,
exited 0, rewrote its report, and did NOT do its job (board voted on 0 proposals
for 12 weeks; deputy board decided nothing for a week; approvals never executed).
Monitors asked "did a file change?" -- never "did the outcome happen, as often as
it should?"  See docs/audit/LIVENESS_GAPS.md.

Three parts:
  1. beat(feature, n_done, n_examined)  -- a feature calls this when it did its JOB
     ("voted on 4 of 4"), not when it started. Writes data/heartbeats/<feature>.json.
  2. config/liveness.json -- the registry: per feature max_silence_hours, min_n_done,
     and `applies_if` (optional heartbeat/path gate so "0 done" is only a failure when
     work was pending).
  3. check() / `python3 liveness.py` -- the watcher. Alarms on: missing heartbeat,
     stale heartbeat, or a fresh heartbeat with n_done < min_n_done (the crashed-vote
     shape: alive but did nothing).  Registered features that nobody instruments yet
     are reported as UNINSTRUMENTED, never as healthy. The watcher beats itself and
     pings APEX_LIVENESS_HC_URL (healthchecks.io dead-man's switch) so that if the
     watcher dies, an external service tells Jack.

Per-host: heartbeats are host-local data/ (no state sync between hosts). A registry
entry names `hosts` it is expected on; the watcher only judges those.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
REGISTRY = HERE / "config" / "liveness.json"


def _hb_dir():
    return Path(os.environ.get("APEX_HEARTBEAT_DIR", DATA / "heartbeats"))


def _host():
    return os.environ.get("APEX_HOST_ID", "") or os.uname().nodename


def beat(feature, n_done, n_examined, now=None):
    """Record that `feature` did its job. Never raises (must not break a pipeline)."""
    try:
        d = _hb_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {"feature": feature, "n_done": int(n_done), "n_examined": int(n_examined),
               "host": _host(),
               "ts": (now or datetime.now(timezone.utc)).isoformat()}
        tmp = d / (feature + ".json.tmp")
        tmp.write_text(json.dumps(rec))
        os.replace(tmp, d / (feature + ".json"))
        return True
    except Exception:
        return False


def _read_hb(feature):
    try:
        return json.loads((_hb_dir() / (feature + ".json")).read_text())
    except Exception:
        return None


def _age_hours(rec, now):
    try:
        ts = datetime.fromisoformat(rec["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0
    except Exception:
        return None


def check(registry=None, now=None, host=None):
    """Judge every registered feature. Returns {'n_examined', 'alarms', 'results'}.

    status per feature: OK | MISSING | STALE | NO_OUTCOME | UNINSTRUMENTED | SKIPPED
    Only MISSING/STALE/NO_OUTCOME are alarms. UNINSTRUMENTED means "registered but
    nothing writes its heartbeat yet" -- reported loudly, counted, but not paged on
    (it would page forever on day one); it is the burn-down list.
    """
    now = now or datetime.now(timezone.utc)
    host = host or _host()
    if registry is None:
        registry = json.loads(REGISTRY.read_text())
    feats = registry.get("features", {})
    results, alarms = {}, []
    for name, spec in feats.items():
        hosts = spec.get("hosts")
        if hosts and host not in hosts:
            results[name] = {"status": "SKIPPED", "why": "not expected on " + host}
            continue
        if now.weekday() >= 5 and spec.get("weekdays_only"):
            results[name] = {"status": "SKIPPED", "why": "weekend"}
            continue
        rec = _read_hb(name)
        if rec is None:
            st = "UNINSTRUMENTED" if not spec.get("instrumented") else "MISSING"
            results[name] = {"status": st}
            if st == "MISSING":
                alarms.append((name, "no heartbeat file but feature is marked instrumented"))
            continue
        age = _age_hours(rec, now)
        if age is None or age > float(spec["max_silence_hours"]):
            results[name] = {"status": "STALE", "age_h": age}
            alarms.append((name, "last outcome %s h ago (limit %s h)"
                           % ("unknown" if age is None else round(age, 1),
                              spec["max_silence_hours"])))
        elif rec.get("n_done", 0) < spec.get("min_n_done", 1) and rec.get("n_examined", 0) > 0:
            # Alive, looked at work, did none of it: the crashed-vote shape.
            results[name] = {"status": "NO_OUTCOME", "age_h": age,
                             "n_done": rec.get("n_done"), "n_examined": rec.get("n_examined")}
            alarms.append((name, "ran but did %s of %s (min %s)"
                           % (rec.get("n_done"), rec.get("n_examined"), spec.get("min_n_done", 1))))
        else:
            results[name] = {"status": "OK", "age_h": age}
    return {"n_examined": len(feats), "alarms": alarms, "results": results,
            "n_uninstrumented": sum(1 for r in results.values() if r["status"] == "UNINSTRUMENTED")}


def _escalate(report):
    try:
        import chairman_queue
        entries = [{"severity": "HIGH", "status": "awaiting_chairman",
                    "dedup_key": "liveness:" + n,
                    "description": "LIVENESS: %s -- %s" % (n, why)}
                   for n, why in report["alarms"]]
        if entries:
            chairman_queue.submit(entries, "liveness")
    except Exception:
        pass


def main():
    rep = check()
    if "--no-alert" not in sys.argv:
        _escalate(rep)
    # Watcher beats itself: 'done' = features it judged.
    beat("liveness_watcher", rep["n_examined"] - rep["n_uninstrumented"], rep["n_examined"])  # external HC ping is the real watch-the-watcher
    print("liveness: examined=%d alarms=%d uninstrumented=%d"
          % (rep["n_examined"], len(rep["alarms"]), rep["n_uninstrumented"]))
    for n, why in rep["alarms"]:
        print("  ALARM", n, "-", why)
    url = os.environ.get("APEX_LIVENESS_HC_URL", "")
    if url and rep["n_examined"] > 0:  # dead-man's switch: only ping if we actually judged something
        try:
            subprocess.run(["curl", "-fsS", "--max-time", "10", url],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    return 1 if rep["alarms"] else 0


if __name__ == "__main__":
    sys.exit(main())
