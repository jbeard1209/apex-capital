#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — EMAIL BUFFER & DAILY DIGEST
# =============================================================================
# Instead of sending every agent email immediately, non-urgent emails are
# held in a buffer and compiled into one clean digest at 6 PM EDT daily.
#
# URGENT emails (dead-man's switch, CRITICAL alerts, circuit breakers) always
# bypass the buffer and send immediately.
#
# Buffer file: data/email_buffer.json
# Digest sent: 6 PM EDT (22:00 UTC) daily via cron
# =============================================================================

import os
import json
import fcntl
import smtplib
import logging
import tempfile
from contextlib import contextmanager
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from typing import Optional

logger = logging.getLogger(__name__)

from config import EMAIL_SENDER, EMAIL_RECIPIENT, GMAIL_APP_PASSWORD as GMAIL_PASSWORD

BUFFER_PATH     = "data/email_buffer.json"

# Bounds how long `flush_digest` may hold the buffer lock on a dead network.
SMTP_TIMEOUT_S  = 60

# ── Urgent keywords — emails containing these bypass the buffer ───────────────
URGENT_PATTERNS = [
    # Chairman directive 2026-07-09: ONE daily digest with everything; only
    # "super super urgent" bypasses. The old list (generic "critical", emojis,
    # IRM dailies, canary orange...) produced ~34 emails/week and drowned the
    # 3 that mattered. Immediate-send is now reserved for money-at-risk or
    # firm-down events ONLY. Everything else buffers into the 6 PM digest.
    "dead-man",
    "dead man",
    "circuit breaker",
    "margin call",
    "stop-loss breach",
    "canary red",          # RED only — orange buffers
    "foreign order",       # unknown actor on the trading account
    "pipeline stalled",    # firm-down
]


# ── One digest a day, from one host ──────────────────────────────────────────
# Jack received two digests back to back on 2026-08-17. Nothing was broken; the
# firm simply had four independent things able to call `flush_digest()` and no
# one of them knew about the others:
#
#   VM   cron 22:00 UTC          -> email_buffer.py -> flush_digest()
#   VM   wakeup_recovery.py      -> JOBS line 81, every 6h + @reboot
#   Mac  catch_up.py             -> JOB_REGISTRY["email_digest"]
#   any  a human running the file
#
# Two of those are on one host and one is on the other, so the fix has to be
# two-part. Per-day idempotence alone would still send twice, once per host,
# because the hosts share no state (CLAUDE.md: the ONLY medium they share is
# the broker) and each carries its own `data/email_buffer.json` with its own
# contents. A host guard alone would still let the 22:00 cron and
# wakeup_recovery both fire on the winning host within the same hour.
#
# Note which of the four was NOT the bug. `wakeup_recovery`'s entry is on the
# designated host and exists to re-run a digest the 22:00 cron actually
# missed; deleting it would trade a duplicate email for a silently skipped
# one. It is correct and it stays. What it lacked was a way to find out that
# the cron had already succeeded — `_log_age_hours("digest")` asks how fresh
# the log file is, and a log file is written by whoever ran the script, not by
# whoever sent mail. The stamp below is that missing fact.
#
# `flush_digest` is the choke point every path already goes through, so both
# checks live here rather than in four schedulers that would drift.
DIGEST_STAMP_PATH = "data/.digest_last_sent"

# How stale the VM's state file may be before the Mac stops believing it.
# Same file and same threshold as `catch_up._mac_exit_guard`, deliberately: a
# second hand-rolled liveness test would be a third opinion about whether the
# VM is alive, and the firm has been bitten by two definitions of one fact
# more than once.
_VM_STATE_FILE = "data/vm_state_monitor.json"
_VM_STATE_MAX_AGE_H = 1.0


def _digest_already_sent_today() -> bool:
    """True if this host already sent today's digest."""
    try:
        with open(DIGEST_STAMP_PATH) as f:
            return json.load(f).get("date") == datetime.now().strftime("%Y-%m-%d")
    except (OSError, ValueError, AttributeError):
        return False


def _stamp_digest_sent(count: int, subjects: Optional[list] = None,
                       lost_subjects: Optional[list] = None) -> None:
    """Record the receipt for the digest that just went out.

    `subjects` is a MANIFEST, not decoration. Without it the receipt says only
    "a digest was sent at T with N reports", and `delivery_state` could answer
    the question "did MY message arrive?" no better than `sent_at >= queued_at`
    — which returns "delivered" for a message that was destroyed at 14:00 and
    never appeared in the 22:00 mail. A receipt that proves a digest happened
    is not a receipt that proves this message was in it
    ([[both-sides-of-a-comparison]]).

    `lost_subjects` closes the remaining hole in that argument. A manifest
    proves a SUBJECT was carried; it cannot prove YOUR message was, because the
    subject that survived the collapse is character-for-character the subject
    of every message the collapse destroyed. So for exactly the reports this
    fix is about — a cardinality-bearing subject covering unrelated alarms —
    the receipt confirmed delivery of the one thing it had deleted. The
    membership test was real and its answer was still wrong
    ([[truncate-before-lock]]: the receipt still said delivered).
    """
    try:
        os.makedirs(os.path.dirname(DIGEST_STAMP_PATH) or ".", exist_ok=True)
        with open(DIGEST_STAMP_PATH, "w") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d"),
                       "sent_at": datetime.now().isoformat(),
                       "reports": count,
                       "subjects": sorted(set(subjects or [])),
                       "lost_subjects": sorted(set(lost_subjects or []))},
                      f, indent=2)
    except OSError as e:
        logger.warning(f"  [EmailBuffer] could not write digest stamp: {e}")


def _may_send_digest():
    """(allowed, reason) — is THIS host the one that sends today's digest?

    THIS GUARD FAILS CLOSED, and that is the opposite of `catch_up
    ._mac_exit_guard`, which runs the canary even when its gate is unreadable.
    The asymmetry is the whole argument in both places and it points the other
    way here. That one authorises EXITS: a wrong "no" leaves a position
    bleeding past its stop for days. This one authorises an EMAIL: a wrong
    "yes" is the duplicate digest that started this, and a wrong "no" delays a
    report by a day on a host whose buffer is still sitting there intact.
    Nothing is lost by staying quiet, so when this cannot tell, it stays quiet.
    """
    try:
        import single_writer
        designated = single_writer.designated_host()
        host = single_writer.this_host()
    except Exception as e:
        return False, f"cannot establish host identity ({e}) — standing down"
    if not designated:
        return False, "no designated host recorded — standing down"
    if host == designated:
        return True, f"this host ({host}) is the designated digest sender"

    # Not the designated host. Send only if the designated one has gone quiet,
    # so a dead VM does not cost Jack his digest entirely.
    try:
        with open(_VM_STATE_FILE) as f:
            raw = json.load(f)
        age_h = (datetime.now().timestamp() - os.path.getmtime(_VM_STATE_FILE)) / 3600
    except (OSError, ValueError) as e:
        return False, (f"{host} is not the designated sender ({designated} is) and "
                       f"its state file is unreadable ({e})")
    if age_h > _VM_STATE_MAX_AGE_H:
        return False, (f"{host} is not the designated sender and {_VM_STATE_FILE} is "
                       f"{age_h:.1f}h old — {designated}'s state is unknown, not known-bad")
    if str(raw.get("writer", "")).upper() in ("HIGH", "CRITICAL") or \
            str(raw.get("power", "")).upper() == "CRITICAL":
        return True, (f"{designated} has gone quiet ({raw.get('writer_msg') or raw.get('writer')}) "
                      f"— {host} is sending the digest in its place")
    return False, f"{designated} is live and owns the digest; {host} stands down"


STRANDED_BUFFER_ALERT_AT = 25
STRANDED_BUFFER_MAX_AGE_H = 72


def _report_stranded_buffer(buffer: list, why: str) -> None:
    """A host that stands down must not let its buffer rot in silence.

    Standing down is the right call for one evening. It is not the right call
    for a month, and the difference is invisible from inside this function —
    the buffer just keeps growing, exactly as it did when the Chairman queue
    picked up "email buffer overflow — 87 unsent emails" from the far side of
    the same problem. So the quiet path has a consequence attached to it: past
    a count or an age, the stranding itself becomes a Chairman item.

    Deliberately NOT an email. The whole purpose of this branch is that this
    host has been told not to email, and a host that answers "I have been
    silenced" by sending mail has not been silenced.
    """
    if not buffer:
        return
    oldest_h = None
    try:
        stamps = [datetime.fromisoformat(str(b.get("buffered_at"))[:26])
                  for b in buffer if b.get("buffered_at")]
        if stamps:
            oldest_h = (datetime.now() - min(stamps)).total_seconds() / 3600
    except (TypeError, ValueError):
        oldest_h = None
    if len(buffer) < STRANDED_BUFFER_ALERT_AT and not (
            oldest_h and oldest_h > STRANDED_BUFFER_MAX_AGE_H):
        return
    detail = (f"{len(buffer)} report(s) buffered on a host that is not sending the "
              f"digest" + (f", oldest {oldest_h:.0f}h old" if oldest_h else "") +
              f". Reason given: {why}")
    logger.warning(f"  [EmailBuffer] STRANDED — {detail}")
    try:
        import chairman_queue
        # `submit(entries, source)` with formed dicts — NOT an `add(...)` helper.
        # The first draft of this called `chairman_queue.add(...)`, which does
        # not exist; the except below would have swallowed the AttributeError
        # and the stranded buffer would have been reported to nobody, by the
        # very code written to stop it being reported to nobody. Producer
        # calling a consumer that was never checked is the house bug
        # (memory `wiring_contract_bugs`), and it is guarded in
        # TestWiringContracts.
        chairman_queue.submit([{
            "id":          f"digest-stranded-{datetime.now().strftime('%Y%m%d')}",
            "dedup_key":   "digest_buffer_stranded",
            "severity":    "MEDIUM",
            "description": detail,
            "ts":          datetime.now().isoformat(),
            "status":      "awaiting_chairman",
        }], "email_buffer")
    except Exception as e:
        logger.warning(f"  [EmailBuffer] could not queue stranded-buffer item: {e}")


def _is_urgent(subject: str, sender_name: str = "") -> bool:
    """Check if an email should bypass the buffer and send immediately."""
    combined = (subject + " " + sender_name).lower()
    return any(p.lower() in combined for p in URGENT_PATTERNS)


class BufferUnreadable(Exception):
    """The buffer file exists but could not be parsed.

    Raised only on the mutating paths. A reader may degrade to "nothing
    queued"; a writer may not, because the next thing a writer does is save,
    and saving over an unreadable file destroys whatever was in it.
    """


def _buffer_dir() -> str:
    # NOT the literal "data": BUFFER_PATH is patched to a tmpdir in tests, and
    # os.makedirs("data") would then create a stray directory in the cwd while
    # the real write went somewhere else entirely.
    return os.path.dirname(BUFFER_PATH) or "."


@contextmanager
def _exclusive():
    """Hold ONE exclusive lock for an entire read-modify-write of the buffer.

    Locking the load and the save separately grants no mutual exclusion at
    all: two producers can both read a 40-entry queue, both append their own
    entry, and both write 41 — and one of the two messages is gone. The lock
    has to span the read AND the write or it is decoration.

    The lock lives in a SEPARATE file. It cannot live in the buffer itself,
    because the buffer is replaced by rename and the lock would follow the
    inode that just got unlinked.
    """
    os.makedirs(_buffer_dir(), exist_ok=True)
    f = open(BUFFER_PATH + ".lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


def _locked(fn):
    """Serialise a whole load-mutate-save against every other writer.

    ⚠ NEVER call one `@_locked` function from inside another. flock is held
    per open file description, so the inner `_exclusive()` opens a second
    descriptor on the same file and blocks forever waiting for the outer one
    to release. A deadlocked `flush_digest` is total email silence to Jack —
    the single worst failure this module has — so it is pinned by
    `test_no_locked_function_calls_another` rather than left to memory.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _exclusive():
            return fn(*args, **kwargs)
    return wrapper


def _load_buffer(strict: bool = False) -> list:
    """Load the current email buffer.

    `strict` separates the two things a bare `except: return []` conflates —
    "nothing is queued" and "I could not read the queue". They must not share
    an answer, because the caller's next move after the second one is to write.
    """
    if not os.path.exists(BUFFER_PATH):
        return []                     # genuinely empty — safe to build on
    try:
        with open(BUFFER_PATH) as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"buffer is {type(data).__name__}, expected list")
        return data
    except Exception as e:            # noqa: BLE001
        if strict:
            raise BufferUnreadable(f"{BUFFER_PATH}: {e}") from e
        logger.warning(f"  [EmailBuffer] could not read buffer: {e}")
        return []


def _load_buffer_for_write() -> list:
    """Load on a mutating path, never silently substituting [] for a bad read.

    The old `_load_buffer` returned [] on any exception, and the caller then
    appended one entry and saved — turning an unreadable file into a queue of
    exactly one message and erasing the day's alerts with no error anywhere.
    Unparseable bytes are moved aside as evidence instead of being written over.
    """
    try:
        return _load_buffer(strict=True)
    except BufferUnreadable as e:
        quarantine = f"{BUFFER_PATH}.corrupt-{datetime.now():%Y%m%dT%H%M%S}"
        try:
            os.replace(BUFFER_PATH, quarantine)
            logger.error(f"  [EmailBuffer] UNREADABLE buffer quarantined to "
                         f"{quarantine} — starting a new queue. {e}")
        except OSError as move_err:
            logger.error(f"  [EmailBuffer] UNREADABLE buffer and it could not be "
                         f"moved aside ({move_err}) — REFUSING to write. {e}")
            raise
        return []


def _save_buffer(buffer: list):
    """Write the buffer atomically. The live file is never truncated in place.

    `open(BUFFER_PATH, "w")` destroys the queue at open(), before a single byte
    of the replacement is written, so any reader arriving in that window sees an
    empty or half-written file. Measured on a 400-entry queue: 68 of 300
    concurrent reads came back empty, and 200 concurrent enqueues left 7
    survivors. mkstemp gives each writer a private name and os.replace swaps it
    in atomically — a reader sees the whole old queue or the whole new one.
    """
    os.makedirs(_buffer_dir(), exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=_buffer_dir(), prefix=".email_buffer.",
                                   suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(buffer, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BUFFER_PATH)
    except Exception as e:            # noqa: BLE001
        logger.error(f"  [EmailBuffer] could not save buffer: {e}")
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


@_locked
def buffer_email(subject: str, html_body: str, sender_name: str = "",
                 key_metrics: Optional[dict] = None,
                 dedup_key: Optional[str] = None) -> bool:
    """
    Add an email to the daily digest buffer.
    Returns True if buffered, False if sent immediately (urgent).

    `dedup_key` is the producer's answer to "which of these are the same
    message?", and it exists because the digest's own answer — the subject
    line — is a display string, and a display string may carry a COUNT where
    an identity belongs.

    `coherence_laws.notify_violations` is the case that proved it. It already
    dedupes: it buffers only when the set of violations CHANGES, so every mail
    it hands over is, by construction, news. Its subject is
    "Coherence: N law(s) violated on <host>" — a cardinality. On 2026-08-27 the
    Mac's buffer held four genuinely different alarms under two such subjects,
    including `nested_probabilities_are_monotone` (buffered 32 times across two
    subjects, on the Kalshi ladders the whole sector rests on) and
    `deployed_set_matches_repo` (the VM running code that is not the repo's).
    The next flush would have collapsed them to the newest per subject, logged
    it as "Removed N duplicate report(s)", and delivered the config-drift one.

    Two dedupe layers, the second strictly coarser than the first, and the
    coarse one deleting what the fine one had certified as news.

    Producers that pass nothing keep exactly today's behaviour (dedupe on
    subject), because for a report whose subject names it — "[CRO REPORT]
    ... Risk:MODERATE" — six regenerations in a day SHOULD collapse to the
    freshest. The key is for the producer that knows its subject does not
    identify it.
    """
    # ── transposed-argument guard ────────────────────────────────────────────
    # The signature is (subject, html_body, sender_name) and on 2026-08-27
    # wakeup_recovery.py called buffer_email("wakeup_recovery", subject, body).
    # Nothing raised: three strings went into three string parameters, one place
    # to the left. Jack received mail with the subject "wakeup_recovery", a body
    # consisting solely of the real subject line, and the entire recovery report
    # — which jobs re-ran, which were already fresh — stuffed into sender_name,
    # where the digest prints it as the agent's name. The one email whose whole
    # job is to say what broke and whether it came back could not say it, and
    # since sender_name also selects the IRM reviewer, a multi-line report was
    # being used as a dispatch key.
    #
    # Static typing cannot see this and neither can a test of the call sites:
    # all 18 in the firm are individually well-formed, and the 6 positional ones
    # are correct. But the arguments are not interchangeable in SHAPE. Across
    # every sender_name literal in the firm the longest is 19 characters
    # ("ML Improvement Loop"), the wordiest is 3 words, and not one contains a
    # newline — because it is an identifier. A document in that slot is a
    # transposition, always.
    #
    # This logs and stamps rather than raising. A mangled email is bad; a
    # pipeline that dies at 06:10 because its status mail was malformed is
    # worse, and dropping the mail would destroy the very evidence of the bug.
    _arg_order_suspect = bool(sender_name) and (
        "\n" in sender_name or len(sender_name) > 64
    )
    if _arg_order_suspect:
        logger.error(
            "  [EmailBuffer] ARGUMENT ORDER SUSPECT — sender_name is %d chars"
            "%s, but every legitimate sender in this firm is a short identifier"
            " (longest: 19 chars, no newlines). This is the signature of a"
            " positional call written as (sender, subject, body) against"
            " (subject, html_body, sender_name). Subject was: %r",
            len(sender_name), " and contains a newline" if "\n" in sender_name else "",
            subject[:80],
        )

    # Deliberately AFTER the guard above: this path returns early, so a
    # transposed call that happened to look urgent would otherwise skip the
    # check entirely — and _is_urgent reads BOTH subject and sender_name, so it
    # is itself being fed the wrong two strings in exactly that case.
    if _is_urgent(subject, sender_name):
        # Urgent — send immediately, don't buffer
        logger.info(f"  [EmailBuffer] URGENT — sending immediately: {subject[:60]}")
        return False  # caller should send via _send_raw

    buffer = _load_buffer_for_write()
    buffer.append({
        "subject":     subject,
        "sender_name": sender_name,
        "html_body":   html_body,
        "key_metrics": key_metrics or {},
        "dedup_key":   dedup_key or "",
        "buffered_at": datetime.now().isoformat(),
        **({"arg_order_suspect": True} if _arg_order_suspect else {}),
    })
    _save_buffer(buffer)
    logger.info(f"  [EmailBuffer] Buffered: {subject[:60]} ({len(buffer)} in queue)")
    return True  # successfully buffered


def delivery_state(buffered_at, subject: str = "") -> tuple:
    """(state, why) for something handed to buffer_email — did it ARRIVE?

    'delivered' | 'pending' | 'lost' | 'unknown'

    buffer_email() returning True means ENQUEUED, not sent. Every caller that
    stamps "I notified Jack" on that return is asserting a delivery it has no
    evidence for: if the digest then fails, dedupes, or the buffer is dropped,
    the caller's own state file says the message went out and it stays quiet.
    `kalshi_calibration.email_verdict` did exactly that — the verdict is
    buffered at 11:45 UTC and delivered by the 22:00 digest ten hours later,
    and the state file was stamped at 11:45. One failed digest would have
    silenced the sector for VERDICT_HEARTBEAT_DAYS with the state file
    insisting Jack had been told.

    This answers it from the two records that actually know, so no caller has
    to guess:
      * the buffer itself — still queued means pending, not lost;
      * data/.digest_last_sent, whose `sent_at` is written ONLY after the SMTP
        send returns (see flush_digest), so it is a receipt, not an intent.

    'unknown' is for a caller with no recorded enqueue time (state written
    before this existed). It is deliberately NOT 'lost': upgrading the code
    must not mail Jack a backlog of verdicts he already read.
    """
    if not buffered_at:
        return "unknown", "no enqueue time recorded"
    try:
        when = datetime.fromisoformat(str(buffered_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "unknown", f"unparseable enqueue time {buffered_at!r}"

    if subject:
        for e in _load_buffer():
            if e.get("subject") == subject:
                return "pending", "still queued for the next digest"

    try:
        with open(DIGEST_STAMP_PATH) as f:
            stamp = json.load(f)
        sent_at = datetime.fromisoformat(str(stamp.get("sent_at")))
    except (OSError, ValueError, TypeError, AttributeError):
        # No receipt on this host is NOT evidence of loss, and resolving it to
        # 'lost' would make every caller re-send on every run of any host that
        # has never flushed a digest — the Mac, for one. It also cannot hide a
        # real loss: flush_digest stamps BEFORE it clears the buffer, so a
        # message that vanished from the buffer with no stamp anywhere is a
        # failed stamp write, not an undelivered email. Absence of evidence
        # gets its own answer ([[verdict-with-no-consequence]]).
        return "unknown", "no digest receipt on this host — cannot tell"

    # Compare like with like: the buffer writes naive local time, the stamp
    # writes naive local time, but a caller may hand us an aware timestamp.
    if (when.tzinfo is None) != (sent_at.tzinfo is None):
        when = when.replace(tzinfo=None)
        sent_at = sent_at.replace(tzinfo=None)

    if sent_at < when:
        return "lost", (f"queued {buffered_at} but the newest digest receipt is "
                        f"{stamp.get('sent_at')} — it left the buffer without "
                        f"being sent")

    # The digest that could have carried it has been sent. Two different
    # questions remain, and the clock answers neither: was THIS subject in it?
    manifest = stamp.get("subjects")
    if subject and isinstance(manifest, list):
        # Membership in the manifest is necessary, not sufficient. If a message
        # under this subject was ALSO destroyed by the collapse, then one copy
        # went and at least one did not, and this function cannot tell which
        # one the caller is holding. Saying "delivered" there is the receipt
        # confirming the message it deleted.
        if subject in (stamp.get("lost_subjects") or []):
            return "unknown", (
                f"digest sent {stamp.get('sent_at')} carrying this subject, but "
                f"another report with the SAME subject and different content was "
                f"dropped in the same flush — the receipt cannot say which copy "
                f"this was. The producer must pass dedup_key= to make its "
                f"messages distinguishable.")
        if subject in manifest:
            return "delivered", (f"digest sent {stamp.get('sent_at')} and the "
                                 f"receipt names this subject")
        return "lost", (f"digest sent {stamp.get('sent_at')} with "
                        f"{stamp.get('reports')} report(s), and this subject is "
                        f"not among them — it was dropped between the enqueue "
                        f"and the send")

    # A receipt from before the manifest existed, or a caller with no subject to
    # match. Fall back to the clock and SAY SO, rather than reporting a
    # membership check that did not happen: this cannot see a message destroyed
    # in the buffer, and the caller deserves to know which of the two answers
    # it is holding.
    return "delivered", (f"digest sent {stamp.get('sent_at')} "
                         f"({stamp.get('reports')} reports) — inferred from the "
                         f"send time, no subject manifest to confirm it")


@_locked
def flush_digest(force: bool = False) -> bool:
    """
    Compile all buffered emails into one daily digest and send it.
    Called at 7 PM EDT by cron. Pass force=True to send even if buffer is empty.

    THE LOCK SPANS THE SMTP SEND, deliberately. The alternative — claim the
    batch, release, then send — loses the whole batch if the process dies
    between the claim and the send, which is a strictly worse failure than a
    producer waiting a few seconds. Nothing in this function may enqueue an
    email while it holds the lock; see `_locked`.

    Deduplication: if the same IDENTITY appears multiple times (e.g. because an
    agent ran hourly and buffered a report each run), only the MOST RECENT entry
    for it is included.  This prevents digest bloat when pipelines run more than
    once per day. The identity is the producer's `dedup_key` when it supplied
    one, and the subject line otherwise — the subject was the only key until
    2026-08-27, when four distinct coherence alarms were found queued under two
    count-bearing subjects, and the log line for destroying them read "Removed
    N duplicate report(s)".

    `force=True` bypasses BOTH the once-per-day stamp and the host guard. No
    scheduler passes it — every automated caller invokes this with no arguments
    — so force is a human saying "send it anyway", which is exactly when the
    guards should get out of the way.
    """
    buffer = _load_buffer_for_write()
    buffered_in = len(buffer)

    if not buffer and not force:
        logger.info("  [EmailBuffer] No buffered emails — digest skipped.")
        return True

    if not force:
        if _digest_already_sent_today():
            logger.info("  [EmailBuffer] Digest already sent today — skipped. "
                        f"({len(buffer)} report(s) will roll into tomorrow's.)")
            return True
        allowed, why = _may_send_digest()
        if not allowed:
            logger.info(f"  [EmailBuffer] Not sending: {why}")
            _report_stranded_buffer(buffer, why)
            return True

    # ── Collapse re-runs of the same report (keep most recent) ────────────────
    # Keyed on the PRODUCER's identity when it supplied one, and on the subject
    # otherwise. A subject is a display string: it may name the report, and it
    # may instead carry a count ("Coherence: 1 law(s) violated on <host>"), in
    # which case two unrelated alarms are one key and one of them is destroyed.
    # See buffer_email's docstring for the four that were queued for exactly
    # that on 2026-08-27.
    #
    # Walk in reverse so the last-seen key wins (most recent run).
    seen_keys: set = set()
    deduped = []
    dropped = []
    for item in reversed(buffer):
        key = item.get("dedup_key") or item.get("subject", "")
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)
        else:
            dropped.append(item)
    buffer = list(reversed(deduped))  # restore chronological order
    # Measured against what was READ, not against a second read of the file.
    # Re-reading asked the disk a question this function had already answered,
    # and would report a negative count the moment a producer wrote in between.
    dupes_removed = buffered_in - len(buffer)

    # WAS IT A DUPLICATE, OR JUST A COLLISION? The old log line asserted the
    # first and could not tell the difference. A drop whose body matches the
    # survivor's is certainly a re-run. A drop whose body DIFFERS is content
    # that existed and now does not — and it is EITHER the same report with
    # fresher numbers (fine, and what the collapse is for) OR two unrelated
    # reports sharing a display string (the coherence loss).
    #
    # This function cannot tell those apart, and neither can any rule written
    # here. "[CRO REPORT] ... Risk:MODERATE | Manager Grade: B" carries its own
    # state in the subject, so its differing bodies are regenerations and its
    # collapse is right; "Coherence: 1 law(s) violated" carries a count, so its
    # differing bodies are different alarms. Same evidence, opposite meaning.
    # Only the producer knows which it is, which is exactly why `dedup_key`
    # exists rather than a cleverer heuristic in here.
    #
    # So this records the census and does NOT raise an alarm on it. A WARNING
    # every night naming six producers whose behaviour is correct is the noise
    # this module exists to prevent, and it would bury the one line that
    # matters. What the census IS good for is the consequential half below: a
    # caller asking "did MY copy go out?" about a collided subject must not be
    # told yes.
    kept_body = {(i.get("dedup_key") or i.get("subject", "")): i.get("html_body")
                 for i in buffer}
    collisions = [i for i in dropped
                  if i.get("html_body") != kept_body.get(
                      i.get("dedup_key") or i.get("subject", ""))]
    undeclared = [i for i in collisions if not i.get("dedup_key")]
    if dupes_removed > 0:
        logger.info(f"  [EmailBuffer] Collapsed {dupes_removed} re-run(s) of "
                    f"reports already in this digest")
    if undeclared:
        who = sorted({i.get("subject", "")[:70] for i in undeclared})
        logger.info(
            "  [EmailBuffer] %d of those had DIFFERENT content from the copy "
            "that survived, under a subject-only key — re-runs with fresher "
            "numbers, or distinct reports sharing a subject; the producer "
            "declared no identity, so this cannot say which. Recorded in the "
            "receipt as unproven delivery for: %s"
            % (len(undeclared), "; ".join(who)))

    today = datetime.now().strftime("%A, %B %d, %Y")
    count = len(buffer)

    # ── Build digest HTML ─────────────────────────────────────────────────────
    sections_html = ""
    for i, item in enumerate(buffer):
        sender   = item.get("sender_name", "Unknown Agent")
        subj     = item.get("subject", "(no subject)")
        body     = item.get("html_body", "")
        buffered = item.get("buffered_at", "")[:16].replace("T", " ")
        metrics  = item.get("key_metrics", {})

        # Extract key metrics summary if available
        metrics_html = ""
        if metrics:
            metric_rows = "".join(
                f"<span style='background:#1a2a1a;padding:2px 8px;border-radius:3px;"
                f"margin:2px;display:inline-block;font-size:12px;'>"
                f"<span style='color:#8b949e;'>{k}:</span> "
                f"<span style='color:#e6edf3;'>{v}</span></span>"
                for k, v in list(metrics.items())[:6]
            )
            metrics_html = f"<div style='margin:8px 0;'>{metric_rows}</div>"

        sections_html += f"""
  <div style="margin-bottom:24px;border:1px solid #21262d;border-radius:6px;overflow:hidden;">
    <div style="background:#161b22;padding:10px 16px;border-bottom:1px solid #21262d;">
      <span style="color:#58a6ff;font-weight:bold;font-size:13px;">{sender}</span>
      <span style="color:#8b949e;font-size:12px;margin-left:10px;">{buffered}</span>
      <div style="color:#e6edf3;font-size:14px;margin-top:4px;">{subj}</div>
      {metrics_html}
    </div>
    <div style="padding:14px 16px;font-size:13px;color:#c9d1d9;
                max-height:400px;overflow:hidden;position:relative;">
      {body}
      <div style="position:absolute;bottom:0;left:0;right:0;height:60px;
                  background:linear-gradient(transparent,#0d1117);"></div>
    </div>
  </div>"""

    if not buffer:
        sections_html = """
  <div style="text-align:center;padding:40px;color:#8b949e;">
    No agent activity to report today.
  </div>"""

    # ── Issues & fixes — the other half of every warning above ────────────────
    # Chairman directive 2026-08-17. Reports say what is wrong; this says what
    # is being DONE about what is wrong, and closes each item exactly once.
    # It renders even on a day with no buffered reports, because "the board
    # fixed X" is worth sending on a quiet day and a quiet day is precisely
    # when a stalled fix is easiest to miss.
    issues_html = ""
    try:
        import board_issue_ledger
        issues_html = board_issue_ledger.digest_section()["html"]
    except Exception as e:
        # Loud, not silent. If this block vanishes quietly, the digest reverts
        # to a list of complaints with no visible sign that the loop is broken,
        # which is the state the directive was written to end.
        logger.warning(f"  [EmailBuffer] issues section unavailable: {e}")
        issues_html = ("<p style='color:#f85149;font-size:12px;'>Issues &amp; fixes "
                       "section could not be built — the board-issue ledger is not "
                       "reporting. Treat the alerts above as UNROUTED.</p>")

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#0d1117;color:#c9d1d9;
             max-width:800px;margin:0 auto;padding:20px;">

  <div style="border-bottom:3px solid #238636;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="color:#238636;margin:0;font-size:22px;">
      📋 Apex Capital — Daily Digest
    </h1>
    <p style="color:#8b949e;margin:6px 0 0 0;font-size:14px;">
      {today} &nbsp;·&nbsp; {count} report(s) from your agents
    </p>
  </div>

  {sections_html}

  {issues_html}

  <div style="border-top:1px solid #21262d;margin-top:24px;padding-top:12px;
              font-size:11px;color:#484f58;text-align:center;">
    Apex Capital Daily Digest &nbsp;·&nbsp; All urgent alerts were sent immediately &nbsp;·&nbsp;
    This digest covers {today}
  </div>

</body>
</html>"""

    # ── Send the digest ───────────────────────────────────────────────────────
    subject = f"📋 Apex Capital Daily Digest — {today} ({count} report{'s' if count != 1 else ''})"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_SENDER
        msg["To"]      = EMAIL_RECIPIENT
        msg.attach(MIMEText(html, "html"))

        try:
            import mail_guard; mail_guard.allow_direct(True)  # THE one daily email
        except Exception:
            pass
        # An explicit timeout, because this call is made holding the buffer
        # lock: without one, smtplib inherits the socket default (often none)
        # and a hung SMTP connection would block every agent trying to enqueue
        # an alert, for as long as the network stayed silent.
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=SMTP_TIMEOUT_S) as server:
            server.login(EMAIL_SENDER, GMAIL_PASSWORD)
            server.send_message(msg)
        try:
            import mail_guard; mail_guard.allow_direct(False)
        except Exception:
            pass

        logger.info(f"  [EmailBuffer] ✅ Daily digest sent ({count} reports)")

        # Stamp BEFORE clearing. If the process dies between the two, the worst
        # case is a buffer that rolls into tomorrow's digest — reports arrive
        # late. Stamping after would leave the window the other way round: a
        # cleared buffer with no stamp, so the next caller this evening sends a
        # second, empty-ish digest. That is the failure being fixed here.
        _stamp_digest_sent(count, [i.get("subject", "") for i in buffer],
                           [i.get("subject", "") for i in undeclared])
        _save_buffer([])
        return True

    except Exception as e:
        logger.error(f"  [EmailBuffer] ❌ Digest send failed: {e}")
        return False


if __name__ == "__main__":
    # Called directly by cron at 6 PM EDT
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    flush_digest()
