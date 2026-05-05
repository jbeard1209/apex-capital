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

BUFFER_PATH     = "data/email_buffer.json"
EMAIL_SENDER    = os.getenv("EMAIL_SENDER",    "jack.beard07@gmail.com")
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "jack.beard07@gmail.com")
GMAIL_PASSWORD  = os.getenv("GMAIL_APP_PASSWORD", "vaje mgnh jhtm kknf")

# ── Urgent keywords — emails containing these bypass the buffer ───────────────
URGENT_PATTERNS = [
    "dead-man",
    "dead man",
    "circuit breaker",
    "critical",
    "⚠️",
    "🚨",
    "from claude",
    "cit sensitive",
    "chairman input required",
    "stop-loss breach",
    "margin call",
    "pipeline stalled",
    "canary red",
    "canary orange",
    "[irm daily check]",
    "irm: risk score",
]


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
    Called at 6 PM EDT by cron. Pass force=True to send even if buffer is empty.
    """
    buffer = _load_buffer()

    if not buffer and not force:
        logger.info("  [EmailBuffer] No buffered emails — digest skipped.")
        return True

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

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_SENDER, GMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"  [EmailBuffer] ✅ Daily digest sent ({count} reports)")

        # Clear the buffer after successful send
        _save_buffer([])
        return True

    except Exception as e:
        logger.error(f"  [EmailBuffer] ❌ Digest send failed: {e}")
        return False


if __name__ == "__main__":
    # Called directly by cron at 6 PM EDT
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    flush_digest()
