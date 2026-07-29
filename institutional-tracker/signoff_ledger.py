#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — ACCOUNTABLE SIGN-OFF LEDGER
# =============================================================================
# The firm is excellent at DETECTING problems and terrible at being ACCOUNTABLE
# for fixing them: the deputy board "approved" 1,464 proposals and implemented
# ~0, because approval carried no consequence. This ledger makes autonomous
# fixes carry named, outcome-weighted accountability before they deploy.
#
# The core idea (Chairman directive 2026-07-29): "have CIT and IRM really
# examine everything and each agent personally approve it with its reputation
# on the line." The trap that idea must avoid is theatre — a signature that
# costs nothing is the deputy board again. So reputation here is scored on the
# OUTCOME of what you signed, not on the act of signing:
#
#   • Sign a fix that HOLDS  → small reputation credit.
#   • Sign a fix that is REVERTED / REGRESSES → large reputation debit (3x).
#   • Reputation is an EWMA (recent behaviour dominates) — you cannot coast on
#     old glory, and you cannot farm rep by rubber-stamping trivia.
#
# Hard guardrails, enforced mechanically (not by policy):
#   1. A change touching trade-execution / risk-limit / credential files can
#      NEVER be auto-cleared — it routes to the Chairman. (The Oracle line.)
#   2. No sign-off is valid without a recorded GREEN test+audit result attached
#      to the change. The gate is a data dependency, not a hope.
#   3. Blast radius over the limit auto-routes to the Chairman regardless of
#      sign-off. Small reversible fixes flow; big ones stop.
#   4. A per-day deploy budget caps autonomous change velocity — the circuit
#      breaker against a runaway self-modification loop.
#   5. CIT (correctness) and IRM (risk) are DIFFERENT lenses and both must sign;
#      Canary holds a veto. One accountable "no" routes the change to a human.
#
# Standalone:  python3 signoff_ledger.py --report
# =============================================================================

from __future__ import annotations

import json
import os
import fnmatch
import tempfile
from datetime import datetime, date

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
LEDGER_PATH     = os.path.join(DATA, "signoff_ledger.json")
REPUTATION_PATH = os.path.join(DATA, "agent_reputation.json")

# --- HARD LINE: files an autonomous change may never self-clear -------------
# Matched with fnmatch against the change's declared file paths (basename OR
# repo-relative). Touch any of these and the change routes to the Chairman.
HARD_LINE_GLOBS = [
    "alpaca_trader.py",        # order submission / execution paths
    "config.py",               # position caps, risk limits, deployment targets
    ".env", "*.env",           # credentials
    "*secret*", "*credential*",
    "position_risk_monitor.py",  # circuit breaker
]

# --- BLAST RADIUS: auto-route to human above these ---------------------------
MAX_BLAST_FILES = 2
MAX_BLAST_LINES = 40           # added + removed, summed

# --- DAILY DEPLOY BUDGET -----------------------------------------------------
DAILY_DEPLOY_BUDGET = 3

# --- REQUIRED SIGNERS --------------------------------------------------------
REQUIRED_ROLES = {"CIT", "IRM"}     # both lenses must sign
VETO_ROLES     = {"CIT", "IRM", "CANARY"}

# --- REPUTATION MODEL --------------------------------------------------------
# EWMA of a per-event quality signal. Asymmetric: a burn hurts 3x a hold.
_EWMA_ALPHA      = 0.30
_SIGNAL_HELD     = +1.0
_SIGNAL_REVERTED = -3.0
_SIGNAL_REGRESSED= -3.0
_OUTCOME_SIGNAL  = {"HELD": _SIGNAL_HELD, "REVERTED": _SIGNAL_REVERTED,
                    "REGRESSED": _SIGNAL_REGRESSED}


# ---------------------------------------------------------------------------
# storage helpers (atomic)
# ---------------------------------------------------------------------------
def _load(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _now() -> str:
    return datetime.now().isoformat()


def _ledger() -> dict:
    d = _load(LEDGER_PATH, {})
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------------------
# guardrail checks
# ---------------------------------------------------------------------------
def touches_hard_line(files: list) -> list:
    """Return the subset of declared files that hit a hard-line glob."""
    hits = []
    for f in files or []:
        base = os.path.basename(str(f))
        for glob in HARD_LINE_GLOBS:
            if fnmatch.fnmatch(base, glob) or fnmatch.fnmatch(str(f), glob):
                hits.append(str(f))
                break
    return hits


def blast_radius_ok(files: list, added: int, removed: int) -> bool:
    return (len(files or []) <= MAX_BLAST_FILES
            and (int(added or 0) + int(removed or 0)) <= MAX_BLAST_LINES)


def deploys_today() -> int:
    today = date.today().isoformat()
    return sum(1 for c in _ledger().values()
               if (c.get("deployed_at") or "")[:10] == today)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def propose_change(change_id: str, title: str, files: list,
                   added: int = 0, removed: int = 0, category: str = "BUG_FIX",
                   test_result: str | None = None) -> dict:
    """Register an autonomous change for sign-off. `test_result` must be the
    literal 'PASS' (smoke + audit both green) for the change to be clearable."""
    led = _ledger()
    hard = touches_hard_line(files)
    rec = {
        "change_id":   change_id,
        "title":       title,
        "files":       list(files or []),
        "added":       int(added or 0),
        "removed":     int(removed or 0),
        "category":    category,
        "test_result": test_result,          # 'PASS' | 'FAIL' | None
        "proposed_at": _now(),
        "signatures":  [],                    # [{role, agent, rationale, at}]
        "veto":        None,                  # {role, agent, reason, at}
        "hard_line_hits": hard,
        "status":      "ROUTE_TO_HUMAN" if hard else "AWAITING_SIGNOFF",
        "outcome":     None,                  # HELD | REVERTED | REGRESSED
        "deployed_at": None,
    }
    led[change_id] = rec
    _save(LEDGER_PATH, led)
    return rec


def attach_test_result(change_id: str, result: str) -> None:
    led = _ledger()
    if change_id in led:
        led[change_id]["test_result"] = result
        _save(LEDGER_PATH, led)


def sign(change_id: str, agent: str, role: str, rationale: str) -> dict:
    """Record a named sign-off. role ∈ {CIT, IRM}. A signature with an empty
    rationale is rejected — accountability requires stating what you checked."""
    role = role.upper()
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    if rec["status"] == "ROUTE_TO_HUMAN":
        # sign-off cannot override the hard line; record the attempt, no effect
        rec.setdefault("blocked_signatures", []).append(
            {"role": role, "agent": agent, "at": _now(),
             "note": "hard-line change — sign-off has no authority here"})
        _save(LEDGER_PATH, led)
        return rec
    if not (rationale or "").strip():
        raise ValueError("sign-off requires a non-empty rationale")
    # one signature per (role, agent); re-signing updates rationale
    rec["signatures"] = [s for s in rec["signatures"]
                         if not (s["role"] == role and s["agent"] == agent)]
    rec["signatures"].append({"role": role, "agent": agent,
                              "rationale": rationale[:400], "at": _now()})
    _refresh_status(rec)
    _save(LEDGER_PATH, led)
    return rec


def veto(change_id: str, agent: str, reason: str, role: str = "CANARY") -> dict:
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    rec["veto"] = {"role": role.upper(), "agent": agent,
                   "reason": reason[:400], "at": _now()}
    rec["status"] = "VETOED_ROUTE_TO_HUMAN"
    _save(LEDGER_PATH, led)
    return rec


def _refresh_status(rec: dict) -> None:
    if rec.get("veto"):
        rec["status"] = "VETOED_ROUTE_TO_HUMAN"; return
    if rec.get("hard_line_hits"):
        rec["status"] = "ROUTE_TO_HUMAN"; return
    ok, _ = _clearance_reasons(rec)
    rec["status"] = "CLEARED" if ok else "AWAITING_SIGNOFF"


def _clearance_reasons(rec: dict) -> tuple[bool, list]:
    reasons = []
    if rec.get("hard_line_hits"):
        reasons.append(f"touches hard-line file(s): {', '.join(rec['hard_line_hits'])}")
    if rec.get("veto"):
        v = rec["veto"]
        reasons.append(f"vetoed by {v['agent']} ({v['role']}): {v['reason']}")
    if rec.get("test_result") != "PASS":
        reasons.append(f"no green test+audit on record (test_result={rec.get('test_result')})")
    if not blast_radius_ok(rec.get("files"), rec.get("added"), rec.get("removed")):
        reasons.append(f"blast radius too large ({len(rec.get('files',[]))} files, "
                       f"{rec.get('added',0)+rec.get('removed',0)} lines > "
                       f"{MAX_BLAST_FILES}/{MAX_BLAST_LINES})")
    roles_signed = {s["role"] for s in rec.get("signatures", [])}
    missing = REQUIRED_ROLES - roles_signed
    if missing:
        reasons.append(f"missing required sign-off from: {', '.join(sorted(missing))}")
    return (len(reasons) == 0, reasons)


def is_cleared(change_id: str) -> tuple[bool, list]:
    """Return (cleared, reasons). Also enforces the daily deploy budget."""
    rec = _ledger().get(change_id)
    if rec is None:
        return (False, ["unknown change"])
    ok, reasons = _clearance_reasons(rec)
    if ok and deploys_today() >= DAILY_DEPLOY_BUDGET:
        return (False, [f"daily deploy budget reached ({DAILY_DEPLOY_BUDGET}) — queues for Chairman"])
    return (ok, reasons)


def mark_deployed(change_id: str) -> None:
    led = _ledger()
    if change_id in led:
        led[change_id]["deployed_at"] = _now()
        led[change_id]["status"] = "DEPLOYED"
        _save(LEDGER_PATH, led)


def record_outcome(change_id: str, outcome: str) -> dict:
    """Close the loop: score the SIGNERS on how their change fared.
    outcome ∈ {HELD, REVERTED, REGRESSED}."""
    outcome = outcome.upper()
    if outcome not in _OUTCOME_SIGNAL:
        raise ValueError(f"outcome must be one of {list(_OUTCOME_SIGNAL)}")
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    rec["outcome"] = outcome
    rec["outcome_at"] = _now()
    _save(LEDGER_PATH, led)
    signal = _OUTCOME_SIGNAL[outcome]
    for s in rec.get("signatures", []):
        _apply_reputation(s["agent"], signal, change_id, outcome)
    return rec


def _apply_reputation(agent: str, signal: float, change_id: str, outcome: str) -> None:
    rep = _load(REPUTATION_PATH, {})
    if not isinstance(rep, dict):
        rep = {}
    r = rep.get(agent) or {"ewma": 0.0, "n_signed": 0, "n_held": 0,
                           "n_burned": 0, "history": []}
    r["ewma"] = round((1 - _EWMA_ALPHA) * r["ewma"] + _EWMA_ALPHA * signal, 4)
    r["n_signed"] += 1
    if outcome == "HELD":
        r["n_held"] += 1
    else:
        r["n_burned"] += 1
    r["history"] = (r.get("history", []) + [
        {"change_id": change_id, "outcome": outcome, "signal": signal, "at": _now()}
    ])[-50:]
    r["last_update"] = _now()
    rep[agent] = r
    _save(REPUTATION_PATH, rep)


def reputation(agent: str | None = None):
    """Signer reputation. ewma < 0 means recent sign-offs have been burning."""
    rep = _load(REPUTATION_PATH, {})
    if not isinstance(rep, dict):
        rep = {}
    if agent:
        return rep.get(agent, {"ewma": 0.0, "n_signed": 0, "n_held": 0, "n_burned": 0})
    return rep


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _report() -> None:
    led = _ledger()
    from collections import Counter
    st = Counter(c.get("status") for c in led.values())
    print("=== Sign-off ledger ===")
    print(f"  changes tracked: {len(led)}  ·  deploys today: {deploys_today()}/{DAILY_DEPLOY_BUDGET}")
    print("  by status:", dict(st))
    rep = reputation()
    if rep:
        print("\n=== Signer reputation (EWMA; negative = burning) ===")
        for a, r in sorted(rep.items(), key=lambda kv: kv[1].get("ewma", 0)):
            print(f"  {a:28} ewma={r['ewma']:+.2f}  held={r['n_held']} burned={r['n_burned']}")
    else:
        print("  (no reputation history yet)")


if __name__ == "__main__":
    import sys
    if "--report" in sys.argv:
        _report()
    else:
        _report()
