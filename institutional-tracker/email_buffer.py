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
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

logger = logging.getLogger(__name__)

from config import EMAIL_SENDER, EMAIL_RECIPIENT, GMAIL_APP_PASSWORD as GMAIL_PASSWORD

BUFFER_PATH     = "data/email_buffer.json"

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


def _stamp_digest_sent(count: int) -> None:
    try:
        os.makedirs(os.path.dirname(DIGEST_STAMP_PATH), exist_ok=True)
        with open(DIGEST_STAMP_PATH, "w") as f:
            json.dump({"date": datetime.now().strftime("%Y-%m-%d"),
                       "sent_at": datetime.now().isoformat(),
                       "reports": count}, f, indent=2)
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


def _load_buffer() -> list:
    """Load the current email buffer."""
    if not os.path.exists(BUFFER_PATH):
        return []
    try:
        with open(BUFFER_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_buffer(buffer: list):
    """Save the email buffer."""
    os.makedirs("data", exist_ok=True)
    with open(BUFFER_PATH, "w") as f:
        json.dump(buffer, f, indent=2, default=str)


def buffer_email(subject: str, html_body: str, sender_name: str = "",
                 key_metrics: Optional[dict] = None) -> bool:
    """
    Add an email to the daily digest buffer.
    Returns True if buffered, False if sent immediately (urgent).
    """
    if _is_urgent(subject, sender_name):
        # Urgent — send immediately, don't buffer
        logger.info(f"  [EmailBuffer] URGENT — sending immediately: {subject[:60]}")
        return False  # caller should send via _send_raw

    buffer = _load_buffer()
    buffer.append({
        "subject":     subject,
        "sender_name": sender_name,
        "html_body":   html_body,
        "key_metrics": key_metrics or {},
        "buffered_at": datetime.now().isoformat(),
    })
    _save_buffer(buffer)
    logger.info(f"  [EmailBuffer] Buffered: {subject[:60]} ({len(buffer)} in queue)")
    return True  # successfully buffered


def flush_digest(force: bool = False) -> bool:
    """
    Compile all buffered emails into one daily digest and send it.
    Called at 7 PM EDT by cron. Pass force=True to send even if buffer is empty.

    Deduplication: if the same subject appears multiple times (e.g. because an
    agent ran hourly and buffered a report each run), only the MOST RECENT entry
    for that subject is included.  This prevents digest bloat when pipelines run
    more than once per day.

    `force=True` bypasses BOTH the once-per-day stamp and the host guard. No
    scheduler passes it — every automated caller invokes this with no arguments
    — so force is a human saying "send it anyway", which is exactly when the
    guards should get out of the way.
    """
    buffer = _load_buffer()

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

    # ── Deduplicate by subject (keep most recent) ─────────────────────────────
    # Walk in reverse order so the last-seen subject wins (most recent run).
    seen_subjects: set = set()
    deduped = []
    for item in reversed(buffer):
        subj = item.get("subject", "")
        # Normalize: strip IRM-review prefix for matching, so an R&D briefing
        # and its IRM review are treated as distinct reports.
        if subj not in seen_subjects:
            seen_subjects.add(subj)
            deduped.append(item)
    buffer = list(reversed(deduped))  # restore chronological order
    dupes_removed = len(_load_buffer()) - len(buffer)
    if dupes_removed > 0:
        logger.info(f"  [EmailBuffer] Removed {dupes_removed} duplicate report(s) before digest")

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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
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
        _stamp_digest_sent(count)
        _save_buffer([])
        return True

    except Exception as e:
        logger.error(f"  [EmailBuffer] ❌ Digest send failed: {e}")
        return False


if __name__ == "__main__":
    # Called directly by cron at 6 PM EDT
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    flush_digest()
