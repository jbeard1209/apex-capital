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
MODE_PATH       = os.path.join(DATA, "signoff_mode.json")     # SHADOW | ENFORCE
SHADOW_LOG_PATH = os.path.join(DATA, "signoff_shadow_log.json")

# --- ROLLOUT MODE ------------------------------------------------------------
# The gate starts in SHADOW: it computes exactly what it WOULD block and logs it,
# but never actually blocks an autonomous action — so wiring it into live
# actuators cannot break the pipeline while the board + CIT watch it. Flipping to
# ENFORCE (Chairman directive 2026-07-29) requires a JOINT board+CIT vote, not a
# manual toggle: see set_mode(). Default is always SHADOW if the file is absent.
_VALID_MODES = ("SHADOW", "ENFORCE")
_FLIP_APPROVERS_REQUIRED = {"BOARD", "CIT"}   # both must approve to enforce

# --- HARD LINE: files an autonomous change may never self-clear -------------
# Red team RT-09/RT-10 (2026-07-29) evaded an exact-name glob with a renamed
# sibling (alpaca_trader_v2.py) and a case variant (ALPACA_TRADER.py). A
# hard-line guardrail must fail SAFE: match case-insensitively on a SUBSTRING
# of the basename, so any near-neighbour of a protected file also routes to the
# Chairman. Over-blocking here only means a human reviews — the correct bias.
HARD_LINE_SUBSTRINGS = [
    "alpaca_trader",           # order submission / execution paths (+ any sibling)
    "config",                  # position caps, risk limits, deployment targets
    "secret", "credential",    # credentials
    "position_risk_monitor",   # circuit breaker
    "risk_limit", "risk_manager",  # risk configuration / scoring
    # 2026-07-29: absorbed cit_implementation_agent's private PROTECTED_FILES so
    # the firm has ONE protected-file policy. These are trade-execution and
    # governance files an autonomous fixer must never rewrite unsupervised.
    "manager_agent",           # places board-approved orders
    "board_meeting",           # governance / vote application
    "irm_review",              # independent risk review
]

# A few files where a substring rule would be too broad or too narrow are pinned
# by exact basename (still matched case-insensitively). main.py is the pipeline
# entrypoint; "main" as a substring would over-match unrelated files.
HARD_LINE_EXACT = {"main.py"}

# --- BLAST RADIUS: auto-route to human above these ---------------------------
MAX_BLAST_FILES = 2
MAX_BLAST_LINES = 40           # added + removed, summed

# --- DAILY DEPLOY BUDGET -----------------------------------------------------
DAILY_DEPLOY_BUDGET = 3
# Red team RT-18: a big change split into N small ones each passes the
# per-change blast limit. A cumulative daily line cap closes the aggregate
# hole — you cannot ship 3× 40-line changes to smuggle a 120-line refactor.
MAX_DAILY_DEPLOY_LINES = 60

# --- REQUIRED SIGNERS --------------------------------------------------------
REQUIRED_ROLES = {"CIT", "IRM"}     # both lenses must sign
VETO_ROLES     = {"CIT", "IRM", "CANARY"}

# --- IRM-VETO → BOARD ESCALATION (Chairman directive 2026-07-29) -------------
# An IRM *risk-judgment* veto is not a dead end and is not overridable by CIT
# alone: it escalates to the board, which can override only by a hard bar —
# a 2/3 supermajority, with the CIT Director (a voting board member) having
# voted, above a quorum. Overriding your own risk officer must be difficult.
# Two vetoes are NEVER board-overridable: a CANARY veto (system-health kill
# switch) and any hard-line file (the Oracle line — Chairman only).
BOARD_OVERRIDE_SUPERMAJORITY = 2.0 / 3.0
BOARD_MIN_QUORUM = 3
_CIT_DIRECTOR_HINT = "cit"          # a voter whose name carries this is the CIT lens

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


def _git_head() -> str | None:
    """Current HEAD short hash, or None if git is unavailable."""
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=BASE, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def _ledger() -> dict:
    d = _load(LEDGER_PATH, {})
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------------------
# guardrail checks
# ---------------------------------------------------------------------------
def touches_hard_line(files: list) -> list:
    """Return the subset of declared files that hit a hard-line rule.
    Case-insensitive substring match on the basename, plus any .env file."""
    hits = []
    for f in files or []:
        base = os.path.basename(str(f)).lower()
        if base == ".env" or base.endswith(".env"):
            hits.append(str(f)); continue
        if base in {e.lower() for e in HARD_LINE_EXACT}:
            hits.append(str(f)); continue
        if any(sub in base for sub in HARD_LINE_SUBSTRINGS):
            hits.append(str(f))
    return hits


def _norm_agent(a: str) -> str:
    """Canonical agent identity for the distinct-signer check (red team RT-23/24):
    case- and whitespace-insensitive so 'Solo', 'solo', 'Solo ' are one entity."""
    return " ".join(str(a).split()).casefold()


def blast_radius_ok(files: list, added: int, removed: int) -> bool:
    # Clamp negatives (red team RT-26): a negative removed can't be used to net
    # a huge `added` down under the limit.
    lines = max(0, int(added or 0)) + max(0, int(removed or 0))
    return len(files or []) <= MAX_BLAST_FILES and lines <= MAX_BLAST_LINES


def _deployed_today_records() -> list:
    today = date.today().isoformat()
    return [c for c in _ledger().values()
            if (c.get("deployed_at") or "")[:10] == today]


def deploys_today() -> int:
    return len(_deployed_today_records())


def lines_deployed_today() -> int:
    return sum(int(c.get("added", 0)) + int(c.get("removed", 0))
              for c in _deployed_today_records())


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
    """Record a green/red test result AND the commit it ran against, so a PASS
    can't be reused after the code moves on (red team RT-16, staleness angle)."""
    led = _ledger()
    if change_id in led:
        led[change_id]["test_result"] = result
        led[change_id]["test_commit"] = _git_head()
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


def escalate_veto_to_board(change_id: str, note: str = "") -> dict:
    """Route an IRM risk-veto up to the board (which can override it by the hard
    bar in resolve_board_escalation). Refuses to escalate the two vetoes that are
    never board-overridable: a CANARY health veto and any hard-line file."""
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    v = rec.get("veto")
    if not v:
        return {"ok": False, "reason": "no veto to escalate"}
    if v.get("overridden_by_board"):
        return {"ok": False, "reason": "veto already overridden"}
    if rec.get("hard_line_hits"):
        return {"ok": False, "reason": "hard-line change — Chairman only, not board-overridable"}
    if v.get("role") == "CANARY":
        return {"ok": False, "reason": "Canary health veto is not board-overridable"}
    rec["board_escalation"] = {
        "reason_from_irm": v.get("reason"),
        "irm_agent": v.get("agent"),
        "note": note[:400],
        "votes": [],
        "opened_at": _now(),
    }
    rec["status"] = "ESCALATED_TO_BOARD"
    _save(LEDGER_PATH, led)
    return {"ok": True, "status": rec["status"]}


def board_vote(change_id: str, member: str, vote: str) -> dict:
    """Record one board member's vote on an escalated IRM veto.
    vote ∈ {OVERRIDE, UPHOLD}. One vote per member (re-voting updates)."""
    vote = str(vote).upper()
    if vote not in ("OVERRIDE", "UPHOLD"):
        raise ValueError("vote must be OVERRIDE or UPHOLD")
    led = _ledger()
    rec = led.get(change_id)
    esc = rec.get("board_escalation") if rec else None
    if not esc:
        raise ValueError(f"{change_id} is not escalated to the board")
    esc["votes"] = [x for x in esc["votes"]
                    if _norm_agent(x["member"]) != _norm_agent(member)]
    esc["votes"].append({"member": member, "vote": vote, "at": _now()})
    _save(LEDGER_PATH, led)
    return rec


def resolve_board_escalation(change_id: str) -> dict:
    """Tally the board vote. Override an IRM veto ONLY if: quorum met, the CIT
    Director voted, and OVERRIDE reached the 2/3 supermajority. On override the
    board supplies the IRM lens itself (it is taking the risk-sign responsibility)
    and reputation attaches to the board. Otherwise the veto is upheld → human."""
    led = _ledger()
    rec = led.get(change_id)
    esc = rec.get("board_escalation") if rec else None
    if not esc:
        return {"resolved": False, "reason": "not escalated"}
    votes = esc.get("votes", [])
    if len(votes) < BOARD_MIN_QUORUM:
        return {"resolved": False, "reason": f"quorum not met ({len(votes)}/{BOARD_MIN_QUORUM})"}
    cit_voted = any(_CIT_DIRECTOR_HINT in _norm_agent(v["member"]) for v in votes)
    n_override = sum(1 for v in votes if v["vote"] == "OVERRIDE")
    frac = n_override / len(votes)
    passed = cit_voted and frac >= BOARD_OVERRIDE_SUPERMAJORITY
    esc["resolved_at"] = _now()
    esc["tally"] = {"override": n_override, "total": len(votes),
                    "fraction": round(frac, 3), "cit_director_voted": cit_voted,
                    "threshold": round(BOARD_OVERRIDE_SUPERMAJORITY, 3)}
    if passed:
        rec["veto"]["overridden_by_board"] = True
        rec["veto"]["override_tally"] = esc["tally"]
        # The board assumes the IRM lens. Attach it if IRM hasn't otherwise signed.
        if not any(s["role"] == "IRM" for s in rec.get("signatures", [])):
            rec["signatures"].append({
                "role": "IRM", "agent": "Main Board (veto override)",
                "rationale": f"board {n_override}/{len(votes)} supermajority overrode "
                             f"IRM veto by {esc.get('irm_agent')}: {esc.get('reason_from_irm')}",
                "at": _now()})
        esc["outcome"] = "OVERRIDDEN"
        _refresh_status(rec)
    else:
        esc["outcome"] = "UPHELD"
        rec["status"] = "VETO_UPHELD_ROUTE_TO_HUMAN"
    _save(LEDGER_PATH, led)
    return {"resolved": True, "outcome": esc["outcome"], "tally": esc["tally"],
            "status": rec["status"]}


def _veto_active(rec: dict) -> bool:
    """A veto blocks unless the board has overridden it (IRM vetoes only)."""
    v = rec.get("veto")
    return bool(v) and not v.get("overridden_by_board")


def _refresh_status(rec: dict) -> None:
    if rec.get("status") == "ESCALATED_TO_BOARD" and _veto_active(rec):
        return                                    # awaiting the board's ruling
    if _veto_active(rec):
        rec["status"] = "VETOED_ROUTE_TO_HUMAN"; return
    if rec.get("hard_line_hits"):
        rec["status"] = "ROUTE_TO_HUMAN"; return
    ok, _ = _clearance_reasons(rec)
    rec["status"] = "CLEARED" if ok else "AWAITING_SIGNOFF"


def _clearance_reasons(rec: dict) -> tuple[bool, list]:
    reasons = []
    files = rec.get("files") or []
    # A change that declares no files can't be assessed for hard-line/blast — it
    # must not auto-clear (red team RT-25).
    if not files:
        reasons.append("no files declared — cannot assess hard-line/blast radius")
    # Non-ASCII filenames can smuggle a homoglyph past the substring match
    # (red team RT-27) — route anything exotic to a human.
    if any(any(ord(ch) > 127 for ch in str(f)) for f in files):
        reasons.append("non-ascii character in a filename — route to human")
    if rec.get("hard_line_hits"):
        reasons.append(f"touches hard-line file(s): {', '.join(rec['hard_line_hits'])}")
    if _veto_active(rec):
        v = rec["veto"]
        reasons.append(f"vetoed by {v['agent']} ({v['role']}): {v['reason']}")
    if rec.get("test_result") != "PASS":
        reasons.append(f"no green test+audit on record (test_result={rec.get('test_result')})")
    else:
        # The green result must have run against the code being deployed. If
        # HEAD has moved since the tests passed, the PASS is stale — re-test.
        head, tc = _git_head(), rec.get("test_commit")
        if head and tc and head != tc:
            reasons.append(f"green tests are stale — ran against {tc} but HEAD is now "
                           f"{head}; re-run tests before deploy")
    if not blast_radius_ok(rec.get("files"), rec.get("added"), rec.get("removed")):
        reasons.append(f"blast radius too large ({len(rec.get('files',[]))} files, "
                       f"{rec.get('added',0)+rec.get('removed',0)} lines > "
                       f"{MAX_BLAST_FILES}/{MAX_BLAST_LINES})")
    roles_signed = {s["role"] for s in rec.get("signatures", [])}
    missing = REQUIRED_ROLES - roles_signed
    if missing:
        reasons.append(f"missing required sign-off from: {', '.join(sorted(missing))}")
    # Dual sign-off is only real if the lenses are DIFFERENT agents. One agent
    # wearing both the CIT and IRM hats is self-approval, not independent review.
    role_agents = {role: {_norm_agent(s["agent"]) for s in rec.get("signatures", []) if s["role"] == role}
                   for role in REQUIRED_ROLES}
    if all(role_agents.get(r) for r in REQUIRED_ROLES):
        import itertools
        for ra, rb in itertools.combinations(REQUIRED_ROLES, 2):
            if role_agents[ra] & role_agents[rb]:
                dupe = ', '.join(sorted(role_agents[ra] & role_agents[rb]))
                reasons.append(f"same agent ({dupe}) signed both {ra} and {rb} lenses — "
                               f"dual sign-off requires two distinct agents")
    return (len(reasons) == 0, reasons)


def is_cleared(change_id: str) -> tuple[bool, list]:
    """Return (cleared, reasons). Also enforces the daily deploy budget."""
    rec = _ledger().get(change_id)
    if rec is None:
        return (False, ["unknown change"])
    ok, reasons = _clearance_reasons(rec)
    if ok and deploys_today() >= DAILY_DEPLOY_BUDGET:
        return (False, [f"daily deploy budget reached ({DAILY_DEPLOY_BUDGET}) — queues for Chairman"])
    change_lines = int(rec.get("added", 0)) + int(rec.get("removed", 0))
    if ok and lines_deployed_today() + change_lines > MAX_DAILY_DEPLOY_LINES:
        return (False, [f"daily line budget would be exceeded "
                        f"({lines_deployed_today()}+{change_lines} > {MAX_DAILY_DEPLOY_LINES}) "
                        f"— queues for Chairman (anti-split guard)"])
    return (ok, reasons)


def reserve_and_deploy(change_id: str) -> tuple[bool, list]:
    """Atomically re-check clearance + budget and stamp deployed in ONE
    load→verify→save, so two racing deployers can't both slip past the budget
    (red team RT-20). This is the only sanctioned way to mark a deploy."""
    ok, reasons = is_cleared(change_id)
    if not ok:
        return (False, reasons)
    led = _ledger()                       # re-read immediately before writing
    rec = led.get(change_id)
    if rec is None:
        return (False, ["unknown change"])
    if len([c for c in led.values()
            if (c.get("deployed_at") or "")[:10] == date.today().isoformat()]) >= DAILY_DEPLOY_BUDGET:
        return (False, ["budget consumed by a concurrent deploy — queues for Chairman"])
    rec["deployed_at"] = _now()
    rec["status"] = "DEPLOYED"
    _save(LEDGER_PATH, led)
    return (True, [])


def mark_deployed(change_id: str) -> None:
    """Thin wrapper; prefer reserve_and_deploy() which enforces the budget."""
    led = _ledger()
    if change_id in led:
        led[change_id]["deployed_at"] = _now()
        led[change_id]["status"] = "DEPLOYED"
        _save(LEDGER_PATH, led)


def record_outcome(change_id: str, outcome: str, reported_by: str = "unattributed") -> dict:
    """Close the loop: score the SIGNERS on how their change fared.
    outcome ∈ {HELD, REVERTED, REGRESSED}.

    Red team RT-21: outcome reporting is only as trustworthy as its provenance.
    Two guards: (1) you cannot score a change that never deployed — a HELD on a
    fix that never shipped is meaningless; (2) every outcome is stamped with the
    reporter and appended to an audit trail, so a self-serving HELD is at least
    attributable and reviewable (HR reads reputation; a pattern of one agent
    reporting only HELDs on its own sign-offs is now visible)."""
    outcome = outcome.upper()
    if outcome not in _OUTCOME_SIGNAL:
        raise ValueError(f"outcome must be one of {list(_OUTCOME_SIGNAL)}")
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    if not rec.get("deployed_at"):
        raise ValueError(f"cannot record an outcome for {change_id}: it never deployed")
    rec["outcome"] = outcome
    rec["outcome_at"] = _now()
    rec["outcome_reported_by"] = reported_by
    rec.setdefault("outcome_audit", []).append(
        {"outcome": outcome, "by": reported_by, "at": _now()})
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


def hr_flag(agent: str, window: int = 25) -> bool:
    """Should HR flag this signer? Uses BOTH the ewma AND a trailing burn count,
    so an agent can't farm trivial HELDs to lift its ewma above the threshold and
    hide a recent burn (red team RT-28). A burn stays visible until it ages out
    of the window — and the raw n_burned count never disappears."""
    r = reputation(agent)
    if not r or r.get("n_signed", 0) < 3:
        return False
    if r.get("ewma", 0.0) < -0.5:
        return True
    recent = r.get("history", [])[-window:]
    return any(h.get("outcome") in ("REVERTED", "REGRESSED") for h in recent)


def reputation(agent: str | None = None):
    """Signer reputation. ewma < 0 means recent sign-offs have been burning."""
    rep = _load(REPUTATION_PATH, {})
    if not isinstance(rep, dict):
        rep = {}
    if agent:
        return rep.get(agent, {"ewma": 0.0, "n_signed": 0, "n_held": 0, "n_burned": 0})
    return rep


# ---------------------------------------------------------------------------
# rollout mode (shadow -> enforce) + preflight gate for live actuators
# ---------------------------------------------------------------------------
def get_mode() -> str:
    """Current gate mode. Absent/invalid file → SHADOW (fail safe: never
    silently start blocking, and never silently start enforcing)."""
    m = _load(MODE_PATH, {})
    mode = (m or {}).get("mode") if isinstance(m, dict) else None
    return mode if mode in _VALID_MODES else "SHADOW"


def set_mode(mode: str, approvals: list, note: str = "") -> tuple[bool, str]:
    """Flip the gate mode. Going to ENFORCE requires a joint board+CIT vote —
    `approvals` must contain distinct entries covering BOTH {BOARD, CIT}
    (Chairman directive 2026-07-29: 'if the board AND the cit team agrees, it
    can be flipped to enforce'). Flipping BACK to SHADOW is always allowed
    (de-risking is never gated). Returns (ok, message)."""
    mode = str(mode).upper()
    if mode not in _VALID_MODES:
        return (False, f"mode must be one of {_VALID_MODES}")
    if mode == "ENFORCE":
        got = {str(a).strip().upper() for a in (approvals or [])}
        missing = _FLIP_APPROVERS_REQUIRED - got
        if missing:
            return (False, f"ENFORCE requires joint approval from {', '.join(sorted(missing))} "
                           f"(have: {', '.join(sorted(got)) or 'none'})")
    _save(MODE_PATH, {
        "mode": mode,
        "approvals": sorted({str(a).strip().upper() for a in (approvals or [])}),
        "note": note[:400],
        "set_at": _now(),
        "set_head": _git_head(),
    })
    return (True, f"gate mode set to {mode}")


def _shadow_log(entry: dict) -> None:
    log = _load(SHADOW_LOG_PATH, [])
    if not isinstance(log, list):
        log = []
    log.append(entry)
    _save(SHADOW_LOG_PATH, log[-500:])   # keep the tail bounded


def preflight(change_id: str, actuator: str = "unknown") -> dict:
    """The single choke point every autonomous actuator must call BEFORE it
    applies a code change. It answers one question: may this proceed?

      • SHADOW  → allow is ALWAYS True (nothing breaks), but if the change would
                  have been blocked we record it to the shadow log so the board +
                  CIT can watch the gate's judgement without risk.
      • ENFORCE → allow == is_cleared(): a would-block actually blocks and the
                  change routes to the Chairman queue.

    Returns {allow, mode, would_block, reasons, change_id, actuator}. Callers
    honour `allow`; they may surface `reasons` either way."""
    mode = get_mode()
    cleared, reasons = is_cleared(change_id)
    would_block = not cleared
    allow = True if mode == "SHADOW" else cleared
    result = {
        "change_id": change_id,
        "actuator": actuator,
        "mode": mode,
        "would_block": would_block,
        "allow": allow,
        "reasons": reasons,
        "at": _now(),
    }
    # SHADOW: log EVERY decision (blocks AND clears) so the promotion gate can
    # prove the sign-off path actually clears legitimate work — a gate that only
    # ever blocks means ENFORCE would be a full stop, not full auto.
    # ENFORCE: only the real blocks are worth recording.
    if mode == "SHADOW" or would_block:
        _shadow_log(result)
    return result


def shadow_summary() -> dict:
    """What the board + CIT review to decide the flip: how often the gate would
    have blocked, and why."""
    log = _load(SHADOW_LOG_PATH, [])
    if not isinstance(log, list):
        log = []
    from collections import Counter
    reason_heads = Counter()
    blocks = [e for e in log if e.get("would_block")]
    clears = [e for e in log if not e.get("would_block")]
    for e in blocks:
        for r in e.get("reasons", []):
            reason_heads[r.split(" (")[0].split(" —")[0][:60]] += 1
    return {
        "mode": get_mode(),
        "events": len(log),
        "would_block_events": len(blocks),
        "would_clear_events": len(clears),
        "distinct_changes": len({e.get("change_id") for e in log}),
        "distinct_would_clear_changes": len({e.get("change_id") for e in clears}),
        "top_reasons": reason_heads.most_common(8),
    }


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
