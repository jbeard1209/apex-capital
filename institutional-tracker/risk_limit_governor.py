#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — RISK-LIMIT GOVERNOR  (asymmetric, notified, hard-capped)
# =============================================================================
# Chairman directive 2026-07-29: "risk limits can be moved but only to a small
# certain point, and I should be notified if they are." Editing config.py itself
# is a hard-line action (the sign-off ledger blocks autonomous source edits), so
# the firm must NOT rewrite the file to move a limit. Instead the governor writes
# a bounded RUNTIME OVERRIDE that config.py applies at import — the source of
# truth (the baseline) is never touched, and every move is logged + notified.
#
# The policy is ASYMMETRIC by design (Chairman's chosen option):
#   • TIGHTEN (make the firm safer) → always allowed, applied immediately, logged.
#   • LOOSEN (take more risk)       → allowed ONLY within ±MAX_LOOSEN_FRAC of the
#     current effective value, at most once per knob per day, and never past a
#     hard CEILING that only the Chairman can raise. A loosen also NOTIFIES the
#     Chairman (decision queue) and logs to the board. Anything outside the band,
#     over the daily step, or past the ceiling → routed to the Chairman, not applied.
#
# Circuit breakers (CRO HIGH/CRITICAL block, the position_risk_monitor) are NOT
# governed here — they stay Chairman-only via the ledger hard line.
#
#   python3 risk_limit_governor.py --report
# =============================================================================
from __future__ import annotations
import json
import os
import tempfile
from datetime import datetime, date

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OVERRIDES_PATH = os.path.join(DATA, "risk_limit_overrides.json")
GOV_LOG_PATH   = os.path.join(DATA, "risk_limit_governor_log.json")
CHAIR_ACTION   = os.path.join(DATA, "chairman_action_required.json")

MAX_LOOSEN_FRAC = 0.10     # a single loosen may move a limit at most 10%

# Eligible knobs. `tighten` = the direction that REDUCES risk. `ceiling`/`floor`
# are the Chairman-only hard bounds a governed move may never cross. `baseline`
# is read live from config so we always band around the real configured value.
#   tighten="down": a SMALLER value is safer (e.g. per-position cap, stop-loss).
#   tighten="up":   a LARGER value is safer (rare; none today).
GOVERNED = {
    "MAX_POSITION_SIZE_PCT": {"tighten": "down", "floor": 0.02, "ceiling": 0.10},
    "MAX_OPEN_POSITIONS":    {"tighten": "down", "floor": 5,    "ceiling": 25},
    "STOP_LOSS_PCT":         {"tighten": "down", "floor": 0.03, "ceiling": 0.12},
    "TARGET_DEPLOYMENT_PCT": {"tighten": "down", "floor": 0.20, "ceiling": 0.85},
}


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


def _now():
    return datetime.now().isoformat()


def overrides() -> dict:
    o = _load(OVERRIDES_PATH, {})
    return o if isinstance(o, dict) else {}


def _baseline(knob: str):
    """The real configured value, read WITHOUT the override applied (config reads
    overrides at import, so we read the raw source constant via a fresh parse)."""
    import config
    # config applies overrides to its globals at import; the untouched baseline is
    # preserved on config._RISK_BASELINES by the same hook.
    base = getattr(config, "_RISK_BASELINES", {})
    if knob in base:
        return base[knob]
    return getattr(config, knob, None)


def effective(knob: str):
    o = overrides().get(knob)
    if isinstance(o, dict) and "value" in o:
        return o["value"]
    return _baseline(knob)


def _is_tighten(knob: str, new_value, current) -> bool:
    d = GOVERNED[knob]["tighten"]
    return (new_value <= current) if d == "down" else (new_value >= current)


def _within_band(knob: str, new_value, current) -> bool:
    if current in (None, 0):
        return False
    return abs(new_value - current) <= abs(current) * MAX_LOOSEN_FRAC + 1e-9


def _adjusted_today(knob: str) -> bool:
    o = overrides().get(knob)
    return bool(isinstance(o, dict) and (o.get("at", "")[:10] == date.today().isoformat()))


def _crosses_ceiling(knob: str, new_value) -> bool:
    cfg = GOVERNED[knob]
    return new_value > cfg["ceiling"] or new_value < cfg["floor"]


def _log(entry: dict):
    log = _load(GOV_LOG_PATH, [])
    if not isinstance(log, list):
        log = []
    log.append(entry)
    _save(GOV_LOG_PATH, log[-500:])


def _notify_chairman(knob, old, new, reason, kind):
    """Surface a risk-limit move (or a routed request) to the Chairman queue."""
    actions = _load(CHAIR_ACTION, [])
    if not isinstance(actions, list):
        actions = []
    actions.append({
        "id": f"risk-limit-{kind}-{knob}-{date.today().isoformat()}",
        "severity": "MEDIUM" if kind == "applied" else "HIGH",
        "status": "notice" if kind == "applied" else "awaiting_chairman",
        "source": "Risk-Limit Governor",
        "description": (f"Risk limit {knob} {'moved' if kind=='applied' else 'CHANGE REQUESTED'} "
                        f"{old} → {new}. Reason: {reason}. "
                        + ("(auto-applied within band; notice only)" if kind == "applied"
                           else "(outside band / over daily step / past ceiling — needs your decision)")),
        "ts": _now(),
    })
    _save(CHAIR_ACTION, actions)


def propose_adjustment(knob: str, new_value, reason: str, by: str = "unattributed") -> dict:
    """The one entry point for any agent that wants to move a risk limit.
    Returns {status, applied, ...}. status ∈ {APPLIED, ROUTED_TO_CHAIRMAN, REJECTED}."""
    if knob not in GOVERNED:
        return {"status": "REJECTED", "applied": False, "reason": f"{knob} is not a governed knob"}
    current = effective(knob)
    if current is None:
        return {"status": "REJECTED", "applied": False, "reason": f"no baseline for {knob}"}
    try:
        new_value = type(current)(new_value)
    except Exception:
        pass

    tighten = _is_tighten(knob, new_value, current)

    # Tightening is always safe: apply immediately (but still never past the floor).
    if tighten:
        if _crosses_ceiling(knob, new_value):
            new_value = max(GOVERNED[knob]["floor"], min(GOVERNED[knob]["ceiling"], new_value))
        result = "APPLIED"
    else:
        # Loosening: banded, rate-limited, ceiling-bounded, or it routes to Jack.
        if _crosses_ceiling(knob, new_value):
            reason_out = "past Chairman-only ceiling/floor"
            _notify_chairman(knob, current, new_value, reason, "requested")
            _log({"knob": knob, "from": current, "to": new_value, "by": by,
                  "reason": reason, "result": "ROUTED", "why": reason_out, "at": _now()})
            return {"status": "ROUTED_TO_CHAIRMAN", "applied": False, "reason": reason_out}
        if not _within_band(knob, new_value, current):
            reason_out = f"loosen exceeds ±{int(MAX_LOOSEN_FRAC*100)}% band"
            _notify_chairman(knob, current, new_value, reason, "requested")
            _log({"knob": knob, "from": current, "to": new_value, "by": by,
                  "reason": reason, "result": "ROUTED", "why": reason_out, "at": _now()})
            return {"status": "ROUTED_TO_CHAIRMAN", "applied": False, "reason": reason_out}
        if _adjusted_today(knob):
            reason_out = "one governed move per knob per day already used"
            _notify_chairman(knob, current, new_value, reason, "requested")
            _log({"knob": knob, "from": current, "to": new_value, "by": by,
                  "reason": reason, "result": "ROUTED", "why": reason_out, "at": _now()})
            return {"status": "ROUTED_TO_CHAIRMAN", "applied": False, "reason": reason_out}
        result = "APPLIED"

    # Persist the override + notify.
    o = overrides()
    o[knob] = {"value": new_value, "from": current, "by": by, "reason": reason[:300],
               "direction": "tighten" if tighten else "loosen", "at": _now()}
    _save(OVERRIDES_PATH, o)
    _notify_chairman(knob, current, new_value, reason, "applied")
    _log({"knob": knob, "from": current, "to": new_value, "by": by,
          "reason": reason, "result": result, "direction": "tighten" if tighten else "loosen",
          "at": _now()})
    return {"status": result, "applied": True, "from": current, "to": new_value,
            "direction": "tighten" if tighten else "loosen"}


def _report():
    print("=== Risk-Limit Governor ===")
    print(f"  band: ±{int(MAX_LOOSEN_FRAC*100)}% per loosen · 1 governed move/knob/day · "
          f"tighten always allowed")
    for knob, cfg in GOVERNED.items():
        eff, base = effective(knob), _baseline(knob)
        tag = "" if eff == base else f"  (overridden from {base})"
        print(f"  {knob:24} = {eff}{tag}   [floor {cfg['floor']} · ceiling {cfg['ceiling']}]")


if __name__ == "__main__":
    _report()
