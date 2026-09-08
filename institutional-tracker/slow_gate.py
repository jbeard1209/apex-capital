#!/usr/bin/env python3
"""slow_gate — the full test_pipeline_smoke suite, off the push path.

Why this exists
---------------
The old pre-push hook ran the FULL 1559-test suite in two zones (local +
TZ=UTC). That takes ~2h 14min per zone, ~4.5h total. GitHub's SSH server
enforces its own idle timeout on push connections that does not respect
client-side ServerAlive keep-alives, so a push under that gate consistently
died with SIGPIPE while the gate was still running. The gate said "OK" and
the push exited 141 anyway; from the outside the two states were
indistinguishable, and the fix could not land through its own gate.

The fast pre-push gate now runs only the wiring-contract guarantees
(wiring_audit + TestWiringContracts, dual-zone, ~25s total). Everything
else runs here, on a schedule, on the host that runs main.py — the same
host that would care if it went red. Reports to `data/slow_gate_report.json`
so the Chairman decision queue can escalate a red slow-gate the same way
it escalates any other production failure.

Runs both zones for the same host-divergence reason the fast gate does:
"green on the Mac is not green" — see [[host_divergence_is_a_bug_class]].
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent
DATA_DIR   = BASE_DIR / "data"
REPORT     = DATA_DIR / "slow_gate_report.json"
SUITE      = BASE_DIR / "test_pipeline_smoke.py"
LOG_DIR    = DATA_DIR / "slow_gate_logs"

# Timeout PER zone. The full suite ran 8028s (~2h 14min) in the last measured
# gate; give it 4h per zone so a slow test doesn't cause a false red, but a
# runaway definitely fails closed.
TIMEOUT_S  = 4 * 60 * 60


def _run_zone(zone: str | None) -> dict:
    """Run test_pipeline_smoke.py under `zone` (or the local zone if None).

    Returns a summary dict; also writes the full log to LOG_DIR so a failure
    can be diagnosed without re-running the ~2h suite.
    """
    env = dict(os.environ)
    if zone:
        env["TZ"] = zone
    env["PYTHONUNBUFFERED"] = "1"

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_name = f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{zone or 'local'}.log"
    log_path = LOG_DIR / log_name

    started = datetime.now().isoformat()
    try:
        with open(log_path, "w") as f:
            r = subprocess.run(
                [sys.executable, str(SUITE)],
                stdout=f, stderr=subprocess.STDOUT,
                env=env, timeout=TIMEOUT_S,
                cwd=str(BASE_DIR),
            )
        ok = (r.returncode == 0)
        detail = f"exit_code={r.returncode}"
    except subprocess.TimeoutExpired:
        ok = False
        detail = f"timeout after {TIMEOUT_S}s"
    finished = datetime.now().isoformat()

    # Extract "Ran N tests in Ms" and FAIL/ERROR lines from the log so the
    # report is skimmable without opening the log file.
    ran_line = ""
    fail_lines: list[str] = []
    try:
        with open(log_path) as f:
            for line in f:
                if line.startswith("Ran "):
                    ran_line = line.strip()
                elif line.startswith(("FAIL:", "ERROR:")):
                    fail_lines.append(line.strip())
    except Exception:
        pass

    return {
        "zone":     zone or "local",
        "ok":       ok,
        "detail":   detail,
        "started":  started,
        "finished": finished,
        "log":      str(log_path.relative_to(BASE_DIR)),
        "ran":      ran_line,
        "failures": fail_lines[:20],
    }


def main() -> int:
    zones = [None, "UTC"]
    zone_results = [_run_zone(z) for z in zones]
    all_ok = all(z["ok"] for z in zone_results)

    report = {
        "run_id":      datetime.now().strftime("%Y%m%dT%H%M%S"),
        "started":     zone_results[0]["started"],
        "finished":    zone_results[-1]["finished"],
        "ok":          all_ok,
        "zones":       zone_results,
        "host":        os.uname().nodename,
        "suite":       str(SUITE.relative_to(BASE_DIR)),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2))

    verdict = "OK" if all_ok else "FAILED"
    print(f"[slow_gate] {verdict}: local={zone_results[0]['ok']} UTC={zone_results[1]['ok']}")
    print(f"[slow_gate] report: {REPORT.relative_to(BASE_DIR)}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
