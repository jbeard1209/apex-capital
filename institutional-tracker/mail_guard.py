#!/usr/bin/env python3
# =============================================================================
# APEX CAPITAL — MAIL GUARD (the REAL one-digest chokepoint)
# =============================================================================
# Why this exists (and why two earlier fixes failed):
#   The one-digest policy was first enforced in email_buffer.buffer_email, then
#   in email_sender.send_alert_email. But ~20 scripts (fe_risk_monitor,
#   fi_risk_monitor, performance_tracker, portfolio_risk_dashboard, every
#   ml_chain_r*, ml_longterm_board, fe_pipeline, …) open their OWN
#   smtplib.SMTP/SMTP_SSL connection and call sendmail()/send_message()
#   DIRECTLY — bypassing both guards. Some of those files don't even exist on
#   the Mac (they live only on the production VM), so per-file edits can't be
#   verified or fully deployed. Result: Jack kept getting dozens of emails/day.
#
# The fix: intercept at the layer EVERY sender shares — smtplib itself. Almost
# every Apex script does `from config import ...`, and config.py calls
# install() on import, so this guard is active in every process, for every
# current/future/VM-only sender, from ONE place.
#
# Policy: an intercepted outgoing message is TRANSMITTED only if
#   (a) a caller explicitly allowed direct send (the daily digest flush), or
#   (b) its subject is on the urgent whitelist (email_buffer._is_urgent).
# Otherwise it is diverted into the daily-digest buffer and NOT transmitted.
# =============================================================================

import logging
import smtplib
import email as _email

logger = logging.getLogger("mail_guard")

# When True, intercepted sends transmit for real. The digest flush sets this
# (the ONE email/day that must actually go out); everything else stays False.
ALLOW_DIRECT = False
_INSTALLED = False


def allow_direct(flag: bool = True):
    """Context-free toggle used by the digest flush to permit its own real send."""
    global ALLOW_DIRECT
    ALLOW_DIRECT = flag


def _extract(subject_or_msg, *args):
    """Return (subject, html_body) from either a MIME message (send_message) or
    a raw RFC-822 string / (from, to, body) tuple (sendmail)."""
    try:
        if hasattr(subject_or_msg, "get"):                 # send_message(msg)
            msg = subject_or_msg
        else:                                              # sendmail(from,to,msg)
            raw = args[1] if len(args) >= 2 else subject_or_msg
            msg = _email.message_from_string(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
        subject = msg.get("Subject", "") or ""
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    body = part.get_payload(decode=False); break
            if not body:
                body = msg.get_payload(0).get_payload() if msg.get_payload() else ""
        else:
            body = msg.get_payload()
        if not isinstance(body, str):
            body = str(body)
        return subject, body
    except Exception as e:
        logger.debug(f"mail_guard extract failed: {e}")
        return "", ""


def _should_transmit(subject: str) -> bool:
    if ALLOW_DIRECT:
        return True
    try:
        from email_buffer import _is_urgent
        return _is_urgent(subject)
    except Exception:
        return True   # fail-open: never SILENTLY drop mail we couldn't classify


def _divert(subject: str, body: str):
    try:
        from email_buffer import buffer_email
        buffer_email(subject=subject, html_body=body, sender_name="Auto (mail_guard)")
        logger.info(f"  [mail_guard] diverted to daily digest: {subject[:60]}")
    except Exception as e:
        logger.warning(f"  [mail_guard] could not buffer, dropping direct send: {e}")


def install():
    """Monkeypatch smtplib so sendmail/send_message obey the one-digest policy."""
    global _INSTALLED
    if _INSTALLED:
        return
    for cls in (smtplib.SMTP, smtplib.SMTP_SSL):
        _orig_sendmail = cls.sendmail
        _orig_send_message = cls.send_message

        def _guard_sendmail(self, *a, __orig=_orig_sendmail, **k):
            subject, body = _extract(*a) if a else ("", "")
            if _should_transmit(subject):
                return __orig(self, *a, **k)
            _divert(subject, body)
            return {}   # sendmail returns {} on full success

        def _guard_send_message(self, msg, *a, __orig=_orig_send_message, **k):
            subject, body = _extract(msg)
            if _should_transmit(subject):
                return __orig(self, msg, *a, **k)
            _divert(subject, body)
            return {}

        cls.sendmail = _guard_sendmail
        cls.send_message = _guard_send_message
    _INSTALLED = True
    logger.info("  [mail_guard] installed — all SMTP sends obey one-digest policy")
