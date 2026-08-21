#!/usr/bin/env python3
"""
APEX CAPITAL — CIT PIPELINE INTEGRITY AGENT
============================================
Runs every 2 hours (cron: 50 */2 * * *) alongside the R&D cycle.

PURPOSE
-------
The CIT director identifies code improvement opportunities but does not
monitor active pipeline WIRING bugs in real time. The deputy_monitor detects
stale files but cannot distinguish "file stale because VM is down" from "file
stale because of a code bug." This agent closes that gap.

MOTIVATION
----------
A 17-day bug: manager_decisions.json was never written by main.py because the
code called evaluate_recommendation() directly instead of manager_agent.run().
The dead-man's switch fired "Manager Agent missed a cycle" 4× per day. The
VM was up. The pipeline was running. A cross-file wiring check would have
caught this on day 1.

CHECKS
------
  1. Dead-man's switch sanity — heartbeat fresh but manager_decisions stale
     → distinguishes code wiring bug from genuine VM outage
  2. Email loop detection — repeated subjects in email_buffer.json
  3. Agent file freshness — lighter version of deputy staleness check
  4. ML model health — AUC regression detection
  5. Peer host liveness — is the OTHER host still alive? (see below)
  6. Single writer — how MANY hosts are trading the shared account? (see below)

EVERY CHECK DECLARES A VERDICT
------------------------------
PASS / FAIL / SKIP, never just "did it append to `issues`". Only a check that
actually RAN and PASSED may close its own Chairman-queue entry. A check that
could not be evaluated is SKIP and closes nothing — absence of a finding means
"resolved" only if the thing that would have produced it really ran, otherwise
the firm marks every open item fixed at the exact moment it goes blind to them.
sweep_governance._closable_kinds learned this first; this agent now shares it.

WHY 5 EXISTS
------------
Every deadman in this firm was a local-file freshness check running on the host
it was watching, so when a host died its watcher died with it. On 2026-08-06 the
GCP VM — the always-on host, ~63 cron jobs, main.py 6×/day, same Alpaca account
— had been unreachable for days and NOTHING raised an alarm. `chairman_inbox`
degraded to a printed "could not reach the VM"; `wakeup_recovery.py` reads
/proc/uptime and so only ever runs *on* the VM, after it comes back. The firm
found out via a downstream symptom: manager_decisions.json went 42h stale
across a full trading day. A host cannot be its own deadman.

WHY 6 EXISTS
------------
Check 5 asks "is the peer alive?" and can only answer from the Mac. On
2026-08-06 it said the VM was unreachable, and the firm read that as the VM
being down. It was not down — it was trading. Order stamps on the shared
Alpaca account showed apexcapitalc placing orders on the same days the Mac
did, including a full day the Mac slept through.

So the dangerous question was never "is the other host alive" but "how many of
us are there", and no host can answer that about itself. Check 6 asks it
through the one medium every host shares — the broker's own order history —
so it needs no ssh, no shared disk, and no cooperation from the other host.
`foreign_order_detector` could not see this: it asks "is this order ours?",
and every one of these was ours.

OUTPUT
------
  data/cit_pipeline_integrity.json   — full findings + per-check verdicts
  data/chairman_action_required.json — CRITICAL/HIGH raised, cleared ones closed
  data/peer_host_liveness.json       — last time the peer host answered

LOCKING
-------
  data/.pipeline_integrity.lock      — fcntl.flock, prevents concurrent runs
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

import chairman_queue
import deadmans_switch as dms
import schedule_expectations as sched

# ── Working directory ─────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_SCRIPT_DIR)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cit_pipeline_integrity")

# ── File paths ────────────────────────────────────────────────────────────────
_HEARTBEAT_PATH    = "data/pipeline_heartbeat.json"
_MANAGER_PATH      = "data/manager_decisions.json"
_EMAIL_BUFFER_PATH = "data/email_buffer.json"
_RESEARCH_PATH     = "data/research_team_report.json"
_RISK_PATH         = "data/risk_bulletin.json"
_CANARY_PATH       = "data/canary_state.json"
_MODEL_META_PATH   = "data/model_v2_meta.json"
_OUTPUT_PATH       = "data/cit_pipeline_integrity.json"
_CHAIRMAN_PATH     = "data/chairman_action_required.json"
_LIVENESS_PATH     = "data/peer_host_liveness.json"
_LOCK_PATH         = "data/.pipeline_integrity.lock"

# ── Thresholds ────────────────────────────────────────────────────────────────
# The dead-man's switch numbers now come from deadmans_switch.py, which is the
# firm's single definition of this alarm. Three modules used to carry their own
# and disagreed about the same file on the same morning — see that module's
# docstring. Aliased rather than inlined so the names below still read.
_HEARTBEAT_FRESH_H      = dms.HEARTBEAT_FRESH_H
_HEARTBEAT_STALE_H      = dms.HEARTBEAT_STALE_H
_MANAGER_STALE_H        = dms.WIRING_BUG_STALE_H
# (_AGENT_STALE_H = 26 lived here until 2026-08-21. Every one of the three
#  files it watched is produced on a `* * * * 1-5` crontab line, so their own
#  schedule guarantees a Friday-to-Monday gap of up to 72.0h — this alarm was
#  arithmetically certain to fire all weekend and every Monday morning on a
#  firm that was working perfectly, and a producer that had actually died over
#  the weekend looked exactly the same. Hours were a proxy; the question is
#  "did a scheduled fire pass with no output after it?", which the host already
#  writes down in its own crontab. See schedule_expectations.py.)
_EMAIL_LOOP_THRESHOLD   = 5    # same subject N+ times = loop
                               # (raised from 3: R&D runs 4×/day; IRM REVIEW copies now excluded
                               # so threshold of 5 catches genuine loops while not flagging R&D)
_MODEL_AUC_FLOOR        = 0.58 # below this = serious regression
_CHAIRMAN_MAX_ENTRIES   = 20   # keep only the most recent N entries

# ── Peer host ─────────────────────────────────────────────────────────────────
_PEER_HOST          = "apex-capital"   # ssh alias of the GCP VM
_PEER_BASE          = "~/institutional-tracker"
# Fallback only. The live ladder counts MISSED SCHEDULED RUNS (see
# check_peer_host_liveness); these two are used solely when the VM's schedule
# cannot be read, and the finding says so when they are.
_PEER_UNREACHABLE_H = 6.0   # no contact for this long on the always-on host = HIGH
_PEER_DOWN_H        = 24.0  # ...and this long = CRITICAL, the firm is running on one leg
_PEER_SSH_TIMEOUT_S = 20

# ── Verdicts ──────────────────────────────────────────────────────────────────
# Only PASS may close a Chairman-queue entry. SKIP is the honest verdict for
# "this host could not evaluate that", and it closes nothing: a check that did
# not run has disproved nothing, and treating its silence as an all-clear would
# retire every open finding the moment the check broke.
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

#: every check this agent owns, and therefore every entry it may close.
#: A name here that no check declares would never be closable and would sit in
#: the Chairman queue forever; a check whose name is missing here can raise an
#: item it can never retract. Guarded by TestPipelineIntegrityVerdicts.
CHECK_NAMES = (
    "deadmans_switch_sanity",
    "email_loop_detection",
    "agent_file_freshness",
    "ml_model_health",
    "peer_host_liveness",
    "single_writer",
)


# =============================================================================
# HELPERS
# =============================================================================

def _load_json(path: str):
    """Load JSON from path; return None on any error."""
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"  Could not load {path}: {e}")
    return None


def _file_age_hours(path: str) -> float:
    """Return file mtime age in hours; inf if missing."""
    try:
        mtime = os.path.getmtime(path)
        return (datetime.now().timestamp() - mtime) / 3600
    except OSError:
        return float("inf")


def _ts_age_hours(ts: str) -> float:
    """Return age of an ISO timestamp in hours; inf if blank/invalid."""
    if not ts:
        return float("inf")
    try:
        return (datetime.now() - datetime.fromisoformat(ts[:19])).total_seconds() / 3600
    except Exception:
        return float("inf")


def _parse_epoch(ts: str):
    """ISO timestamp -> epoch seconds, or None if blank/unparseable.

    None means "no readable production time", which is a different fact from
    "produced long ago" and must not be flattened into a large age.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)[:19]).timestamp()
    except Exception:
        return None


def _get_timestamp_from_json(data, *keys) -> str:
    """Try multiple key names to find a timestamp in a dict."""
    if not isinstance(data, dict):
        return ""
    for k in keys:
        v = data.get(k, "")
        if v:
            return str(v)
    return ""


def _is_weekday() -> bool:
    return datetime.now().weekday() < 5  # Mon=0 … Fri=4


def _now_iso() -> str:
    return datetime.now().isoformat()


def _run_id() -> str:
    return f"pip-integrity-{datetime.now().strftime('%Y%m%d-%H')}"


def _skip(skipped: dict, check: str, why: str) -> None:
    """Declare that `check` could not be evaluated on this run, and why.

    Distinct from passing. A check that returns without appending an issue looks
    identical to one that ran and found nothing, and that ambiguity is what makes
    a silent all-clear possible — so the not-evaluated case has to say so out
    loud rather than be inferred from an empty list.
    """
    skipped[check] = why
    logger.info(f"    SKIPPED — {why}")


def _verdicts(issues: list, skipped: dict) -> dict:
    """check name -> PASS / FAIL / SKIP for every check this agent owns.

    Derived, not self-reported, so a check cannot claim PASS while having just
    raised a CRITICAL. The only thing a check declares for itself is SKIP, which
    is the one state that is not observable from the issue list.
    """
    failed = {i.get("check") for i in issues}
    out = {}
    for name in CHECK_NAMES:
        if name in skipped:
            out[name] = SKIP
        elif name in failed:
            out[name] = FAIL
        else:
            out[name] = PASS
    return out


# =============================================================================
# CHECK 1 — DEAD-MAN'S SWITCH SANITY
# =============================================================================

def check_deadmans_switch(issues: list, skipped: dict):
    logger.info("\n  [1/6] Dead-man's switch sanity check...")
    me = "deadmans_switch_sanity"

    # --- pipeline_heartbeat.json ---
    hb_data = _load_json(_HEARTBEAT_PATH)
    if hb_data is None:
        hb_age_h = float("inf")
        logger.info("    pipeline_heartbeat.json missing — treating as infinitely stale")
    else:
        hb_ts = _get_timestamp_from_json(hb_data, "last_started", "generated_at", "timestamp")
        hb_age_h = _ts_age_hours(hb_ts) if hb_ts else _file_age_hours(_HEARTBEAT_PATH)

    # --- manager_decisions.json ---
    # Age comes from the newest DECISION, via the shared policy — never from the
    # file mtime. This used to be `min(file_mtime_age, last_entry_ts_age)`, the
    # YOUNGER of the two, so any process that rewrote the file reset the
    # apparent age without a single new decision behind it. On 2026-08-06 that
    # is exactly what happened: morning_briefing reported the Manager 39h stale
    # (correctly — a 42h gap across the whole of Wednesday Aug 5, a trading day)
    # while this check, the one wired to the Chairman queue, printed
    # "OK — heartbeat and manager_decisions are consistent".
    mgr_data = _load_json(_MANAGER_PATH)
    if mgr_data is None:
        mgr_age_h = float("inf")
        mgr_eff_h = float("inf")
    else:
        mgr_age_h = dms.decision_age_hours(mgr_data)
        mgr_eff_h = dms.effective_age_hours(mgr_data)

    logger.info(f"    pipeline_heartbeat age: {hb_age_h:.1f}h")
    logger.info(f"    manager_decisions age:  {mgr_age_h:.1f}h "
                f"({mgr_eff_h:.1f}h excl. weekends)")

    # This host cannot see the pipeline's files at all. NOT the same statement
    # as "the pipeline is fine", and not — as the comment here used to claim —
    # proof that we are on GCP: "main.py runs on Mac" and "pipeline_heartbeat
    # and manager_decisions only exist on the Mac host" both stopped being true
    # when the firm migrated to the VM, which now runs main.py 6x a day. The
    # honest verdict is SKIP: nothing was evaluated, so nothing is disproved and
    # no open finding may be closed off the back of it.
    if hb_age_h == float("inf") and mgr_age_h == float("inf"):
        return _skip(skipped, me,
                     "neither pipeline_heartbeat.json nor manager_decisions.json "
                     "exists on this host — the pipeline does not run here")

    if not os.path.exists(_MANAGER_PATH):
        return _skip(skipped, me,
                     "manager_decisions.json does not exist on this host, so the "
                     "Manager's cadence cannot be assessed from here")

    # Pattern 1: heartbeat fresh, manager stale → wiring bug
    if hb_age_h < _HEARTBEAT_FRESH_H and mgr_age_h > _MANAGER_STALE_H:
        msg = (
            f"Pipeline running (heartbeat {hb_age_h:.1f}h old) but the newest "
            f"decision in manager_decisions.json is {mgr_age_h:.1f}h old "
            f"(threshold {_MANAGER_STALE_H}h) — likely code wiring bug, not VM issue. "
            f"Check that main.py writes manager_decisions.json after every manager block."
        )
        logger.warning(f"    CRITICAL: {msg}")
        issues.append({
            "severity":   "CRITICAL",
            "check":      me,
            "message":    msg,
            "auto_fixed": False,
        })
        return

    # Pattern 2: heartbeat stale on a weekday → genuine pipeline issue
    if hb_age_h > _HEARTBEAT_STALE_H and _is_weekday():
        msg = (
            f"pipeline_heartbeat.json is {hb_age_h:.1f}h old on a weekday "
            f"(threshold {_HEARTBEAT_STALE_H}h) — possible genuine pipeline outage or VM issue."
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      me,
            "message":    msg,
            "auto_fixed": False,
        })
        return

    # Pattern 3: the pipeline started recently enough not to trip Pattern 2, but
    # the Manager has still not decided in a working day. Nothing used to catch
    # this: Pattern 1 needs a heartbeat under 4h and Pattern 2 only ever looks at
    # the heartbeat, so a Manager that quietly stopped while the pipeline kept
    # starting fell straight through the gap between them and this check printed
    # "OK". Weekend-adjusted, because the pipeline is weekday-only.
    if mgr_eff_h > dms.STALE_H:
        msg = (
            f"No Manager decision for {mgr_age_h:.1f}h ({mgr_eff_h:.1f}h excl. "
            f"weekends, threshold {dms.STALE_H}h) while the pipeline heartbeat is "
            f"{hb_age_h:.1f}h old — the Manager has stopped deciding on trading days."
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      me,
            "message":    msg,
            "auto_fixed": False,
        })
        return

    logger.info("    OK — heartbeat and manager_decisions are consistent.")


# =============================================================================
# CHECK 2 — EMAIL LOOP DETECTION
# =============================================================================

def check_email_loop(issues: list, skipped: dict):
    logger.info("\n  [2/6] Email loop detection...")
    me = "email_loop_detection"

    buffer = _load_json(_EMAIL_BUFFER_PATH)
    # An empty buffer and a missing buffer are different facts. An empty list is
    # evaluable — there are no emails, so there is no loop, and that is a real
    # PASS. A missing or unreadable file means the check saw nothing at all and
    # must not be allowed to close an open loop finding on that basis.
    if buffer is None or not isinstance(buffer, list):
        return _skip(skipped, me,
                     "email_buffer.json is missing or is not a list — nothing to read")
    if not buffer:
        logger.info("    email_buffer.json is empty — no loop possible.")
        return

    logger.info(f"    {len(buffer)} email(s) in buffer.")

    # Normalise subjects: strip leading emoji/whitespace, lower for comparison
    def _norm(s: str) -> str:
        # Drop leading non-alpha characters (emoji, spaces) then lowercase
        stripped = s.lstrip()
        # Remove common emoji prefixes by stripping non-ASCII at the start
        while stripped and ord(stripped[0]) > 127:
            stripped = stripped[1:]
        return stripped.strip().lower()

    subject_counts: dict[str, list[str]] = {}
    for entry in buffer:
        raw_subject = entry.get("subject", "") if isinstance(entry, dict) else ""
        if not raw_subject:
            continue
        # Skip IRM REVIEW shadow copies — every buffered email gets one automatically.
        # Counting them would double every subject count and fire false loop alerts.
        if raw_subject.strip().startswith("[IRM REVIEW]"):
            continue
        key = _norm(raw_subject)
        subject_counts.setdefault(key, []).append(raw_subject)

    loop_subjects = {k: v for k, v in subject_counts.items() if len(v) >= _EMAIL_LOOP_THRESHOLD}

    if loop_subjects:
        examples = "; ".join(
            f'"{list(v)[0]}" ×{len(v)}' for k, v in list(loop_subjects.items())[:3]
        )
        msg = (
            f"Repeated email subjects in buffer ({len(loop_subjects)} unique subject(s) "
            f"appear {_EMAIL_LOOP_THRESHOLD}+ times): {examples}"
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      "email_loop_detection",
            "message":    msg,
            "auto_fixed": False,
        })

    # False-alarm pattern: DEAD-MAN'S SWITCH email but pipeline is actually fresh
    hb_data = _load_json(_HEARTBEAT_PATH)
    if hb_data is not None:
        hb_ts = _get_timestamp_from_json(hb_data, "last_started", "generated_at", "timestamp")
        hb_age_h = _ts_age_hours(hb_ts) if hb_ts else _file_age_hours(_HEARTBEAT_PATH)
        if hb_age_h < _HEARTBEAT_FRESH_H:
            for entry in buffer:
                subj = entry.get("subject", "") if isinstance(entry, dict) else ""
                if "DEAD" in subj.upper() and "MAN" in subj.upper():
                    msg = (
                        f"Dead-man's switch alert in email buffer but pipeline heartbeat is "
                        f"only {hb_age_h:.1f}h old — this is the false-alarm pattern (wiring "
                        f"bug causes missed-cycle emails even though the VM is running fine)."
                    )
                    logger.warning(f"    HIGH: {msg}")
                    issues.append({
                        "severity":   "HIGH",
                        "check":      "email_loop_detection",
                        "message":    msg,
                        "auto_fixed": False,
                    })
                    break

    if not loop_subjects:
        logger.info("    OK — no repeated subjects detected.")


# =============================================================================
# CHECK 3 — AGENT FILE FRESHNESS
# =============================================================================

def check_agent_freshness(issues: list, skipped: dict):
    logger.info("\n  [3/6] Agent file freshness check...")

    # (path, label, the script whose crontab line is responsible for it).
    # research_team.py is on no crontab of its own — main.py invokes it — so
    # main.py's schedule is the one that decides whether its report is late.
    watch_files = [
        (_RESEARCH_PATH, "research_team_report.json", "main.py"),
        (_RISK_PATH,     "risk_bulletin.json",        "main.py"),
        (_CANARY_PATH,   "canary_state.json",         "canary_agent.py"),
    ]

    late, unverifiable, not_ours = [], [], []
    for path, label, producer in watch_files:
        data = _load_json(path)
        ts = _get_timestamp_from_json(
            data, "generated_at", "timestamp", "last_level_time", "ts"
        ) if data is not None else ""

        # Prefer the file's own "when was this produced" stamp over the mtime:
        # a deploy or a chmod moves the mtime without producing anything.
        produced = _parse_epoch(ts)
        if produced is None and data is not None:
            try:
                produced = os.path.getmtime(path)
            except OSError:
                produced = None

        v = sched.lateness(producer, produced)
        age_txt = ("missing" if produced is None
                   else "%.1fh old" % ((datetime.now().timestamp() - produced) / 3600))
        logger.info("    %s: %s — %s (%s)", label, age_txt, v["verdict"],
                    sched.describe(producer))

        if v["verdict"] == sched.LATE:
            late.append((label, producer, v))
        elif v["verdict"] == sched.SCHEDULE_UNREADABLE:
            unverifiable.append(label)
        elif v["verdict"] == sched.NOT_SCHEDULED_HERE:
            not_ours.append((label, producer))

    if late:
        details = "; ".join(
            "%s (producer %s missed its %s run)" % (
                lbl, prod, v["missed_fire"][11:16]) for lbl, prod, v in late)
        msg = (
            f"{len(late)} agent output file(s) are missing a scheduled run "
            f"and no recovery appears in progress: {details}"
        )
        logger.warning(f"    MEDIUM: {msg}")
        issues.append({
            "severity":   "MEDIUM",
            "check":      "agent_file_freshness",
            "message":    msg,
            "auto_fixed": False,
        })

    # "Could not read the scheduler" is not "everything is fine". Without the
    # crontab this check has no denominator at all, and silently returning OK
    # would be the fallback-that-means-act shape: a degraded read substituting
    # the reassuring answer.
    if unverifiable:
        msg = (
            "Could not read this host's crontab, so freshness of %s is "
            "unverifiable — this check reported neither pass nor fail."
            % ", ".join(unverifiable)
        )
        logger.warning(f"    MEDIUM: {msg}")
        issues.append({
            "severity":   "MEDIUM",
            "check":      "agent_file_freshness",
            "message":    msg,
            "auto_fixed": False,
        })

    # There is no state sync between the Mac and the VM. Judging a file this
    # host never produces against this host's data/ compares two hosts' facts,
    # which is how the roster ratchet accused live agents of vanishing.
    if not_ours:
        logger.info("    %d file(s) belong to the other host (%s) — not judged "
                    "here.", len(not_ours),
                    ", ".join("%s←%s" % (l, p) for l, p in not_ours))

    if not (late or unverifiable):
        logger.info("    OK — every watched file this host owns is on schedule.")


# =============================================================================
# CHECK 4 — ML MODEL HEALTH
# =============================================================================

def check_ml_model_health(issues: list, skipped: dict):
    logger.info("\n  [4/6] ML model health check...")
    me = "ml_model_health"

    meta = _load_json(_MODEL_META_PATH)
    if meta is None:
        return _skip(skipped, me,
                     "model_v2_meta.json not found — no model to assess")

    auc = meta.get("auc") or meta.get("val_auc") or meta.get("best_auc")
    if auc is None:
        return _skip(skipped, me,
                     "model_v2_meta.json carries no AUC field — nothing to compare")

    try:
        auc = float(auc)
    except (ValueError, TypeError):
        return _skip(skipped, me,
                     f"AUC value is not numeric ({auc!r}) — cannot compare to the floor")

    logger.info(f"    Model AUC: {auc:.4f} (floor: {_MODEL_AUC_FLOOR})")

    if auc < _MODEL_AUC_FLOOR:
        trained_at = meta.get("trained_at", meta.get("timestamp", "unknown"))
        msg = (
            f"ML model AUC is {auc:.4f}, below serious-regression floor "
            f"({_MODEL_AUC_FLOOR}). Model trained at: {trained_at}. "
            f"Review training data quality and feature pipeline before deploying signals."
        )
        logger.warning(f"    HIGH: {msg}")
        issues.append({
            "severity":   "HIGH",
            "check":      "ml_model_health",
            "message":    msg,
            "auto_fixed": False,
        })
    else:
        logger.info("    OK — model AUC above regression floor.")


# =============================================================================
# CHECK 5 — PEER HOST LIVENESS
# =============================================================================

def _probe_peer() -> tuple:
    """(reachable, remote_heartbeat_age_h_or_None, detail).

    One ssh, short timeout, no writes. Returns rather than raises: an
    unreachable host is the thing being measured, not an error.
    """
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           _PEER_HOST, f"cat {_PEER_BASE}/data/pipeline_heartbeat.json 2>/dev/null || echo NOFILE"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_PEER_SSH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return (False, None, f"ssh to {_PEER_HOST} timed out after {_PEER_SSH_TIMEOUT_S}s")
    except FileNotFoundError:
        return (None, None, "no ssh client on this host")
    if r.returncode != 0:
        return (False, None, f"ssh to {_PEER_HOST} failed: {r.stderr.strip()[:160]}")

    out = (r.stdout or "").strip()
    if out == "NOFILE" or not out:
        return (True, None, "host answered but has no pipeline_heartbeat.json")
    try:
        hb = json.loads(out)
    except Exception:
        return (True, None, "host answered but its pipeline_heartbeat.json is not valid JSON")
    ts = _get_timestamp_from_json(hb, "last_started", "generated_at", "timestamp")
    return (True, _ts_age_hours(ts) if ts else None, "host answered")


def check_single_writer(issues: list, skipped: dict):
    """How many hosts are trading this account?

    Every other check in this file asks a host about itself, which is exactly
    why the fork went unseen: `peer_host_liveness` above can only SKIP on the
    VM, and on the Mac it reported the VM unreachable — from which the firm
    concluded the VM was down. It was not down. It was trading.

    This check asks the one question the others cannot, through the one medium
    both hosts always share: the broker. Orders carry `apex-<host>-...` stamps,
    so counting distinct hosts in the account's own order history needs no ssh
    and no cooperation from the other host. On 2026-08-06 that count was 2 on
    four consecutive trading days, and 3 across the month.

    Unlike peer liveness this runs everywhere, because the account is visible
    from everywhere — and an extra writer is precisely the thing a host cannot
    be trusted to report about itself.
    """
    logger.info("\n  [6/6] Single-writer check...")
    me = "single_writer"

    try:
        import single_writer as sw
    except Exception as e:
        return _skip(skipped, me, f"single_writer module unavailable: {e}")

    # Load .env if a caller has not already. Without this the check SKIPs on
    # every standalone run — honest, but permanently uninformative, and a check
    # that can only ever say "cannot tell" is not a check.
    if not os.environ.get("ALPACA_API_KEY"):
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass

    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        return _skip(skipped, me,
                     "no Alpaca credentials on this host — the order history is "
                     "the only cross-host evidence and it cannot be read here")

    try:
        import urllib.request
        base = os.environ.get("ALPACA_BASE_URL",
                              "https://paper-api.alpaca.markets").rstrip("/")
        req = urllib.request.Request(
            base + "/v2/orders?status=all&limit=500&direction=desc",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        with urllib.request.urlopen(req, timeout=30) as r:
            orders = json.load(r)
    except Exception as e:
        # Cannot reach the broker is cannot-tell, never all-clear.
        return _skip(skipped, me, f"could not read order history: {type(e).__name__}: {str(e)[:120]}")

    def _emit(sev, msg, suffix=""):
        if sev == "SKIP":
            _skip(skipped, me, msg)
            return
        if sev == "PASS":
            logger.info(f"    OK — {msg}")
            return
        full = (f"{msg} Guard mode is '{sw.mode()}'"
                + (f", designated writer '{sw.designated_host()}'." if sw.designated_host()
                   else " — APEX_TRADING_HOST is unset, so nothing is being blocked.")
                + suffix)
        logger.warning(f"    {sev}: {full}")
        issues.append({"severity": sev, "check": me,
                       "message": full, "auto_fixed": False})

    # Two independent questions. "How many hosts are writing?" is a COUNT in a
    # window; "is the designated writer still writing?" is a TIMESTAMP. Running
    # only the first is how the VM's death hid for eight days: it had traded
    # inside the 48h window before it stopped, so the tally listed it as present
    # and the multi-host branch returned CRITICAL about the fork long before
    # anything could ask whether one of those hosts had gone quiet.
    tally = sw.hosts_trading_recently(orders, hours=48)
    logger.info(f"    48h host tally: {tally or '(none)'}")
    _emit(*sw.divergence_verdict(tally))

    if hasattr(sw, "writer_silence_verdict"):
        last_seen = sw.hosts_last_seen(orders)
        logger.info("    last seen: " + (", ".join(
            f"{h}={t.isoformat()[:16]}" for h, t in
            sorted(last_seen.items(), key=lambda kv: kv[1], reverse=True)) or "(none)"))
        _emit(*sw.writer_silence_verdict(last_seen))


def check_peer_host_liveness(issues: list, skipped: dict):
    """Is the OTHER host still alive?

    Every other deadman in this firm watches local files, so each host is its
    own watcher and a host that dies takes its watcher with it. On 2026-08-06
    the GCP VM — always-on, ~63 cron jobs, main.py 6x/day, trading the same
    Alpaca paper account — had been unreachable for days and not one alarm
    fired. The firm learned about it from a symptom two steps downstream.

    Runs from the Mac only, and that asymmetry is real rather than incidental:
    the Mac can ssh to the VM, the VM cannot ssh back (chairman_queue_sync says
    the same). On the VM this is SKIP, which closes nothing — it must not look
    like an all-clear for a question this host is in no position to answer.
    """
    logger.info("\n  [5/6] Peer host liveness...")
    me = "peer_host_liveness"
    now = _now_iso()

    if sys.platform != "darwin":
        return _skip(skipped, me,
                     f"this host is {sys.platform}, not the Mac — only the Mac can "
                     f"ssh to {_PEER_HOST}, so peer liveness is unanswerable here")

    state = _load_json(_LIVENESS_PATH) or {}
    if not isinstance(state, dict):
        state = {}

    reachable, remote_hb_h, detail = _probe_peer()
    logger.info(f"    {_PEER_HOST}: {detail}")

    # No ssh client at all is "cannot tell", not "the host is down". Reporting
    # a missing tool as an outage would send the Chairman to restart a VM that
    # was never off — cannot-compare is not differs.
    if reachable is None:
        return _skip(skipped, me, detail)

    if reachable:
        state.update({"host": _PEER_HOST, "last_ok": now, "last_checked": now,
                      "consecutive_failures": 0,
                      "remote_heartbeat_age_h": remote_hb_h})
        _write_liveness(state)

        # Reachable but its pipeline has stopped is a different failure from
        # unreachable, and a quieter one — the box answers, so every ssh-based
        # sync keeps working and nothing else notices.
        if remote_hb_h is not None and remote_hb_h > _HEARTBEAT_STALE_H and _is_weekday():
            msg = (f"{_PEER_HOST} is reachable but its pipeline_heartbeat.json is "
                   f"{remote_hb_h:.1f}h old on a weekday (threshold {_HEARTBEAT_STALE_H}h) — "
                   f"the always-on host is up but its pipeline has stopped.")
            logger.warning(f"    HIGH: {msg}")
            issues.append({"severity": "HIGH", "check": me,
                           "message": msg, "auto_fixed": False})
            return
        logger.info("    OK — peer host is up and its pipeline is running.")
        return

    # Unreachable. Age the outage from the last CONFIRMED contact, never from
    # this run: one failed ssh is a blip, and calling it an outage would page
    # the Chairman over a dropped packet.
    state["host"] = _PEER_HOST
    state["last_checked"] = now
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    _write_liveness(state)

    last_ok = state.get("last_ok")
    if not last_ok:
        # No successful contact has EVER been recorded. State the fact; do not
        # invent a duration for it. A fabricated "0.0h" here would read as a
        # blip and be ignored.
        msg = (f"{_PEER_HOST} is unreachable ({detail}) and this firm has no record "
               f"of it ever answering. The always-on VM runs main.py ~6x/day against "
               f"the same Alpaca account; while it is down the Mac is the only host "
               f"trading, and the Mac only runs while it is awake.")
        logger.warning(f"    HIGH: {msg}")
        issues.append({"severity": "HIGH", "check": me,
                       "message": msg, "auto_fixed": False})
        return

    down_h = _ts_age_hours(str(last_ok))
    logger.info(f"    last confirmed contact: {down_h:.1f}h ago "
                f"({state['consecutive_failures']} consecutive failures)")

    # Severity in the firm's own units, not in hours.
    #
    # This ladder used to be 6h = HIGH, 24h = CRITICAL. Both numbers were
    # picked, and an hour does not mean the same thing twice: ten hours
    # overnight costs the firm nothing, ten hours across a trading afternoon
    # costs it four pipeline runs, and a 60-hour weekend outage on a
    # weekday-only schedule costs it nothing at all while scoring CRITICAL
    # twice over. What actually matters is how many scheduled main.py runs
    # went missing while the box was unreachable — so count those.
    #
    # The VM's schedule is not on this host's crontab, so it is read from the
    # snapshot the VM mirrors daily through git. If that snapshot is missing or
    # too old to trust, peer_crontab() returns None and this falls back to the
    # old hour ladder, saying so in the message: a degraded read must announce
    # itself, not quietly substitute a number (memory `fallback-that-means-act`).
    peer_cron = sched.peer_crontab()
    missed = sched.missed_fires_since("main.py", _parse_epoch(str(last_ok)),
                                      cron_text=peer_cron) if peer_cron else None
    per_day = sched.fires_per_busiest_day("main.py", cron_text=peer_cron) \
        if peer_cron else None

    if missed is not None and per_day:
        basis = (f"{missed} scheduled main.py run(s) have been missed since then "
                 f"({per_day} run(s) is a full trading day)")
        if missed >= per_day:
            sev = "CRITICAL"
        elif missed >= 1:
            sev = "HIGH"
        else:
            logger.info("    Not yet an outage — no scheduled run missed since "
                        "last contact (%s).", basis)
            return
    else:
        basis = (f"the VM's schedule could not be read (no trustworthy "
                 f"vm_crontab_snapshot), so this falls back to elapsed hours "
                 f"— thresholds {_PEER_UNREACHABLE_H}h/{_PEER_DOWN_H}h")
        if down_h > _PEER_DOWN_H:
            sev = "CRITICAL"
        elif down_h > _PEER_UNREACHABLE_H:
            sev = "HIGH"
        else:
            logger.info("    Not yet an outage — under the "
                        f"{_PEER_UNREACHABLE_H}h fallback threshold, and {basis}.")
            return

    msg = (f"{_PEER_HOST} has been unreachable for {down_h:.1f}h "
           f"(last contact {str(last_ok)[:19]}, {state['consecutive_failures']} consecutive "
           f"failed probes; {detail}). {basis[0].upper()}{basis[1:]}. This is the ALWAYS-ON "
           f"host: ~63 cron jobs including main.py on the same Alpaca paper account. "
           f"While it is down the Mac is the firm's only host and it only runs while "
           f"awake. If the instance was restarted its external IP has changed — update "
           f"it with gcp_setup/update_ip.sh <NEW_IP>.")
    logger.warning(f"    {sev}: {msg}")
    issues.append({"severity": sev, "check": me, "message": msg, "auto_fixed": False})


def _write_liveness(state: dict) -> None:
    os.makedirs("data", exist_ok=True)
    tmp = _LIVENESS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, _LIVENESS_PATH)


# =============================================================================
# OUTPUT — write findings + update chairman queue
# =============================================================================

def _write_output(issues: list, skipped: dict):
    now_iso = _now_iso()
    healthy = len(issues) == 0
    verdicts = _verdicts(issues, skipped)

    report = {
        "checked_at": now_iso,
        "issues":     issues,
        "healthy":    healthy,
        # Per-check verdicts are part of the record, not a debug aid: a reader
        # of this file has to be able to tell "we looked and found nothing" from
        # "we never looked", and `issues: []` alone says both.
        "verdicts":   verdicts,
        "skipped":    skipped,
    }

    os.makedirs("data", exist_ok=True)
    with open(_OUTPUT_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"\n  Wrote findings to {_OUTPUT_PATH}")

    # ALWAYS reconcile — this used to run only `if has_critical_or_high`, which
    # is why a cleared finding had no way out of the Chairman queue. A run in
    # which everything passes is precisely the run that has something to close.
    _update_chairman_queue(issues, verdicts, now_iso)


def _update_chairman_queue(issues: list, verdicts: dict, now_iso: str):
    """Append CRITICAL/HIGH issues to chairman_action_required.json.

    Dedup by check name, not by the id (which embeds the run hour): this
    agent runs every 2h, so a chronic issue (e.g. email_loop_detection
    stuck HIGH for days) was re-queuing itself every single run instead of
    being recognized as already pending — the same silent-dedup bug class
    fixed 2026-07-23/24 in board_meeting.py / deputy_board.py / logic_officer.py
    / logic_team_implementation_agent.py, just on this 7th write site.

    Also: the old `combined[-20:]` truncation trimmed the WHOLE shared
    queue, so a burst of this agent's own findings could silently evict
    another producer's still-unresolved item. Cap only this agent's own
    entries; never touch entries from other sources.
    """
    own_source = "cit_pipeline_integrity_agent"

    run_id = _run_id()
    new_entries = []
    for iss in issues:
        if iss["severity"] not in ("CRITICAL", "HIGH"):
            continue
        key = iss["check"]
        entry = {
            "id":          f"{run_id}-{iss['check'][:20]}",
            "dedup_key":   key,
            "severity":    iss["severity"],
            "description": iss["message"],
            "source":      own_source,
            "ts":          now_iso,
            "status":      "awaiting_chairman",
        }
        new_entries.append(entry)
        logger.info(f"  Queued to chairman: [{iss['severity']}] {iss['check']}")

    # Close first, then raise. A check that PASSED this run has actively
    # disproved its own finding, so the entry goes out before anything new comes
    # in — otherwise a queue at its cap could evict a live finding to make room
    # for one that is about to be retired anyway.
    #
    # ONLY verdict == PASS closes. FAIL is still true and SKIP proved nothing;
    # closing on SKIP is the false all-clear this whole build exists to prevent
    # (sweep_governance._closable_kinds, same lesson).
    cleared = [name for name in CHECK_NAMES if verdicts.get(name) == PASS]
    if cleared:
        chairman_queue.resolve(cleared, own_source, path=_CHAIRMAN_PATH)

    chairman_queue.submit(new_entries, own_source,
                          path=_CHAIRMAN_PATH,
                          max_entries=_CHAIRMAN_MAX_ENTRIES)


# =============================================================================
# MAIN
# =============================================================================

def main():
    now = datetime.now()
    logger.info("=" * 60)
    logger.info("  APEX CAPITAL — CIT PIPELINE INTEGRITY AGENT")
    logger.info(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # ── Single-instance lock ──────────────────────────────────────────────────
    os.makedirs("data", exist_ok=True)
    lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another instance is already running — exiting.")
        lock_fh.close()
        sys.exit(0)

    try:
        issues: list = []
        skipped: dict = {}

        # Each check is isolated. An exception used to take down the whole run
        # before _write_output, so one broken check blinded the other three and
        # left no report at all. Now it becomes that check's SKIP — which closes
        # nothing — and the rest still run.
        for name, fn in (
            ("deadmans_switch_sanity", check_deadmans_switch),
            ("email_loop_detection",   check_email_loop),
            ("agent_file_freshness",   check_agent_freshness),
            ("ml_model_health",        check_ml_model_health),
            ("peer_host_liveness",     check_peer_host_liveness),
            ("single_writer",          check_single_writer),
        ):
            try:
                fn(issues, skipped)
            except Exception as e:
                logger.exception(f"  {name} raised — recording as SKIP")
                _skip(skipped, name, f"check raised {type(e).__name__}: {str(e)[:160]}")

        _write_output(issues, skipped)

        if skipped:
            logger.info("\n  NOT EVALUATED this run (these close nothing):")
            for k, v in skipped.items():
                logger.info(f"    {k}: {v}")

        if not issues:
            logger.info("\n  ALL CLEAR — pipeline integrity looks healthy.")
        else:
            severities = [i["severity"] for i in issues]
            logger.info(
                f"\n  DONE — {len(issues)} issue(s) found: "
                + ", ".join(f"{s}×{severities.count(s)}" for s in dict.fromkeys(severities))
            )

        sys.exit(0)

    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    # Woken by wake_agent.py — handle the inbox and exit. Must be the first
    # statement in __main__: everything below this line is the full pipeline,
    # and a wake is explicitly not a request to run it.
    from agent_inbox import handle_wake_if_requested as _wake
    if _wake('CITPipelineMonitor'):
        raise SystemExit(0)
    main()
