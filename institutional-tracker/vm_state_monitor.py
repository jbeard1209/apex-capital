#!/usr/bin/env python3
"""
APEX CAPITAL — VM STATE MONITOR (runs on the Mac, asks GCP directly)
=====================================================================
The gap this closes, stated plainly: on 2026-08-06 the GCP free trial ended and
the instance was STOPPED. The firm ran exit-only for **eight days** and nothing
told the Chairman. Every check we had asked the wrong host the wrong question:

  - ssh timed out    -> "unreachable", which we had already learned is not "dead"
  - the 48h tally    -> a COUNT, so six orders from before the stop read as alive
  - healthchecks.io  -> only knows "did main.py ping", and its mail did not land
  - anything on the VM itself -> died with the VM

Nobody ever asked **Google** whether the machine was on. That is one API call and
it is authoritative: it needs no ssh, no cooperation from the VM, and it is true
even when the VM is powered off, out of quota, or has no external IP.

TWO INDEPENDENT QUESTIONS. Neither may stand in for the other:

  1. Is the box powered on?      -> GCP  (gcloud compute instances describe)
  2. Is the writer writing?      -> broker (single_writer.writer_silence_verdict)

A RUNNING instance that places no orders is still a broken firm -- that is the
whole lesson of 2026-08-14, one level up. Today's 18:00 run proved the inverse
too: main.py completed and pinged the heartbeat GREEN while 117 of its 135
orders failed. "The process ran" and "the firm traded" are different facts.

UNKNOWN IS NOT OK. If gcloud is missing, unauthenticated, or errors, this exits
non-zero and escalates. A monitor that goes quiet when its own plumbing breaks
is the failure it was built to catch (see memory `presence-is-not-currency`).

Usage:
    python3 vm_state_monitor.py            # check + escalate on trouble
    python3 vm_state_monitor.py --report   # print only, never escalate
    python3 vm_state_monitor.py --quiet    # exit code only (for LaunchAgent)

Exit codes: 0 = all good, 1 = degraded/HIGH, 2 = CRITICAL, 3 = cannot tell.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_FILE = DATA_DIR / "vm_state_monitor.json"

# From INFRASTRUCTURE.md row 1. The instance name is NOT the project name --
# that confusion is why the VM could not be looked up for months.
INSTANCE = "apex-capital-configuration"
ZONE = "us-east4-a"
PROJECT = "apex-capital-495523"

# Anything other than RUNNING means the firm's designated writer cannot trade.
HEALTHY_STATUS = "RUNNING"


def _gcloud_status():
    """(status, detail). status is a GCP power state, or None if we cannot tell.

    Deliberately distinguishes "the box is off" from "I could not ask" -- they
    have different fixes and only one of them is an emergency about the VM.
    """
    try:
        out = subprocess.run(
            ["gcloud", "compute", "instances", "describe", INSTANCE,
             f"--zone={ZONE}", f"--project={PROJECT}",
             "--format=value(status)"],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return None, ("gcloud is not installed on this host. "
                      "brew install --cask google-cloud-sdk")
    except subprocess.TimeoutExpired:
        return None, "gcloud timed out after 60s (network? auth prompt?)"

    if out.returncode != 0:
        err = (out.stderr or "").strip()
        if "credential" in err.lower() or "auth" in err.lower():
            return None, ("gcloud has no credentials on this host. "
                          "Run: gcloud auth login")
        return None, f"gcloud failed (rc={out.returncode}): {err[:300]}"

    status = (out.stdout or "").strip()
    return (status or None), (status or "gcloud returned an empty status")


def _writer_status():
    """(severity, message) from the broker. Independent of GCP entirely."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import single_writer as sw

        orders = sw.fetch_broker_orders()
        last_seen = sw.hosts_last_seen(orders)
        # `orders` is passed so the verdict can see whether the corroborating
        # order was an exit. A sell clears a gate that stops every buy, so on
        # its own it proves the market was open, not that the writer could have
        # opened anything.
        return sw.writer_silence_verdict(last_seen, orders=orders)
    except Exception as e:
        # Cannot tell != fine. Say so and let the caller escalate.
        return "UNKNOWN", f"could not read the broker: {type(e).__name__}: {e}"


def _notify_desktop(title, message):
    """macOS banner. Loud, immediate, and NOT the 117-item Chairman queue.

    The queue is where an alarm goes to be one of a hundred. This is a second,
    deliberately different channel; it is best-effort and never fatal.
    """
    try:
        safe = message.replace('"', "'")[:240]
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "{title}" sound name "Basso"'],
            capture_output=True, timeout=15)
    except Exception:
        pass


def _load_prev():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(payload):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def check(escalate=True):
    now = datetime.now(timezone.utc)
    status, detail = _gcloud_status()
    wsev, wmsg = _writer_status()

    lines = []
    if status is None:
        power_sev = "UNKNOWN"
        lines.append(f"[UNKNOWN] cannot determine instance power state — {detail}")
    elif status != HEALTHY_STATUS:
        power_sev = "CRITICAL"
        lines.append(
            f"[CRITICAL] instance '{INSTANCE}' is {status}, not {HEALTHY_STATUS}. "
            f"The designated writer cannot trade. "
            f"Start it: gcloud compute instances start {INSTANCE} "
            f"--zone={ZONE} --project={PROJECT}")
    else:
        power_sev = "PASS"
        lines.append(f"[PASS] instance '{INSTANCE}' is {status}")

    lines.append(f"[{wsev}] {wmsg}")

    rank = {"PASS": 0, "SKIP": 0, "UNKNOWN": 1, "HIGH": 1, "CRITICAL": 2}
    worst = max(rank.get(power_sev, 1), rank.get(wsev, 1))

    prev = _load_prev()
    changed = (prev.get("power") != power_sev) or (prev.get("writer") != wsev)
    _save({"checked_at": now.isoformat(), "power": power_sev,
           "gcp_status": status, "writer": wsev, "detail": detail,
           "writer_msg": wmsg})

    # Escalate on trouble. Banner only when the state CHANGED, so a sustained
    # outage does not become its own flood -- but the exit code stays hot every
    # run, so a scheduler or a human polling it always sees the truth.
    if escalate and worst >= 1:
        if changed:
            _notify_desktop(
                "APEX CAPITAL — VM PROBLEM",
                lines[0] if power_sev != "PASS" else lines[1])
        try:
            from pod_health_monitor import _escalate
            _escalate("PROD",
                      {"name": "VM state monitor",
                       "fix": f"gcloud compute instances start {INSTANCE} "
                              f"--zone={ZONE} --project={PROJECT}"},
                      "vm_state",
                      "CRITICAL" if worst >= 2 else "HIGH",
                      " | ".join(lines))
        except Exception:
            pass

    return worst, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true",
                    help="print only, never escalate")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    args = ap.parse_args()

    worst, lines = check(escalate=not args.report)

    if not args.quiet:
        print(f"VM STATE MONITOR — {datetime.now(timezone.utc).isoformat()[:19]}Z")
        print(f"  instance {INSTANCE} / {ZONE} / {PROJECT}")
        for ln in lines:
            print(f"  {ln}")

    return {0: 0, 1: 1, 2: 2}.get(worst, 3)


if __name__ == "__main__":
    sys.exit(main())
