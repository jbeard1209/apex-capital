#!/usr/bin/env python3
"""
Board Issue Ledger — the closed loop between a warning and its fix
================================================================================
Chairman directive, 2026-08-17. The daily digest had been printing lines like

    ⚠ R&D report is 62h old — research may be stale

every morning for weeks. The warning was true, it was accurate, and nothing
happened as a result of it. A digest that reports a problem to a human who
cannot personally fix it, with no route to whoever can, is a newsletter about
the firm's decay. So warnings now enter a ledger with a lifecycle:

    DETECTED -> RELAYED -> ONGOING -> FIXED -> RETIRED

and the digest line changes as the issue moves:

    ⚠ R&D report is 62h old — research may be stale:
        this information has been relayed to the board
    ... — ongoing fix by board (<explanation>) (<follow-up date>)
    ... — fixed by board (<explanation>) (<date the board handled it>)

RETIRED items are never mentioned again, which is the point of the state.

WHAT THIS DELIBERATELY DOES NOT DO
--------------------------------------------------------------------------------
It does not define what "one issue" means. `problem_lifecycle_sentinel
.signature()` already does, and it is imported rather than reimplemented. The
firm's costliest recurring bug is two components holding two definitions of one
fact (see `single_writer.py`, and the monotonicity law that carried a private
copy of the scanner's coherence test and was wrong 283 times out of 287). A
second normalizer here would mean the ledger and the sentinel could disagree
about whether today's "74h old" is yesterday's "62h old" — and they would
disagree silently, each looking correct in isolation.

It also does not re-implement escalation-on-recurrence. Relayed items land in
`cit_pending_proposals.json`, which the sentinel already scans, so a fix that
keeps failing becomes its LOOPING case and goes to the full board as an
ARCHITECTURE item. This module feeds that machine; it does not compete with it.

THE RULE THAT MAKES THIS MORE THAN BOOKKEEPING
--------------------------------------------------------------------------------
**The board may not declare its own fix successful.** A vote is a decision, not
an outcome. Promotion ONGOING -> FIXED requires the ORIGINATING DETECTOR to have
gone quiet: the alert that raised the issue must not have re-fired between the
board's ruling and its follow-up date. If it re-fires, the issue returns to
ONGOING with a strike, and the strike is what eventually makes it the sentinel's
LOOPING problem. Without this rule "fixed by board" means "the board said so",
which is exactly the class of claim the firm has been burned by
(memory `plausible-is-not-validated`).

And the trap inside that rule: **silence is only evidence if someone was
listening.** If `morning_briefing.py` stops running, its alerts stop appearing,
and a naive reading promotes every outstanding issue to FIXED on the strength of
an observer that died. That is `presence-is-not-currency` in its purest form. So
every observer records a heartbeat on every run *including runs that produce no
alerts*, and promotion requires the observer to have actually run since the
ruling. When it has not, the verdict is CANNOT_TELL — which defers, names an
owner, and expires to the Chairman rather than quietly resolving
(memory `verdict-with-no-consequence`).

HOST SCOPE
--------------------------------------------------------------------------------
`data/board_issue_ledger.json` is per-host runtime state; the hosts share no
medium but the broker (CLAUDE.md). The observers and the digest both run on the
VM, so the ledger the digest reads is the ledger the observers wrote.

But `catch_up.py` ALSO runs `morning_briefing.py` and `board_meeting.py` on the
Mac, against the Mac's own independent `data/`. Left ungated, this module would
therefore run a second, complete governance loop there: the Mac's briefing files
an issue into the Mac's `cit_pending_proposals.json`, the Mac's board rules on
it, and the Mac's ledger records a fix — with its own explanation and its own
follow-up date, differing from the VM's, and rendered to nobody, because #88's
designated-host gate means only the VM sends the digest. Two boards ruling
independently on one issue while one verdict is invisible is strictly worse than
one board, and it is the shape of the Oracle third host (memory
`oracle_third_host`) at small scale.

So `relay()` is gated on `single_writer.designated_host()` and FAILS CLOSED:
observing is local and useful, but filing to a board queue nobody reads is the
silent half. The gate direction matches `email_buffer._may_send_digest` for the
same reason — a wrong "yes" creates a phantom governance track, a wrong "no"
delays a relay by hours on a host whose ledger is intact and will relay on the
next run. Nothing is lost by staying quiet, so when it cannot tell, it does.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("board_issue_ledger")

DATA = Path(__file__).resolve().parent / "data"
LEDGER_PATH = DATA / "board_issue_ledger.json"

# Consumers, all verified to exist (see TestBoardIssueLedger):
PENDING_PATH = DATA / "cit_pending_proposals.json"      # read by deputy_board + board_meeting
CHAIRMAN_PATH = DATA / "chairman_action_required.json"  # read by chairman_decision_sla
DEPUTY_REPORT = DATA / "deputy_board_report.json"
BOARD_REPORT = DATA / "board_meeting_report.json"

# ── policy ───────────────────────────────────────────────────────────────────
RELAY_AFTER_SIGHTINGS = 2      # one sighting can be a blip; two is a pattern
MAX_RELAYS_PER_RUN = 3         # pace the intake — see below
#
# MAX_RELAYS_PER_RUN exists because the first live dry run filed NINE proposals
# in a single pass, into a board queue that already held thirty — and one of the
# nine alerts was "CIT: 30 proposals pending board vote — queue getting long".
# Relaying that one lengthens the queue it is complaining about. The loop would
# have been a mild positive feedback loop on the board's own backlog, arriving
# as a burst nobody could act on, which is how a governance channel gets ignored
# and then ignored permanently.
#
# The cap paces intake instead of discarding it: what does not go this run is
# still DETECTED, still counted in the digest's denominator, and goes next run.
# Ordering is most-persistent-first (sightings desc, then oldest), so the thing
# that has been wrong longest is the thing the board sees first.
DEFAULT_FOLLOW_UP_DAYS = 7     # how long a board fix gets to prove itself
FOLLOW_UP_GRACE_DAYS = 3       # slack past the date before the lapse escalates
MAX_STRIKES = 2                # failed fixes before the sentinel's structural path
RETIRE_AFTER_SHOWN = 1         # Jack: once reported fixed, "it shouldnt be mentioned again"
QUIET_WINDOW_DAYS = 2          # silence required AT the deadline, not merely at the instant
#
# QUIET_WINDOW_DAYS exists because "not firing right now" and "fixed" are not the
# same claim. Observers here run on cron — morning_briefing once a day, others
# every few hours — so at any given second most detectors are between runs and
# every issue looks quiet. Demanding a window wider than the slowest observer's
# period means the silence had to survive at least one real opportunity to fire.
# Raise this if an observer is ever scheduled less often than every 2 days;
# a window narrower than the observer's period silently reads "hasn't run yet"
# as "problem solved", which is the exact substitution this module exists to
# refuse.

DETECTED, RELAYED, ONGOING, FIXED, RETIRED = (
    "DETECTED", "RELAYED", "ONGOING", "FIXED", "RETIRED")

# One definition of "the same issue", imported, never copied. See module docstring.
try:
    from problem_lifecycle_sentinel import normalize_title, signature  # noqa: F401
except Exception:  # pragma: no cover - the sentinel is a hard dependency
    raise


def _now() -> str:
    return datetime.now().isoformat()


def _load(path: Path, default):
    try:
        with open(path) as f:
            obj = json.load(f)
        return obj if isinstance(obj, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path: Path, obj) -> None:
    try:
        os.makedirs(path.parent, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"  [IssueLedger] could not write {path.name}: {e}")


def _days_since(iso: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(str(iso)[:26])
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now()) - then).total_seconds() / 86400.0


def _blank() -> dict:
    return {"issues": {}, "observers": {}, "version": 1}


def load_ledger() -> dict:
    led = _load(LEDGER_PATH, {})
    if not isinstance(led, dict) or "issues" not in led:
        return _blank()
    led.setdefault("observers", {})
    return led


# ─────────────────────────────────────────────────────────────────────────────
# Observation
# ─────────────────────────────────────────────────────────────────────────────

def alert_text(a) -> str:
    """One alert, as text, whatever shape it arrived in.

    `morning_briefing.py` appends BOTH plain strings and
    `{"source","severity","message"}` dicts to the same `alerts` list, and its
    email template interpolates each entry directly into an <li>. So the dict
    alerts have been reaching Jack rendered as Python reprs —
    `{'source': 'Logic Team', 'severity': 'HIGH', ...}` — for as long as the
    Logic Team and Freshness Sentinel injections have existed. Normalizing here
    is not incidental tidying: an unnormalized dict stringifies with its
    `days_open` counter inside, so its signature would change every single day
    and the ledger would see one recurring issue as an endless stream of new
    ones, never reaching the two sightings that trigger a relay.
    """
    if isinstance(a, dict):
        src = str(a.get("source") or "").strip()
        msg = str(a.get("message") or a.get("detail") or a.get("title") or "").strip()
        sev = str(a.get("severity") or "").strip()
        return (f"{src}: " if src else "") + msg + (f" [{sev}]" if sev else "")
    return str(a)


def observe(alerts: List, source: str, ledger: Optional[dict] = None,
            save: bool = True) -> dict:
    """Record this observer's run and every alert it raised.

    CALL THIS EVEN WHEN `alerts` IS EMPTY. The empty call is not a no-op — it is
    the heartbeat that makes a later silence mean "the detector looked and found
    nothing" instead of "the detector was not there". Every promotion to FIXED
    rests on that distinction, and an observer that only reports when it has
    something to say cannot support it.
    """
    led = ledger if ledger is not None else load_ledger()
    now = _now()
    led["observers"][source] = {
        "last_ran": now,
        "last_alert_count": len(alerts),
    }
    for raw in alerts:
        text = alert_text(raw).strip()
        if not text:
            continue
        sig = signature({"title": text}, f"alert:{source}")
        rec = led["issues"].get(sig)
        if rec is None:
            rec = {
                "sig": sig,
                "state": DETECTED,
                "source": source,          # kept LOCAL — never sent to the board
                "first_seen": now,
                "last_seen": now,
                "sightings": 1,
                "latest_text": text,
                "strikes": 0,
                "shown_fixed": 0,
                "history": [{"at": now, "to": DETECTED, "why": "first sighting"}],
            }
            led["issues"][sig] = rec
        else:
            rec["last_seen"] = now
            rec["sightings"] = int(rec.get("sightings", 0)) + 1
            rec["latest_text"] = text
    if save:
        _save(LEDGER_PATH, led)
    return led


def annotate(text: str, source: str, ledger: Optional[dict] = None) -> str:
    """Return the alert line with its current board status appended.

    This is what turns the digest from a list of complaints into a status
    report. An un-relayed alert is returned unchanged: claiming it reached the
    board before it did would be the worst possible failure of this feature,
    because Jack would stop chasing something nobody has.

    `source` is REQUIRED and must be the same string the caller passed to
    `observe()`. It is namespaced into the signature, so a caller that observes
    as "morning_briefing" and annotates as "briefing" would compute a different
    signature, find no record, and silently return every line unannotated —
    output produced, nothing acted on, no error raised. That is the firm's house
    bug (memory `wiring_contract_bugs`), so the argument is positional and
    mandatory rather than a default that can be forgotten, and
    `TestBoardIssueLedger` AST-checks that each caller uses one string for both.
    """
    led = ledger if ledger is not None else load_ledger()
    sig = signature({"title": str(text)}, f"alert:{source}")
    rec = led["issues"].get(sig)
    if not rec:
        return text
    state = rec.get("state")
    if state == RELAYED:
        return f"{text}: this information has been relayed to the board"
    if state == ONGOING:
        return (f"{text}: ongoing fix by board ({rec.get('explanation', 'no reason recorded')}) "
                f"({str(rec.get('follow_up_by', ''))[:10]})")
    if state in (FIXED, RETIRED):
        return (f"{text}: fixed by board ({rec.get('explanation', 'no reason recorded')}) "
                f"({str(rec.get('fixed_at', ''))[:10]})")
    return text


def process(alerts: List, source: str) -> List[str]:
    """Observe, relay what has earned it, and return the display lines.

    THE ONE CALL AN OBSERVER MAKES. Relaying happens inside it, before the
    annotation is read, so an alert that crosses the threshold this run is
    already marked "relayed to the board" in the very digest that reports it.
    Observing in one place and annotating in another would leave a day's lag in
    which the digest shows a bare warning that has in fact already gone out —
    and Jack would keep chasing something the board already holds.
    """
    led = observe(alerts, source, save=False)
    relay(led, save=False)
    _save(LEDGER_PATH, led)
    return [annotate(alert_text(a), source, led) for a in alerts]


# ─────────────────────────────────────────────────────────────────────────────
# Relay — anonymous, by directive
# ─────────────────────────────────────────────────────────────────────────────

def _board_item(rec: dict, now: str) -> dict:
    """The proposal as the board sees it.

    ANONYMOUS BY DIRECTIVE. `source` — which agent noticed — is deliberately
    absent, and `TestBoardIssueLedger` asserts it stays absent. The board is
    meant to weigh the finding, not the finder; the firm has HR ratings and a
    performance ledger keyed to agent identity, and an operational warning that
    arrives stamped with its author invites the vote to become a judgement about
    that author. The identity is not destroyed, it is kept here in the ledger,
    so an audit can still answer "who raised this" without the board seeing it.
    """
    strikes = int(rec.get("strikes", 0))
    # The id carries the attempt number; the dedup_key does not. That split is
    # load-bearing. A re-filed issue needs a NEW id or `_append_deduped` refuses
    # it — the first proposal is still sitting in the queue, already ruled on, so
    # a stable id would leave a struck issue stuck in DETECTED forever, silently
    # never reaching the board again. A stable dedup_key is what lets
    # `board_meeting._route_approved_cit_proposals` recognise the same finding
    # across attempts and flag it as persistent, which is precisely what a
    # failed fix should look like from the board's side.
    return {
        "id": f"issue-{rec['sig']}-a{strikes}",
        "source": "Independent Risk Monitor (anonymous)",
        "category": "ARCHITECTURE" if strikes else "INTEGRATION",
        "severity": "HIGH" if strikes else "MEDIUM",
        "confidence": "HIGH",
        "title": f"[OPS] {rec.get('latest_text', '')[:110]}",
        "dedup_key": f"issue_{rec['sig']}",
        "description": (
            f"An automated health check has raised this {rec.get('sightings', 1)}x since "
            f"{str(rec.get('first_seen', ''))[:10]} and it is still being raised. "
            f"Latest wording: \"{rec.get('latest_text', '')}\". "
            + (f"A previous board fix did not hold ({strikes} failed attempt(s)); this needs a "
               f"structural remedy, not another retry. " if strikes else "")
            + "The board is asked to rule and to name a follow-up date. The fix is NOT "
              "recorded as successful on the vote — it is recorded as successful only if "
              "the detector that raised this stays quiet through that date."
        ),
        "proposed_fix": "Board to determine; requires an explanation and a follow-up date.",
        "status": "pending",
        "ts": now,
    }


def _append_deduped(path: Path, entry: dict, key_field: str = "id") -> bool:
    items = _load(path, [])
    for it in items:
        if isinstance(it, dict) and it.get(key_field) == entry.get(key_field):
            return False
    items.append(entry)
    _save(path, items)
    return True


def may_relay() -> Tuple[bool, str]:
    """(allowed, why) — is THIS host the one whose board queue actually gets read?

    Fails closed. See HOST SCOPE in the module docstring: the Mac runs the same
    briefing and board scripts against its own `data/`, so an ungated relay
    creates a second governance track that no digest ever shows.
    """
    try:
        import single_writer
        designated = single_writer.designated_host()
        host = single_writer.this_host()
    except Exception as e:
        return False, f"cannot establish host identity ({e}) — not relaying"
    if not designated:
        return False, "no designated host recorded — not relaying"
    if host != designated:
        return False, (f"{host} is not the designated host ({designated}); its board "
                       f"queue is not the one the digest reports on")
    return True, f"this host ({host}) is the designated host"


def relay(ledger: Optional[dict] = None, save: bool = True) -> List[str]:
    """Send every issue that has earned it to the board. Idempotent."""
    led = ledger if ledger is not None else load_ledger()
    allowed, why = may_relay()
    if not allowed:
        # Recorded, not merely skipped. `digest_section` reads this so a host
        # that has been silently standing down for weeks is visible as a fact
        # rather than as an absence of relays nobody thinks to look for.
        led["relay_standdown"] = {"at": _now(), "why": why}
        if save:
            _save(LEDGER_PATH, led)
        logger.info(f"  [BoardIssueLedger] not relaying — {why}")
        return []
    led.pop("relay_standdown", None)
    now = _now()
    sent = []
    ready = [(sig, rec) for sig, rec in led["issues"].items()
             if rec.get("state") == DETECTED
             and int(rec.get("sightings", 0)) >= RELAY_AFTER_SIGHTINGS]
    ready.sort(key=lambda kv: (-int(kv[1].get("sightings", 0)),
                              str(kv[1].get("first_seen", ""))))
    led["relay_backlog"] = max(0, len(ready) - MAX_RELAYS_PER_RUN)
    for sig, rec in ready[:MAX_RELAYS_PER_RUN]:
        item = _board_item(rec, now)
        if _append_deduped(PENDING_PATH, item):
            rec["state"] = RELAYED
            rec["relayed_at"] = now
            rec["board_ref"] = item["id"]      # must match, or rulings never land
            rec.setdefault("history", []).append(
                {"at": now, "to": RELAYED, "why": f"{rec.get('sightings')} sightings"})
            sent.append(sig)
    if save and sent:
        _save(LEDGER_PATH, led)
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Board rulings
# ─────────────────────────────────────────────────────────────────────────────

def _rulings() -> Dict[str, Tuple[str, str]]:
    """{proposal_id: (classification, explanation)} from the deputy/full board."""
    out: Dict[str, Tuple[str, str]] = {}
    rep = _load(DEPUTY_REPORT, {})
    for r in (rep.get("results") or []):
        if not isinstance(r, dict):
            continue
        pid = r.get("proposal_id")
        if not pid:
            continue
        why = ""
        for v in (r.get("votes") or []):
            if isinstance(v, dict) and v.get("rationale"):
                why = str(v["rationale"])
                break
        out[pid] = (str(r.get("classification") or "").upper(), why)
    return out


def ingest_rulings(ledger: Optional[dict] = None, save: bool = True,
                   now: Optional[datetime] = None) -> List[str]:
    """Move RELAYED -> ONGOING for issues the board has actually ruled on."""
    led = ledger if ledger is not None else load_ledger()
    rulings = _rulings()
    stamp = (now or datetime.now())
    moved = []
    for sig, rec in led["issues"].items():
        if rec.get("state") != RELAYED:
            continue
        ruling = rulings.get(rec.get("board_ref", ""))
        if not ruling:
            continue
        classification, why = ruling
        if classification not in ("APPROVED", "AUTO_IMPLEMENT", "IMPLEMENTED"):
            # DEFERRED / NEEDS_INFO / REJECTED are not fixes. The issue stays
            # RELAYED so the digest keeps saying so, and the sentinel's STALE
            # path is what eventually forces the question.
            continue
        rec["state"] = ONGOING
        rec["explanation"] = (why or "board approved a remedy")[:180]
        rec["ruled_at"] = stamp.isoformat()
        rec["follow_up_by"] = (stamp + timedelta(days=DEFAULT_FOLLOW_UP_DAYS)).isoformat()
        rec.setdefault("history", []).append(
            {"at": stamp.isoformat(), "to": ONGOING, "why": classification})
        moved.append(sig)
    if save and moved:
        _save(LEDGER_PATH, led)
    return moved


# ─────────────────────────────────────────────────────────────────────────────
# Verification — the board does not get to mark its own homework
# ─────────────────────────────────────────────────────────────────────────────

def _observer_ran_since(led: dict, source: str, since_iso: str) -> Optional[bool]:
    """Did `source` run after `since_iso`? None if we have never seen it run."""
    obs = led.get("observers", {}).get(source)
    if not obs or not obs.get("last_ran"):
        return None
    try:
        return datetime.fromisoformat(str(obs["last_ran"])[:26]) > \
               datetime.fromisoformat(str(since_iso)[:26])
    except (TypeError, ValueError):
        return None


def verify(ledger: Optional[dict] = None, save: bool = True,
           now: Optional[datetime] = None) -> dict:
    """Promote, demote, or escalate every ONGOING issue.

    Three outcomes and no fourth:
      FIXED          the detector was listening and had gone quiet by the date
      ONGOING+strike the date arrived and the detector was still firing
      CANNOT_TELL    the detector was not running, so silence proves nothing;
                     past the grace period this becomes a Chairman item rather
                     than an answer, because an unanswerable question that never
                     escalates is indistinguishable from a resolved one.

    NOTHING IS JUDGED BEFORE THE FOLLOW-UP DATE, and that is the whole point of
    having one. The first version of this function struck an issue the moment the
    detector fired again after the ruling — which is wrong, and wrong in the
    direction that destroys the feature. The board's vote APPROVES a remedy; it
    does not deploy it. Between the ruling and the fix landing, the detector is
    supposed to keep firing, because the condition it watches is genuinely still
    true. Striking there would mark every real fix as failed within hours, re-file
    it, and hit MAX_STRIKES on two honest attempts — turning the Chairman queue
    into a stream of "the board keeps failing" alarms generated entirely by this
    function's own impatience. The follow-up date is the deadline; a deadline that
    is enforced early is not a deadline.

    The order below is also load-bearing: liveness is checked BEFORE silence.
    `last_seen` only advances when the observer runs, so a dead observer looks
    exactly like a quiet one (memory `presence-is-not-currency`). Ask "was anyone
    listening" first, or every switched-off detector reports a fix.
    """
    led = ledger if ledger is not None else load_ledger()
    stamp = now or datetime.now()
    out = {"fixed": [], "struck": [], "cannot_tell": [], "escalated": []}
    for sig, rec in led["issues"].items():
        if rec.get("state") != ONGOING:
            continue
        ruled_at = rec.get("ruled_at") or rec.get("relayed_at") or rec.get("first_seen")
        # 1. Has the deadline arrived? Before it, an ONGOING issue is simply
        #    ongoing, and the digest keeps reporting it as such.
        due = rec.get("follow_up_by")
        if not due or stamp < datetime.fromisoformat(str(due)[:26]):
            continue
        # 2. Was anyone listening? Must precede the silence test.
        listened = _observer_ran_since(led, rec.get("source", ""), ruled_at)
        if not listened:
            rec["verdict"] = "CANNOT_TELL"
            rec["verdict_why"] = (
                f"the observer that raised this ({rec.get('source') or 'unknown'}) has not "
                f"run since the board ruled, so its silence is not evidence of a fix")
            out["cannot_tell"].append(sig)
            overdue = _days_since(due, stamp) or 0
            if overdue > FOLLOW_UP_GRACE_DAYS and not rec.get("escalated_at"):
                _escalate(rec, stamp,
                          f"this has sat unverifiable for {overdue:.0f}d past its follow-up "
                          f"date because {rec.get('source') or 'its observer'} is not running. "
                          f"Fix the observer or reassign the issue — it cannot be closed on "
                          f"silence from a detector that is switched off.")
                out["escalated"].append(sig)
            continue
        # 3. Listening. Is it still firing at its own deadline? Then the fix did
        #    not hold. QUIET_WINDOW_DAYS of real silence, not merely "not seen
        #    this second", is what separates a fix from a lull between runs.
        quiet_days = _days_since(rec.get("last_seen"), stamp)
        if quiet_days is None or quiet_days < QUIET_WINDOW_DAYS:
            rec["strikes"] = int(rec.get("strikes", 0)) + 1
            rec["state"] = DETECTED          # back to the start; relay() re-files it
            rec["follow_up_by"] = None
            rec["verdict"] = "NOT_FIXED"
            rec["verdict_why"] = (
                f"still being raised at its follow-up date (last seen "
                f"{str(rec.get('last_seen', ''))[:10]}) — strike {rec['strikes']}")
            rec.setdefault("history", []).append(
                {"at": stamp.isoformat(), "to": DETECTED,
                 "why": f"still firing at the follow-up date (strike {rec['strikes']})"})
            out["struck"].append(sig)
            if rec["strikes"] >= MAX_STRIKES:
                _escalate(rec, stamp,
                          f"a board-ratified fix has now failed {rec['strikes']}x; the "
                          f"next attempt should be structural, not another retry")
                out["escalated"].append(sig)
            continue
        # 4. Listening, and quiet. That is a fix.
        rec["state"] = FIXED
        rec["fixed_at"] = stamp.isoformat()
        rec["verdict"] = "FIXED"
        rec.pop("verdict_why", None)
        rec.setdefault("history", []).append(
            {"at": stamp.isoformat(), "to": FIXED,
             "why": f"{rec.get('source')} ran and raised nothing for "
                    f"{quiet_days:.1f}d before the follow-up date"})
        out["fixed"].append(sig)
    if save:
        _save(LEDGER_PATH, led)
    return out


def _escalate(rec: dict, stamp: datetime, why: str) -> None:
    rec["escalated_at"] = stamp.isoformat()
    _append_deduped(CHAIRMAN_PATH, {
        "id": f"issue-escalated-{rec['sig']}",
        "dedup_key": f"issue_escalated_{rec['sig']}",
        "source": "Board Issue Ledger",
        "severity": "HIGH" if int(rec.get("strikes", 0)) >= MAX_STRIKES else "MEDIUM",
        "description": f"\"{rec.get('latest_text', '')}\" — {why}",
        "status": "awaiting_chairman",
        "ts": stamp.isoformat(),
    })


# ─────────────────────────────────────────────────────────────────────────────
# The digest section
# ─────────────────────────────────────────────────────────────────────────────

def digest_section(ledger: Optional[dict] = None, save: bool = True) -> dict:
    """Build the 'Fixed issues' block, and retire what has now been reported.

    Returns {'lines', 'html', 'counts'}. Reporting a FIXED issue is the act that
    retires it — Jack asked that a closed issue not be mentioned again, and the
    only moment we can be sure he has seen it is the moment we render it.
    """
    led = ledger if ledger is not None else load_ledger()
    lines, retired = [], []
    counts = {DETECTED: 0, RELAYED: 0, ONGOING: 0, FIXED: 0, RETIRED: 0}
    for sig, rec in sorted(led["issues"].items(),
                           key=lambda kv: kv[1].get("first_seen", "")):
        state = rec.get("state", DETECTED)
        counts[state] = counts.get(state, 0) + 1
        text = rec.get("latest_text", "")
        if state == ONGOING:
            lines.append(f"{text} — ongoing fix by board "
                         f"({rec.get('explanation', 'no reason recorded')}) "
                         f"({str(rec.get('follow_up_by', ''))[:10]})")
        elif state == FIXED:
            lines.append(f"{text} — fixed by board "
                         f"({rec.get('explanation', 'no reason recorded')}) "
                         f"({str(rec.get('fixed_at', ''))[:10]})")
            rec["shown_fixed"] = int(rec.get("shown_fixed", 0)) + 1
            if rec["shown_fixed"] >= RETIRE_AFTER_SHOWN:
                rec["state"] = RETIRED
                rec.setdefault("history", []).append(
                    {"at": _now(), "to": RETIRED, "why": "reported fixed to the Chairman"})
                retired.append(sig)
        elif state == RELAYED and rec.get("verdict") != "CANNOT_TELL":
            lines.append(f"{text} — relayed to the board, awaiting a ruling "
                         f"(since {str(rec.get('relayed_at', ''))[:10]})")
        if rec.get("verdict") == "CANNOT_TELL":
            lines.append(f"{text} — CANNOT VERIFY: {rec.get('verdict_why', '')}")
    # A denominator, always. A section that lists three fixes without saying
    # three of how many is the kind of reporting `internal_audit_agent` exists
    # to refuse: it reads as progress whether or not any was made.
    live = counts[DETECTED] + counts[RELAYED] + counts[ONGOING]
    total = live + counts[FIXED]
    header = (f"{counts[FIXED]} fixed / {counts[ONGOING]} in progress / "
              f"{counts[RELAYED]} awaiting a board ruling / {counts[DETECTED]} "
              f"newly detected — {total} tracked issue(s), "
              f"{counts[RETIRED]} closed and retired to date")
    # A host that has been standing down for weeks looks exactly like a host with
    # nothing to relay. Say which one it is, in the report, unprompted.
    sd = led.get("relay_standdown")
    if sd and counts[DETECTED]:
        lines.append(f"⚠ {counts[DETECTED]} issue(s) are NOT reaching the board: "
                     f"{sd.get('why', 'reason not recorded')} (since {str(sd.get('at',''))[:10]})")
    # Pacing is not the same as dropping, but it looks identical from the outside
    # unless the queue depth is stated. A backlog that stops draining is a broken
    # relay wearing the cap as a disguise.
    backlog = int(led.get("relay_backlog") or 0)
    if backlog and not sd:
        lines.append(f"↻ {backlog} further issue(s) queued for the board — "
                     f"{MAX_RELAYS_PER_RUN} are filed per pipeline run to keep the "
                     f"board's queue actionable")
    if save and retired:
        _save(LEDGER_PATH, led)
    # An empty body has two completely different meanings and they must not share
    # a sentence. The first live render said "12 newly detected — 12 tracked
    # issue(s)" directly above "No open issues tracked", which is a flat
    # contradiction inside one section of Jack's email — and the kind that
    # teaches a reader to stop trusting the section rather than to ask about it.
    # A DETECTED issue correctly produces no line here: this section reports what
    # is being DONE, and nothing is being done until the board has it. So the
    # empty case has to say WHICH empty it is.
    if lines:
        rows = "".join(f"<li style='margin:4px 0;'>{l}</li>" for l in lines)
        body = f"<ul style='margin:0;padding-left:18px;font-size:13px;'>{rows}</ul>"
    elif counts[DETECTED]:
        body = (f"<p style='color:#8b949e;font-size:12px;'>{counts[DETECTED]} issue(s) "
                f"detected and awaiting a second sighting before they are filed with the "
                f"board — nothing is in the board's hands yet. The warnings themselves are "
                f"listed above.</p>")
    else:
        body = "<p style='color:#8b949e;font-size:12px;'>No open issues tracked.</p>"
    html = (f"<h3 style='margin:18px 0 4px;'>Issues &amp; fixes</h3>"
            f"<p style='color:#8b949e;font-size:12px;margin:0 0 6px;'>{header}</p>" + body)
    return {"lines": lines, "html": html, "counts": counts, "header": header}


# ─────────────────────────────────────────────────────────────────────────────

def run(save: bool = True, now: Optional[datetime] = None) -> dict:
    """One full turn of the loop. Safe to call every pipeline cycle."""
    led = load_ledger()
    relayed = relay(led, save=False)
    ruled = ingest_rulings(led, save=False, now=now)
    verdicts = verify(led, save=False, now=now)
    if save:
        _save(LEDGER_PATH, led)
    return {"relayed": relayed, "ruled": ruled, **verdicts,
            "tracked": len(led["issues"])}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    if "--report" in argv:
        sec = digest_section(save=False)
        print(sec["header"])
        for l in sec["lines"]:
            print(f"  • {l}")
        return 0
    out = run()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
