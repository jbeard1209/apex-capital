# =============================================================================
# EMAIL SENDER — Gmail App Password
# =============================================================================
# Sends the daily HTML report via Gmail's SMTP with App Password auth.
# =============================================================================

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Optional

from config import EMAIL_SENDER, EMAIL_RECIPIENT, GMAIL_APP_PASSWORD

logger = logging.getLogger(__name__)

GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

# Prevent IRM review re-entrancy (IRM reviewing its own review)
_irm_review_active = False


def _send_raw(html_body: str, subject: str) -> bool:
    """
    Low-level SMTP send. Used internally and by irm_review_agent.
    Does NOT trigger another IRM review.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText("Please view in an HTML-capable email client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"❌ _send_raw failed: {e}")
        return False


def send_daily_report(
    html_body:   str,
    subject:     Optional[str] = None,
    paper_day:   int  = 1,
    sender_name: str  = "",
    key_metrics: Optional[dict] = None,
) -> bool:
    """
    Send (or buffer) the daily HTML report email via Gmail.

    Non-urgent emails are held in the daily digest buffer and sent
    as one consolidated email at 6 PM EDT.  Urgent emails (dead-man's
    switch, CRITICAL, circuit breakers) bypass the buffer and send
    immediately via send_alert_email().

    Args:
        html_body:  Full HTML string (from report_generator.build_html_report)
        subject:    Email subject line (auto-generated if None)
        paper_day:  Day number in the 30-day paper trading run

    Returns:
        True if buffered or sent successfully, False otherwise.
    """
    global _irm_review_active

    if subject is None:
        today   = datetime.now().strftime("%b %d, %Y")
        subject = f"📈 Institutional Tracker | Day {paper_day}/30 | {today}"

    # ── Route through email buffer ────────────────────────────────────────────
    # Urgent emails send immediately; everything else goes to the digest.
    try:
        from email_buffer import buffer_email, _is_urgent
        if _is_urgent(subject, sender_name):
            # Send immediately as a priority alert
            success = _send_raw(html_body, subject)
            if success:
                logger.info(f"✅ Urgent email sent immediately: {subject[:60]}")
            return success
        else:
            buffered = buffer_email(subject, html_body, sender_name, key_metrics)
            if buffered:
                logger.info(f"✅ Email buffered for daily digest: {subject[:60]}")
                # Still run IRM review even for buffered emails
                if not _irm_review_active and sender_name:
                    _irm_review_active = True
                    try:
                        from irm_review_agent import trigger_irm_review, IRM_REVIEW_EXCLUDED
                        if sender_name.lower() not in IRM_REVIEW_EXCLUDED:
                            trigger_irm_review(subject, sender_name, key_metrics)
                    except Exception as e:
                        logger.debug(f"IRM review hook error (non-fatal): {e}")
                    finally:
                        _irm_review_active = False
                return True
    except Exception as e:
        logger.debug(f"Email buffer error (falling back to direct send): {e}")

    # ── Fallback: send directly if buffer unavailable ─────────────────────────
    success = _send_raw(html_body, subject)

    if success:
        logger.info(f"✅ Daily report emailed to {EMAIL_RECIPIENT}")
        if not _irm_review_active and sender_name:
            _irm_review_active = True
            try:
                from irm_review_agent import trigger_irm_review, IRM_REVIEW_EXCLUDED
                if sender_name.lower() not in IRM_REVIEW_EXCLUDED:
                    trigger_irm_review(subject, sender_name, key_metrics)
            except Exception as e:
                logger.debug(f"IRM review hook error (non-fatal): {e}")
            finally:
                _irm_review_active = False
    else:
        logger.error("❌ Email send failed — check credentials in config.py")

    return success


def send_alert_email(subject: str, body: str) -> bool:
    """
    Send a priority alert email to the Chairman.
    Used by the dead-man's switch and any other urgent pipeline alerts.
    Bypasses the IRM review hook — alerts are not reviewed, they ARE the signal.

    Args:
        subject: Email subject line (should include severity indicator)
        body:    HTML body string
    Returns:
        True if sent successfully, False otherwise.
    """
    wrapped = f"""
    <html>
    <body style="background:#0d1117;color:#c9d1d9;font-family:Arial,sans-serif;padding:24px;">
      <div style="border-left:4px solid #f85149;padding-left:16px;margin-bottom:20px;">
        <h2 style="color:#f85149;margin:0 0 8px;">⚠️ APEX CAPITAL — PRIORITY ALERT</h2>
        <p style="color:#8b949e;margin:0;font-size:13px;">{datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}</p>
      </div>
      {body}
      <hr style="border-color:#30363d;margin-top:30px;">
      <p style="color:#6e7681;font-size:11px;">Apex Capital automated alert system · This message bypasses normal IRM review.</p>
    </body>
    </html>
    """
    success = _send_raw(wrapped, subject)
    if success:
        logger.info(f"✅ Alert email sent: {subject[:60]}")
    else:
        logger.error(f"❌ Alert email failed: {subject[:60]}")
    return success


def send_test_email() -> bool:
    """Send a quick test email to verify credentials are working."""
    html = """
    <html><body style="background:#0d1117;color:#c9d1d9;font-family:Arial;padding:20px;">
    <h2 style="color:#58a6ff;">✅ Institutional Tracker — Test Email</h2>
    <p>If you're reading this, your Gmail App Password is configured correctly!</p>
    <p>The system is ready to send daily trading reports.</p>
    <p style="color:#8b949e;font-size:12px;">Sent at: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """</p>
    </body></html>
    """
    return send_daily_report(
        html_body=html,
        subject="✅ Institutional Tracker — Setup Confirmation",
        paper_day=0,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Sending test email...")
    success = send_test_email()
    if success:
        print("✅ Test email sent! Check your inbox.")
    else:
        print("❌ Failed. Check your credentials in config.py")
