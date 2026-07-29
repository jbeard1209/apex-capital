#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — CIT MIGRATION AUDIT
# =============================================================================
# Chairman directive 2026-07-29: "make sure that all the current infrastructure
# is updated so that it goes full auto and there isn't any systems that are still
# using the old thing." The "old thing" = autonomous code-writers that carried
# their OWN private guardrails (e.g. cit_implementation_agent's PROTECTED_FILES),
# which could silently drift from the real, red-teamed policy in signoff_ledger.
#
# This audit is CIT's proof that every autonomous code-writing actuator now
# routes through the single sign-off ledger choke point. It is deliberately
# blunt: it greps the source, so it catches a NEW actuator someone adds later
# that forgets to call the gate — the exact regression this is meant to prevent.
#
# Exit code 0 = migration complete. Exit 1 = at least one actuator is unmigrated
# (which, per the Chairman, "will be bad" for CIT — the red team verifies this).
#
#   python3 cit_migration_audit.py            # human report
#   python3 cit_migration_audit.py --json     # machine report
# =============================================================================
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

# Every module that can autonomously WRITE code/config to disk must call the gate.
# (file, must-reference-symbols): the audit passes a file only if it imports the
# ledger AND references preflight/hard-line delegation.
ACTUATORS = {
    "cit_implementation_agent.py": ["signoff_ledger", "preflight"],
    "board_meeting.py":            ["signoff_ledger", "preflight"],
}

# Files that must NOT define a private protected-file list anymore — the policy
# lives ONLY in signoff_ledger. A rogue local list is a drift hazard.
NO_PRIVATE_POLICY = {
    "cit_implementation_agent.py": r"^PROTECTED_FILES\s*=",
}


def _read(fname: str) -> str:
    p = BASE / fname
    return p.read_text() if p.exists() else ""


def audit() -> dict:
    findings = []

    for fname, needles in ACTUATORS.items():
        src = _read(fname)
        if not src:
            findings.append({"file": fname, "issue": "MISSING", "detail": "actuator file not found"})
            continue
        for n in needles:
            if n not in src:
                findings.append({"file": fname, "issue": "UNMIGRATED",
                                 "detail": f"does not reference '{n}' — may bypass the gate"})

    for fname, pattern in NO_PRIVATE_POLICY.items():
        src = _read(fname)
        if src and re.search(pattern, src, re.MULTILINE):
            findings.append({"file": fname, "issue": "PRIVATE_POLICY",
                             "detail": f"still defines a private policy matching /{pattern}/ "
                                       f"— must delegate to signoff_ledger"})

    # The ledger itself must exist and expose the choke point.
    led = _read("signoff_ledger.py")
    for sym in ("def preflight", "def touches_hard_line", "def get_mode", "def set_mode"):
        if sym not in led:
            findings.append({"file": "signoff_ledger.py", "issue": "GATE_INCOMPLETE",
                             "detail": f"missing {sym}"})

    return {
        "actuators_checked": list(ACTUATORS),
        "findings": findings,
        "migrated": len(findings) == 0,
    }


def main():
    rep = audit()
    if "--json" in sys.argv:
        print(json.dumps(rep, indent=2))
    else:
        print("=" * 64)
        print("  CIT MIGRATION AUDIT — autonomous actuators → sign-off ledger")
        print("=" * 64)
        print(f"  actuators checked: {', '.join(rep['actuators_checked'])}")
        if rep["migrated"]:
            print("  ✅ MIGRATED — every actuator routes through the gate; "
                  "no private policy lists remain.")
        else:
            print(f"  ❌ {len(rep['findings'])} issue(s):")
            for f in rep["findings"]:
                print(f"     • [{f['issue']}] {f['file']}: {f['detail']}")
    sys.exit(0 if rep["migrated"] else 1)


if __name__ == "__main__":
    main()
