"""send_newsletter_email.py, email the monthly newsletter source markdown via Resend.
Success case: markdown file exists, HTML body + raw markdown attachment.
Failure case: file missing (export crashed), plain-text failure notification with run URL.
"""
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
        import resend
    except ImportError:
        log.error("resend package not installed")
        return False

    resend_key = os.getenv("RESEND_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to = os.getenv("ALERT_TO")
    if not resend_key or not alert_from or not alert_to:
        log.error("RESEND_API_KEY, ALERT_FROM, or ALERT_TO not set")
        return False

    resend.api_key = resend_key

    params = {
        "from": alert_from,
        "to": [alert_to],
        "subject": subject,
        "html": html_body,
    }

    if attachment_path is not None and attachment_path.exists():
        raw = attachment_path.read_bytes()
        params["attachments"] = [
            {
                "filename": attachment_path.name,
                "content": list(raw),
            }
        ]

    try:
        response = resend.Emails.send(params)
        email_id = response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
        log.info("Resend: id=%s subject='%s'", email_id, subject[:80])
        return True
    except Exception as exc:
        log.error("Resend send failed: %s", exc)
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
        log.info("Markdown file found: %s, sending success email", md_path)
        md_text = md_path.read_text(encoding="utf-8")
        html_body = _md_to_html(md_text)
        subject = f"Newsletter source, {month_long}"
        ok = _send(subject, html_body, attachment_path=md_path)
    else:
        log.warning("Markdown file not found: %s, sending failure email", md_path)
        html_body = (
            f"<p>The newsletter source export failed for <strong>{month_long}</strong>. "
            f"No markdown file was written.</p>"
            f"<p><a href=\"{run_url}\">View the Actions run log</a></p>"
        )
        subject = f"Newsletter source FAILED, {month_long}"
        ok = _send(subject, html_body, attachment_path=None)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
