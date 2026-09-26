"""
email_client.py
Resend send wrapper for all sportstech-digest pipelines. Replaces SendGrid.
Raises on any non-2xx response so a failed send fails the run instead of
passing silently.

Resend allows 10 requests/sec per account. Sends are spaced by
MIN_SEND_INTERVAL_S, and a 429 is retried (honouring retry-after /
ratelimit-reset when present, else exponential backoff) before raising.
The 2026-09-26 daily run crashed on the 11th email of a tight loop without this.
"""

import logging
import os
import time

import requests

log = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

MIN_SEND_INTERVAL_S = 0.15   # ~6.7 req/s — safely under Resend's 10/s limit
MAX_429_RETRIES     = 3      # retries after the first attempt, so 4 attempts total
BACKOFF_BASE_S      = 1.0    # 1s, 2s, 4s when Resend gives no header to go on
MAX_BACKOFF_S       = 30.0   # never trust a header into a multi-minute stall

_last_send_at = 0.0  # time.monotonic() of the previous request, shared across callers


def _throttle():
    global _last_send_at
    wait = MIN_SEND_INTERVAL_S - (time.monotonic() - _last_send_at)
    if wait > 0:
        time.sleep(wait)
    _last_send_at = time.monotonic()


def _retry_wait(resp, attempt):
    """Seconds to wait before retrying a 429: header if usable, else exponential."""
    for header in ("retry-after", "ratelimit-reset"):
        value = resp.headers.get(header)
        if value is None:
            continue
        try:
            return min(max(float(value), MIN_SEND_INTERVAL_S), MAX_BACKOFF_S)
        except ValueError:
            continue  # e.g. an HTTP-date retry-after; fall through to backoff
    return min(BACKOFF_BASE_S * (2 ** attempt), MAX_BACKOFF_S)


def _split(addrs):
    if not addrs:
        return []
    return [a.strip() for a in addrs.split(",") if a.strip()]


def send_email(subject, html_body, cc=None, attachments=None):
    """Send an HTML email via Resend.

    Reads RESEND_API_KEY, ALERT_FROM, ALERT_TO and optional cc from env.
    attachments: list of {"filename": str, "content": <base64 str>}.
    Returns the HTTP status code on success. Raises RuntimeError on missing
    config, or requests.HTTPError on a non-2xx response so CI fails loudly —
    for a 429, only once MAX_429_RETRIES retries are used up.
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

    for attempt in range(MAX_429_RETRIES + 1):
        _throttle()
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
        if resp.status_code == 429 and attempt < MAX_429_RETRIES:
            wait = _retry_wait(resp, attempt)
            log.warning(
                "Resend 429 rate limited — retry %d/%d in %.2fs",
                attempt + 1, MAX_429_RETRIES, wait,
            )
            time.sleep(wait)
            continue
        break

    if resp.status_code >= 400:
        log.error("Resend error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.status_code
