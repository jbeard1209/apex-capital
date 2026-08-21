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

import hashlib
import json
import os
import fnmatch
import socket
import tempfile
from datetime import datetime, date, timezone

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
# Turning the gate back OFF is also accountable (red team X3), but the Chairman
# alone suffices — a human must always be able to kill a misfiring guardrail.
_DOWNGRADE_APPROVERS = {"CHAIRMAN"}

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
    """UTC, always. Red team X10: `datetime.now()` is LOCAL, so the Mac (EDT)
    and the VM (UTC) rolled the daily deploy budget 4 hours apart — a change
    blocked by budget on one host was clearable on the other."""
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _host() -> str:
    return socket.gethostname().split(".")[0]


def _git_head() -> str | None:
    """Current HEAD short hash, or None if git is unavailable."""
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=BASE, capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except Exception:
        return None


def _files_fingerprint(files: list) -> str:
    """SHA of the CONTENT of every declared file.

    Red team X9: the staleness guard used to compare git HEAD, and the GCP VM is
    not a git checkout (deploy.sh rsyncs with --exclude=.git/). So `_git_head()`
    returned None there, the `if head and tc` check short-circuited, and the
    "green tests must have run against the deployed code" guardrail silently did
    not exist on the always-on host running 63 cron jobs. A content hash needs no
    git — and is strictly stronger, because it also catches an uncommitted edit
    made between the test run and the deploy."""
    h = hashlib.sha256()
    for f in sorted(str(x) for x in (files or [])):
        h.update(f.encode("utf-8", "replace") + b"\0")
        p = f if os.path.isabs(f) else os.path.join(BASE, os.path.basename(f))
        try:
            with open(p, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
        except Exception:
            h.update(b"<absent>")          # a file that does not exist yet
        h.update(b"\n")
    return h.hexdigest()[:16]


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


def _deployed_today_records(led: dict | None = None) -> list:
    """Deploys in the current UTC day, counted across EVERY host present in the
    ledger.

    Red team X10/X11: this used local `date.today()` (Mac EDT vs VM UTC — the
    budget window differed by 4h between the two brains) and the ledger itself
    is per-host uncommitted state, so each host granted itself a full budget.
    Counting host-stamped deploys keeps the cap firm-wide once data/ is synced;
    until then the residual per-host exposure is documented in DAILY_DEPLOY_BUDGET."""
    today = _today()
    return [c for c in (led if led is not None else _ledger()).values()
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
    prev = led.get(change_id)
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
        "vetoes":      [],                    # full history; see veto()
        "hard_line_hits": hard,
        "status":      "ROUTE_TO_HUMAN" if hard else "AWAITING_SIGNOFF",
        "outcome":     None,                  # HELD | REVERTED | REGRESSED
        "deployed_at": None,
    }
    # A PASS supplied at propose time is a real API path (most actuators use it),
    # so it must be fingerprinted here too — otherwise the X9 staleness guard
    # rejects it forever for "no fingerprint" and nothing on that path can ever
    # clear. Caught by RT-24 reporting a bypass that was actually total paralysis.
    if test_result is not None:
        rec["test_commit"] = _git_head()
        rec["test_fingerprint"] = _files_fingerprint(rec["files"])

    if prev is not None:
        # Red team X1 — THE most serious hole found: re-proposing an existing
        # change_id used to overwrite the record wholesale, which silently
        # erased a standing veto. Any actuator could defeat Canary by calling
        # propose_change() twice with the same id. A veto, a deploy stamp and a
        # scored outcome are FACTS ABOUT THE PAST; a new proposal cannot unmake
        # them.
        for k in ("veto", "vetoes", "deployed_at", "deploy_host", "outcome",
                  "outcome_at", "outcome_reported_by", "outcome_audit",
                  "scored_outcome", "blocked_signatures", "board_escalation"):
            if prev.get(k) is not None:
                rec[k] = prev[k]
        rec["proposed_at"] = prev.get("proposed_at", rec["proposed_at"])
        rec["reproposed_at"] = _now()
        # Signatures attach to a SPECIFIC payload. If the payload moved, the
        # signers reviewed something else — void their sign-off and the green
        # test rather than letting a new diff inherit old approvals.
        same_payload = (
            sorted(str(f) for f in (prev.get("files") or [])) == sorted(str(f) for f in (files or []))
            and int(prev.get("added", 0)) == int(added or 0)
            and int(prev.get("removed", 0)) == int(removed or 0)
        )
        if same_payload:
            rec["signatures"] = prev.get("signatures", [])
            rec["test_result"] = test_result if test_result is not None else prev.get("test_result")
            rec["test_commit"] = prev.get("test_commit")
            rec["test_fingerprint"] = prev.get("test_fingerprint")
        else:
            rec["payload_changed_at"] = _now()
        _refresh_status(rec)

    led[change_id] = rec
    _save(LEDGER_PATH, led)
    return rec


def attach_test_result(change_id: str, result: str) -> None:
    """Record a green/red test result AND a fingerprint of the code it ran
    against, so a PASS can't be reused after the code moves on (red team RT-16
    staleness angle; X9 for the no-git host)."""
    led = _ledger()
    if change_id in led:
        led[change_id]["test_result"] = result
        led[change_id]["test_commit"] = _git_head()
        led[change_id]["test_fingerprint"] = _files_fingerprint(led[change_id].get("files"))
        # X14: without this the cached `status` stayed AWAITING_SIGNOFF after the
        # last missing precondition was met, so --report and the board briefing
        # showed a state that disagreed with is_cleared().
        _refresh_status(led[change_id])
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


# A CANARY veto is a system-health kill switch and is never board-overridable;
# an IRM veto is a risk judgement the board may override at a hard bar. So the
# ACTIVE veto must always be the most severe one on record, never the most
# recent — see veto().
_VETO_SEVERITY = {"CANARY": 3, "CIT": 2, "IRM": 1}


def _strongest_veto(vetoes: list):
    live = [v for v in (vetoes or []) if not v.get("cleared_at")]
    if not live:
        return None
    return max(live, key=lambda v: (_VETO_SEVERITY.get(v.get("role"), 0), v.get("at", "")))


def veto(change_id: str, agent: str, reason: str, role: str = "CANARY") -> dict:
    """Record a veto. Vetoes ACCUMULATE — a later one never replaces an earlier
    one.

    Red team X2: `rec["veto"]` was a single slot that each call overwrote. So a
    non-overridable CANARY health veto could be laundered into a board-
    overridable IRM veto simply by calling veto() again with role='IRM' — and
    the board could then override the Canary kill switch it is explicitly never
    allowed to override. Keeping every veto and surfacing the most severe one
    makes that downgrade impossible."""
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    rec.setdefault("vetoes", [])
    if rec.get("veto") and rec["veto"] not in rec["vetoes"]:
        rec["vetoes"].append(rec["veto"])      # migrate a pre-existing single veto
    rec["vetoes"].append({"role": role.upper(), "agent": agent,
                          "reason": reason[:400], "at": _now()})
    rec["veto"] = _strongest_veto(rec["vetoes"])
    rec["status"] = "VETOED_ROUTE_TO_HUMAN"
    _save(LEDGER_PATH, led)
    return rec


def clear_veto(change_id: str, agent: str, reason: str, role: str | None = None) -> dict:
    """Withdraw vetoes, attributably.

    Red team X13: there was no way to lift a veto once conditions changed (e.g.
    Canary recovers to GREEN), so the only escape was the X1 re-propose hole —
    a legitimate need served by an illegitimate mechanism. Withdrawal is now
    explicit, named, and logged; `role=None` withdraws all of them."""
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    if not (reason or "").strip():
        raise ValueError("clearing a veto requires a stated reason")
    want = role.upper() if role else None
    n = 0
    for v in rec.get("vetoes", []):
        if v.get("cleared_at"):
            continue
        if want and v.get("role") != want:
            continue
        v["cleared_at"] = _now(); v["cleared_by"] = agent; v["cleared_reason"] = reason[:400]
        n += 1
    rec["veto"] = _strongest_veto(rec.get("vetoes"))
    _refresh_status(rec)
    _save(LEDGER_PATH, led)
    return {"cleared": n, "status": rec["status"]}


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
        # The green result must have run against the code being deployed.
        # Content fingerprint first: it works on the VM, which has no .git, and
        # it also catches an uncommitted edit inside the same commit. A PASS with
        # no fingerprint at all predates this guard and is treated as stale
        # rather than trusted — fail toward re-testing (red team X9).
        fp_now, fp_test = _files_fingerprint(files), rec.get("test_fingerprint")
        head, tc = _git_head(), rec.get("test_commit")
        if not fp_test:
            reasons.append("green tests carry no code fingerprint — re-run tests "
                           "so the PASS can be tied to the code being deployed")
        elif fp_now != fp_test:
            reasons.append(f"green tests are stale — ran against code fingerprint "
                           f"{fp_test} but the declared files now hash {fp_now}; re-run tests")
        elif head and tc and head != tc:
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


class _DeployLock:
    """Advisory inter-PROCESS lock around the reserve→deploy critical section.

    Honest about its limit (red team X8: the old docstring claimed atomicity
    the code did not provide): flock serialises the ~78 cron jobs on a single
    host, which is where the real race is. It does NOT synchronise the Mac
    against the VM — they have separate filesystems.

    2026-08-07: the sentence that used to end this docstring — "so the firm-wide
    budget still rests on both hosts sharing data/ via sync_to_cloud" — named a
    mechanism that does not exist. `sync_to_cloud.py` pushed to Railway, which
    was decommissioned 2026-07-10; the surviving copies under dashboard/ and
    cloud/ still POST to a dead URL. There is NO shared data/ between hosts and
    has not been for a month, so DAILY_DEPLOY_BUDGET and MAX_DAILY_DEPLOY_LINES
    are enforced PER HOST, not firm-wide.

    That is currently harmless for one reason only: the sole production caller of
    reserve_and_deploy is cit_implementation_agent (directly, and via
    overnight_board_sprint), and neither is on the VM's crontab — so exactly one
    host spends the budget. That is an accident of scheduling, not a guarantee.
    Schedule the deployer on the VM and the firm silently gets 2x its own limit,
    the same shape as the two-host trading fork (see single_writer.py). Guarded
    by test_pipeline_smoke.py::TestDeployBudgetIsSingleHost, which fails if the
    deployer ever appears in data/vm_crontab_snapshot.txt while the ledger is
    still per-host."""

    def __init__(self):
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
            os.makedirs(DATA, exist_ok=True)
            self.fh = open(os.path.join(DATA, ".signoff_deploy.lock"), "w")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self.fh = None            # locking unavailable — proceed unserialised
        return self

    def __exit__(self, *exc):
        try:
            if self.fh:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                self.fh.close()
        except Exception:
            pass
        return False


def reserve_and_deploy(change_id: str) -> tuple[bool, list]:
    """Re-check clearance + budget and stamp deployed inside a host-level lock,
    so two racing deployers can't both slip past the budget (red team RT-20).
    This is the only sanctioned way to mark a deploy."""
    with _DeployLock():
        ok, reasons = is_cleared(change_id)
        if not ok:
            return (False, reasons)
        led = _ledger()                   # re-read inside the lock, before writing
        rec = led.get(change_id)
        if rec is None:
            return (False, ["unknown change"])
        # Both budgets (count AND the anti-split line cap) are enforced by the
        # is_cleared() call above, which re-reads the ledger from disk INSIDE this
        # lock — so it already sees any concurrent deploy that landed since the
        # caller last checked. An earlier version of this function re-verified the
        # line cap here as well; mutation testing proved that branch could never
        # fire, and its comment claimed it was closing a hole that is_cleared()
        # already closes. Dead code carrying a false safety claim is worse than no
        # code: it makes the next reader think two things are guarding the door.
        rec["deployed_at"] = _now()
        rec["deploy_host"] = _host()
        rec["status"] = "DEPLOYED"
        _save(LEDGER_PATH, led)
        return (True, [])


def mark_deployed(change_id: str) -> tuple[bool, list]:
    """Deprecated alias for reserve_and_deploy().

    Red team X6: this used to stamp DEPLOYED directly, skipping every budget
    check — an unguarded door standing next to the guarded one. It had no
    production callers, so rather than leave the hole for a future actuator to
    find, it now delegates. Kept only so existing call sites keep working."""
    return reserve_and_deploy(change_id)


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
    # X12: `reported_by` defaulted to the literal string "unattributed", so the
    # attribution requirement was satisfied by not attributing. An outcome that
    # moves reputation must name who reported it.
    reporter = (reported_by or "").strip()
    if not reporter or reporter.lower() in {"unattributed", "unknown", "none", "n/a"}:
        raise ValueError("record_outcome requires a named reporter — reputation "
                         "moves on this, so an anonymous outcome is not acceptable")
    led = _ledger()
    rec = led.get(change_id)
    if rec is None:
        raise KeyError(f"unknown change {change_id}")
    if not rec.get("deployed_at"):
        raise ValueError(f"cannot record an outcome for {change_id}: it never deployed")
    rec["outcome"] = outcome
    rec["outcome_at"] = _now()
    rec["outcome_reported_by"] = reporter
    rec.setdefault("outcome_audit", []).append(
        {"outcome": outcome, "by": reporter, "at": _now()})

    # X5: reputation used to move on EVERY call, so reporting "HELD" on one
    # deployed change twelve times scored the signers twelve times — free
    # reputation from a single fix, which is exactly the rubber-stamp incentive
    # this ledger exists to remove. Score only on a genuine CHANGE of outcome
    # (HELD → REVERTED must still burn the signers), never on a repeat.
    already = rec.get("scored_outcome")
    rec["scored_outcome"] = outcome
    _save(LEDGER_PATH, led)
    if already == outcome:
        return rec
    signal = _OUTCOME_SIGNAL[outcome]
    for s in rec.get("signatures", []):
        _apply_reputation(s["agent"], signal, change_id, outcome)
    return rec


def _apply_reputation(agent: str, signal: float, change_id: str, outcome: str) -> None:
    # X4: reputation was keyed on the RAW agent string while the distinct-signer
    # check normalised — so "CIT-Bot" carrying an ewma of -0.9 could sign again
    # as "cit-bot " and present a clean record. Identity must mean the same thing
    # everywhere, or a bad reputation is opt-out.
    key = _norm_agent(agent)
    rep = _load(REPUTATION_PATH, {})
    if not isinstance(rep, dict):
        rep = {}
    r = rep.get(key) or rep.get(agent) or {"ewma": 0.0, "n_signed": 0, "n_held": 0,
                                           "n_burned": 0, "history": []}
    rep.pop(agent, None)                       # fold any legacy raw-name record in
    r["display_name"] = agent
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
    rep[key] = r
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
        # Look up on the same normalised identity the ledger scores on (X4), with
        # a raw-name fallback for records written before that fix.
        return (rep.get(_norm_agent(agent)) or rep.get(agent)
                or {"ewma": 0.0, "n_signed": 0, "n_held": 0, "n_burned": 0})
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


def mode_provenance() -> tuple:
    """(mode, source, detail) — where this host's mode actually came from.

    get_mode() collapses two different states into the same string, and it must
    keep doing so: every caller needs a mode, and SHADOW is the right default.
    But "the firm decided SHADOW" and "this host has no idea" are not the same
    fact, and the distinction is the whole safety of the ENFORCE flip.

    Measured 2026-08-21: the Mac carried a recorded SHADOW from 2026-07-29; the
    VM had NO FILE AT ALL, and never had — signoff_mode.json was tracked in git
    but missing from deploy.sh's data/ allowlist, and the VM is not a checkout.
    Both hosts returned "SHADOW", so the gap was invisible from the value alone.
    It cost nothing while the answer happened to match the default. It stops
    costing nothing the moment board+CIT vote ENFORCE: that write lands on one
    host, and the other keeps actuating on a default it cannot tell from a
    decision, against the same broker account.

    `source` is "recorded" (a set_mode() write is present and valid), "default"
    (no file — this host is running the fallback, not a decision), or "invalid"
    (a file exists but does not parse to a known mode, which is a corrupted
    record and must never read as a clean default).
    """
    raw = _load(MODE_PATH, None)
    if raw is None or raw == {}:
        if not os.path.exists(MODE_PATH):
            return ("SHADOW", "default",
                    "no %s on this host — running the fallback, not a decision"
                    % os.path.basename(MODE_PATH))
        return ("SHADOW", "invalid", "%s is present but empty or unreadable"
                % os.path.basename(MODE_PATH))
    mode = raw.get("mode") if isinstance(raw, dict) else None
    if mode not in _VALID_MODES:
        return ("SHADOW", "invalid",
                "%s holds mode=%r, which is not one of %s"
                % (os.path.basename(MODE_PATH), mode, _VALID_MODES))
    return (mode, "recorded", "set %s on %s at head %s"
            % (raw.get("set_at") or "?", raw.get("set_host") or "an unnamed host",
               raw.get("set_head") or "?"))


def set_mode(mode: str, approvals: list, note: str = "") -> tuple[bool, str]:
    """Flip the gate mode. Going to ENFORCE requires a joint board+CIT vote —
    `approvals` must contain distinct entries covering BOTH {BOARD, CIT}
    (Chairman directive 2026-07-29: 'if the board AND the cit team agrees, it
    can be flipped to enforce'). Flipping BACK to SHADOW is always allowed
    (de-risking is never gated). Returns (ok, message).

    PROPAGATION: this writes a per-host file and there is no state sync between
    hosts. signoff_mode.json is in deploy.sh's data/ allowlist as of 2026-08-21,
    and deploy is one-way Mac→VM, so a flip is only firm-wide once it is
    recorded ON THE MAC, committed, and deployed. Flip it on the VM and the next
    deploy overwrites it with the Mac's copy — which is the right precedence
    (one checkout, one deploying host, one decision), but it means the vote must
    be recorded where the deploy originates. mode_provenance() is how any host
    reports that it is running a default rather than a decision."""
    mode = str(mode).upper()
    if mode not in _VALID_MODES:
        return (False, f"mode must be one of {_VALID_MODES}")
    got = {str(a).strip().upper() for a in (approvals or [])}
    prev = get_mode()
    if mode == "ENFORCE":
        missing = _FLIP_APPROVERS_REQUIRED - got
        if missing:
            return (False, f"ENFORCE requires joint approval from {', '.join(sorted(missing))} "
                           f"(have: {', '.join(sorted(got)) or 'none'})")
    elif prev == "ENFORCE":
        # Red team X3 — the gate could switch itself off. Downgrading was
        # unconditional on the reasoning that "de-risking is never gated", which
        # is true for a human and false for the actuator being gated: any code
        # blocked by ENFORCE could call set_mode("SHADOW", []) and walk straight
        # through. Turning the gate OFF is now as accountable as turning it on,
        # with a Chairman fast-path so a misfiring gate can still be killed.
        if not (got & _DOWNGRADE_APPROVERS) and not _FLIP_APPROVERS_REQUIRED <= got:
            return (False, "leaving ENFORCE requires CHAIRMAN, or joint BOARD+CIT "
                           f"(have: {', '.join(sorted(got)) or 'none'})")
    _save(MODE_PATH, {
        "mode": mode,
        "approvals": sorted(got),
        "note": note[:400],
        "set_at": _now(),
        "set_head": _git_head(),
        "previous_mode": prev,
        "set_host": _host(),
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
    # Provenance travels with the mode. This dict is what the board and CIT read
    # to decide the ENFORCE flip, and "SHADOW" alone does not tell them whether
    # they are looking at a decision or at a host that never received one.
    mode, mode_source, mode_detail = mode_provenance()
    return {
        "mode": mode,
        "mode_source": mode_source,
        "mode_detail": mode_detail,
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
    mode, src, detail = mode_provenance()
    print("=== Sign-off ledger ===")
    print(f"  gate mode: {mode}  ({src}) — {detail}")
    if src != "recorded":
        print("  ⚠  this host is not running a recorded decision. If the firm has "
              "flipped the gate,\n     that flip did not reach here.")
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
