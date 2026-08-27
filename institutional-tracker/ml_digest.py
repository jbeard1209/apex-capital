#!/usr/bin/env python3
"""
ML DIGEST — the WARNING half of the ML monitors' escalation path.

WHAT WAS WRONG
--------------
`ml_data_watchdog`, `ml_drift_monitor` and `ml_feature_drift_monitor` each split
their findings by severity: CRITICAL went out by email, WARNING went to
`data/digest_queue.json`. Nothing has ever read that file. Its only three
mentioning modules are those same three writers, each with a private
load-append-store — a writer re-reading its own output, which every static
"does anyone reference this?" test counts as a consumer.

So the WARNING branch was a no-op with a receipt. Three ML data-quality warnings
(feature null rates, date continuity, label-count trend) sat in it from
2026-06-12 to 2026-08-26 — 75 days, ten complete passes of the firm's entire
schedule — and no detector said a word. The severity-below-the-escalation-floor
shape again (memory `severity-below-the-escalation-floor`): only the top band
was wired to an owner, so everything under it was written down instead of sent.

WHAT THIS DOES
--------------
Delivers through `email_buffer.buffer_email`, which IS the firm's digest path —
one batched mail a day, through mail_guard, with `_is_urgent` free to promote
anything that deserves promoting. That is the consumer the old code's own
docstring claimed ("for daily digest") and never had.

`digest_queue.json` survives as a DEAD-LETTER file, and that is a deliberate
inversion of what it used to be. It used to hold everything, so its contents
meant nothing; it now holds only what could not be delivered, so a row in it is
a fault by construction — and `coherence_laws.law_work_queues_drain` measures
the age of the oldest row against a deadline derived from the schedule, so the
file cannot go quiet again. Rows that later deliver are removed, which is what
makes it a queue rather than one more log nobody drains.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DIGEST_QUEUE_PATH = DATA_DIR / "digest_queue.json"

log = logging.getLogger(__name__)


def _load_dead_letters() -> List[dict]:
    if not DIGEST_QUEUE_PATH.exists():
        return []
    try:
        rows = json.loads(DIGEST_QUEUE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        log.warning("digest_queue.json is malformed — treating as empty.")
        return []
    return rows if isinstance(rows, list) else []


def _save_dead_letters(rows: List[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIGEST_QUEUE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(DIGEST_QUEUE_PATH)


def _entry_html(entry: dict) -> str:
    detail = entry.get("detail")
    if isinstance(detail, dict):
        body = "".join(
            f"<tr><td style='padding:2px 10px 2px 0;color:#8b949e;'>{k}</td>"
            f"<td style='padding:2px 0;'>{v}</td></tr>"
            for k, v in detail.items())
        detail_html = f"<table style='font-size:13px;'>{body}</table>"
    else:
        detail_html = f"<p style='font-size:13px;'>{detail}</p>"
    return (f"<h3 style='margin:14px 0 4px;font-size:14px;'>"
            f"{entry.get('subject', 'ML warning')}</h3>"
            f"<p style='color:#8b949e;font-size:12px;margin:0 0 6px;'>"
            f"{entry.get('source', 'ml')} &middot; {entry.get('timestamp', '')}</p>"
            f"{detail_html}")


def deliver(entries: List[dict], source: Optional[str] = None) -> bool:
    """Send WARNING-level ML findings to the daily digest. True if delivered.

    Anything that fails to deliver — including a `buffer_email` that declines
    because the subject reads as urgent, which means the caller was supposed to
    send it itself — is written to the dead-letter file rather than dropped.
    Silence is the one outcome this path is not allowed to have: it is what the
    old code did on every single call, successfully, for 75 days.

    Delivery is attempted for the BACKLOG TOO. A dead-letter file that only ever
    grows is the failure this module was written to end, so a run that can reach
    the digest drains what earlier runs could not.
    """
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    backlog = _load_dead_letters()
    pending = backlog + entries
    if not pending:
        return True

    try:
        import email_buffer
    except Exception as e:                       # pragma: no cover - import guard
        log.error("digest unreachable (%s: %s) — %d entry/entries dead-lettered",
                  type(e).__name__, e, len(pending))
        _save_dead_letters(pending)
        return False

    src = source or (pending[0].get("source") if pending else "ml")
    subject = (f"ML Data Quality — {len(pending)} WARNING"
               f"{'' if len(pending) == 1 else 's'}")
    html = ("<p style='font-size:13px;'>Below the CRITICAL band, so this is "
            "batched rather than paged. Every entry is a check that fired.</p>"
            + "".join(_entry_html(e) for e in pending))

    try:
        ok = email_buffer.buffer_email(subject, html, sender_name=str(src),
                                       key_metrics={"warnings": len(pending)})
    except Exception as e:
        log.error("digest delivery raised %s: %s — %d entry/entries dead-lettered",
                  type(e).__name__, e, len(pending))
        _save_dead_letters(pending)
        return False

    if not ok:
        # buffer_email returns False for URGENT subjects, meaning "you send it".
        # These monitors have no raw-send path of their own and must not grow
        # one — mail_guard exists because raw smtplib is how the email flood
        # happened (memory `email_leak_root_cause`). Dead-letter it instead, so
        # the drain law reports a delivery path that needs a decision.
        log.warning("digest declined %d entry/entries (urgent path) — dead-lettered",
                    len(pending))
        _save_dead_letters(pending)
        return False

    if backlog:
        log.info("digest delivered %d new + %d recovered dead-letter entry/entries",
                 len(entries), len(backlog))
    _save_dead_letters([])
    return True


def drain_backlog() -> bool:
    """Retry whatever earlier runs could not deliver. Call once per monitor run.

    Without this the backlog could only drain WHEN NEW WORK ARRIVED, because
    `deliver` is reached from inside each monitor's `if WARNING:` branch. A queue
    that clears only on the arrival of another item is not drained; it is one
    quiet week away from being exactly the stuck file this module replaced.
    """
    return deliver([])


def dead_letter_age_days(now: Optional[datetime] = None) -> Optional[float]:
    """Age of the oldest undelivered entry, or None if there are none.

    Exposed so a monitor can report its own backlog without re-implementing the
    parse. `coherence_laws.law_work_queues_drain` measures the same quantity from
    the file directly and is the authority — this is a convenience, not a second
    source, and it must never be used to certify the queue to itself.
    """
    now = now or datetime.now(timezone.utc)
    oldest = None
    for row in _load_dead_letters():
        raw = row.get("timestamp") if isinstance(row, dict) else None
        if not isinstance(raw, str):
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if oldest is None or dt < oldest:
            oldest = dt
    return None if oldest is None else (now - oldest).total_seconds() / 86400.0
