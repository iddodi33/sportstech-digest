"""
email_client.py
Resend send wrapper for all sportstech-digest pipelines. Replaces SendGrid.
Raises on any non-2xx response so a failed send fails the run instead of
passing silently.
"""

import logging
import os

import requests

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


def _split(addrs):
    if not addrs:
        return []
    return [a.strip() for a in addrs.split(",") if a.strip()]


def send_email(subject, html_body, cc=None, attachments=None):
    """Send an HTML email via Resend.

    Reads RESEND_API_KEY, ALERT_FROM, ALERT_TO and optional cc from env.
    attachments: list of {"filename": str, "content": <base64 str>}.
    Returns the HTTP status code on success. Raises RuntimeError on missing
    config, or requests.HTTPError on a non-2xx response so CI fails loudly.
    """
    api_key = os.getenv("RESEND_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to = os.getenv("ALERT_TO")

    if not api_key or not alert_from or not alert_to:
        raise RuntimeError("RESEND_API_KEY, ALERT_FROM, or ALERT_TO not set")

    payload = {
        "from": alert_from,
        "to": _split(alert_to),
        "subject": subject,
        "html": html_body,
    }
    cc_list = _split(cc)
    if cc_list:
        payload["cc"] = cc_list
    if attachments:
        payload["attachments"] = attachments

    resp = requests.post(
        RESEND_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    log.info("Resend status=%s subject='%s'", resp.status_code, subject[:80])
    if resp.status_code >= 400:
        log.error("Resend error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.status_code
