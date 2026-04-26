"""sendgrid_client.py — SendGrid send wrapper for the weekly summary email."""

import logging
import os

log = logging.getLogger(__name__)


def send_email(subject: str, html_body: str) -> bool:
    """Send html_body to ALERT_TO via SendGrid. Returns True on success."""
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
    except ImportError:
        log.error("sendgrid package not installed — run: pip install sendgrid")
        return False

    sg_key     = os.getenv("SENDGRID_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to   = os.getenv("ALERT_TO")

    if not sg_key or not alert_from or not alert_to:
        log.error("SENDGRID_API_KEY, ALERT_FROM, or ALERT_TO not set")
        return False

    message = Mail(
        from_email=alert_from,
        to_emails=alert_to,
        subject=subject,
        html_content=html_body,
    )

    try:
        sg = SendGridAPIClient(sg_key)
        response = sg.send(message)
        log.info("SendGrid: status=%s subject='%s'", response.status_code, subject[:80])
        if response.status_code >= 400:
            log.error(
                "SendGrid returned error status %s: %s",
                response.status_code, response.body,
            )
            return False
        return True
    except Exception as exc:
        log.error("SendGrid send failed: %s", exc)
        return False
