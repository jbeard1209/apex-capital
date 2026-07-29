#!/usr/bin/env python3
"""
Sign-off ledger — SHADOW → ENFORCE promotion gate.
================================================================================
Turns "leave it in shadow a week, and if nothing goes wrong put it into full
place" into an OBJECTIVE, auditable checklist instead of a bare timer.

Key framing (read this before touching it):
  Flipping SHADOW → ENFORCE makes the gate STRICTER — it blocks more and routes
  more to the Chairman. It is a safety-INCREASING move. The only real risk of
  flipping is OPERATIONAL: if the sign-off path can't earn clean sign-offs, then
  ENFORCE would over-block and stall autonomous fixing (a "full stop", not "full
  auto"). So this gate mostly proves ENFORCE won't stall the firm.

This script NEVER flips the mode itself. A GREEN result only means "the evidence
supports promotion — take it to the board + CIT for the joint vote". The flip is
still `signoff_ledger.set_mode("ENFORCE", ["BOARD","CIT"])`, gated on that vote
(Chairman directive). Turning strictness back OFF (→ SHADOW) is always allowed.

Exit code: 0 = READY to recommend the flip vote; 1 = NOT READY (reasons printed).
Usage:
    python3 signoff_promotion_gate.py            # evaluate + print, set exit code
    python3 signoff_promotion_gate.py --json      # machine-readable
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import signoff_ledger as sl

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# --- promotion thresholds ---------------------------------------------------
MIN_WINDOW_DAYS      = 7     # observe at least a week of shadow traffic
MIN_EVIDENCE_EVENTS  = 5     # enough preflight decisions to mean something
MIN_WOULD_CLEAR      = 1     # >=1 change that WOULD have cleared → not a full stop


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _run(cmd: list) -> tuple[bool, str]:
    """Run a checker script; return (passed, short tail of output)."""
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=900)
        tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
        return p.returncode == 0, tail[0][:200]
    except Exception as e:  # a checker that won't even run is a fail-closed NOT-READY
        return False, f"{type(e).__name__}: {e}"


def _hr_flagged_signers() -> list:
    """Any signer currently HR-flagged for reputation farming is disqualifying."""
    try:
        rep = json.load(open(os.path.join(DATA, "agent_reputation.json")))
    except Exception:
        return []
    flagged = []
    agents = rep.get("agents", rep) if isinstance(rep, dict) else {}
    for name, r in (agents.items() if isinstance(agents, dict) else []):
        if isinstance(r, dict) and r.get("hr_flag"):
            flagged.append(name)
    return flagged


def evaluate() -> dict:
    checks = []   # (name, ok, detail, blocking)

    # 1) Observation window ---------------------------------------------------
    mode_meta = sl._load(sl.MODE_PATH, {})
    mode = sl.get_mode()
    set_at = _parse(mode_meta.get("set_at", ""))
    days = round((_now() - set_at).total_seconds() / 86400, 2) if set_at else 0.0
    checks.append((
        f"observation window ≥ {MIN_WINDOW_DAYS}d",
        mode == "SHADOW" and days >= MIN_WINDOW_DAYS,
        f"mode={mode}, {days}d since window start ({mode_meta.get('note','')[:40]})",
        True,
    ))

    # 2) Evidence volume + 3) sign-off path actually clears -------------------
    summ = sl.shadow_summary()
    checks.append((
        f"evidence volume ≥ {MIN_EVIDENCE_EVENTS} decisions",
        summ.get("events", 0) >= MIN_EVIDENCE_EVENTS,
        f"{summ.get('events',0)} preflight decisions logged",
        True,
    ))
    would_clear = summ.get("distinct_would_clear_changes", 0)
    checks.append((
        f"sign-off path clears ≥ {MIN_WOULD_CLEAR} change (ENFORCE won't be a full stop)",
        would_clear >= MIN_WOULD_CLEAR,
        f"{would_clear} distinct change(s) would have cleared; "
        f"{summ.get('would_block_events',0)} would-blocks",
        True,
    ))

    # 4) Red team — 0 bypasses ------------------------------------------------
    ok, tail = _run([sys.executable, "signoff_ledger_redteam.py"])
    checks.append(("red-team: 0 bypasses", ok, tail, True))

    # 5) Migration audit — every actuator on the ledger -----------------------
    ok, tail = _run([sys.executable, "cit_migration_audit.py"])
    checks.append(("migration audit clean", ok, tail, True))

    # 6) Tests + wiring green on current HEAD ---------------------------------
    ok_s, tail_s = _run([sys.executable, "test_pipeline_smoke.py"])
    checks.append(("smoke tests green", ok_s, tail_s, True))
    ok_w, tail_w = _run([sys.executable, "wiring_audit.py"])
    checks.append(("wiring audit clean", ok_w, tail_w, True))

    # 7) No signer currently HR-flagged for farming ---------------------------
    flagged = _hr_flagged_signers()
    checks.append((
        "no HR-flagged signers",
        not flagged,
        "none" if not flagged else f"flagged: {', '.join(flagged)}",
        True,
    ))

    blocking_fail = [c for c in checks if c[3] and not c[1]]
    ready = not blocking_fail
    return {
        "ready": ready,
        "mode": mode,
        "window_days": days,
        "checks": [
            {"name": n, "ok": ok, "detail": d, "blocking": b} for (n, ok, d, b) in checks
        ],
        "blocking_failures": [c[0] for c in blocking_fail],
        "next_step": (
            "RECOMMEND FLIP: convene board + CIT joint vote; on pass call "
            "signoff_ledger.set_mode('ENFORCE', ['BOARD','CIT'])."
            if ready else
            "STAY IN SHADOW: resolve the blocking failures above, then re-run."
        ),
        "at": _now().isoformat(),
    }


def main() -> int:
    res = evaluate()
    if "--json" in sys.argv:
        print(json.dumps(res, indent=2))
        return 0 if res["ready"] else 1
    flag = "✅ READY" if res["ready"] else "⛔ NOT READY"
    print("=" * 70)
    print(f"  SHADOW → ENFORCE promotion gate   {flag}")
    print(f"  mode={res['mode']}  window={res['window_days']}d")
    print("=" * 70)
    for c in res["checks"]:
        mark = "✓" if c["ok"] else "✗"
        print(f"  [{mark}] {c['name']}")
        print(f"        {c['detail']}")
    print("-" * 70)
    print(f"  → {res['next_step']}")
    return 0 if res["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
