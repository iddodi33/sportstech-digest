"""send_newsletter_email.py — email the monthly newsletter source markdown via SendGrid.

Success case: markdown file exists → HTML body + raw markdown attachment.
Failure case: file missing (export crashed) → plain-text failure notification with run URL.
"""

import base64
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _send(subject: str, html_body: str, attachment_path: Path | None = None) -> bool:
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Attachment,
            FileContent,
            FileName,
            FileType,
            Mail,
        )
    except ImportError:
        log.error("sendgrid package not installed")
        return False

    sg_key = os.getenv("SENDGRID_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to = os.getenv("ALERT_TO")

    if not sg_key or not alert_from or not alert_to:
        log.error("SENDGRID_API_KEY, ALERT_FROM, or ALERT_TO not set")
        return False

    message = Mail(
        from_email=alert_from,
        to_emails=alert_to,
        subject=subject,
        html_content=html_body,
    )

    if attachment_path is not None and attachment_path.exists():
        raw = attachment_path.read_bytes()
        encoded = base64.b64encode(raw).decode()
        attachment = Attachment(
            FileContent(encoded),
            FileName(attachment_path.name),
            FileType("text/markdown"),
        )
        message.attachment = attachment

    try:
        sg = SendGridAPIClient(sg_key)
        response = sg.send(message)
        log.info("SendGrid: status=%s subject='%s'", response.status_code, subject[:80])
        if response.status_code >= 400:
            log.error("SendGrid error %s: %s", response.status_code, response.body)
            return False
        return True
    except Exception as exc:
        log.error("SendGrid send failed: %s", exc)
        return False


def _md_to_html(md_text: str) -> str:
    try:
        import markdown as md_lib
        return md_lib.markdown(md_text, extensions=["extra"])
    except ImportError:
        # Fallback: wrap in <pre> so the content is still readable
        escaped = md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre>{escaped}</pre>"


def main():
    now = datetime.now(timezone.utc)
    month_str = now.strftime("%Y-%m")
    month_long = now.strftime("%B %Y")
    run_url = os.getenv("GITHUB_RUN_URL", "(run URL not set)")

    md_path = Path("newsletter") / f"{month_str}-newsletter-source.md"

    if md_path.exists():
        log.info("Markdown file found: %s — sending success email", md_path)
        md_text = md_path.read_text(encoding="utf-8")
        html_body = _md_to_html(md_text)
        subject = f"Newsletter source — {month_long}"
        ok = _send(subject, html_body, attachment_path=md_path)
    else:
        log.warning("Markdown file not found: %s — sending failure email", md_path)
        html_body = (
            f"<p>The newsletter source export failed for <strong>{month_long}</strong>. "
            f"No markdown file was written.</p>"
            f"<p><a href=\"{run_url}\">View the Actions run log</a></p>"
        )
        subject = f"Newsletter source FAILED — {month_long}"
        ok = _send(subject, html_body, attachment_path=None)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
