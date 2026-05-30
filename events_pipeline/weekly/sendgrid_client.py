"""sendgrid_client.py — Resend send wrapper for the weekly events summary email."""

import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from email_client import send_email as _resend_send

log = logging.getLogger(__name__)


def send_email(subject: str, html_body: str) -> bool:
    """Send html_body to ALERT_TO via Resend. Returns True on success. Raises on failure."""
    _resend_send(subject, html_body)
    return True
