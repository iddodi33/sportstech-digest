"""test_extractor.py — CLI test harness for the events extractor.

By default: read-only. Fetches the URL, runs extraction, prints results.
With --upsert: writes relevant events to the hub Supabase events table.

Usage:
  python events_pipeline/test_extractor.py <url>
  python events_pipeline/test_extractor.py <url> --upsert
"""

import argparse
import json
import logging
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

_REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "NEXT_PUBLIC_SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
]


def _check_env() -> bool:
    missing = [v for v in _REQUIRED_ENV if not os.getenv(v)]
    for v in missing:
        log.error("Missing required env var: %s", v)
    return not missing


def _validate_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract structured event data from a URL using Claude Sonnet.\n"
            "By default this is read-only. Pass --upsert to write to the hub DB."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?", help="Event page URL to extract (http/https)")
    parser.add_argument(
        "--upsert",
        action="store_true",
        help=(
            "Write the extracted event to Supabase (source='test'). "
            "Only acts when relevance_category != 'not_relevant'."
        ),
    )
    args = parser.parse_args()

    if not args.url:
        parser.print_help()
        sys.exit(0)

    if not _check_env():
        sys.exit(1)

    if not _validate_url(args.url):
        log.error("Invalid URL %r — must start with http:// or https://", args.url)
        sys.exit(1)

    # ── Extract ────────────────────────────────────────────────────────────────
    from events_pipeline.extractor import extract_event, ExtractorError

    log.info("Starting extraction for: %s", args.url)
    try:
        extraction = extract_event(args.url)
    except ExtractorError as exc:
        log.error("Extraction failed: %s", exc)
        sys.exit(1)

    # ── Print full result ──────────────────────────────────────────────────────
    print()
    print(json.dumps(extraction, indent=2, ensure_ascii=False))
    print()

    # ── Summary ────────────────────────────────────────────────────────────────
    category   = extraction.get("relevance_category", "unknown")
    confidence = extraction.get("extraction_confidence", "unknown")
    name       = extraction.get("name") or "—"
    date_val   = extraction.get("date") or "—"
    reason     = extraction.get("relevance_reason") or ""

    print("─" * 60)
    print(f"  Category  : {category}")
    print(f"  Confidence: {confidence}")
    print(f"  Name      : {name}")
    print(f"  Date      : {date_val}")
    if reason:
        print(f"  Reason    : {reason}")
    print("─" * 60)

    # ── Optional upsert ────────────────────────────────────────────────────────
    if args.upsert:
        if category == "not_relevant":
            log.info("Skipping upsert — relevance_category is 'not_relevant'")
        else:
            from events_pipeline.supabase_events_client import upsert_event
            log.info("Upserting event to hub Supabase (source='test')...")
            event_id, was_inserted = upsert_event(extraction, source="test")
            if event_id:
                action = "INSERTED" if was_inserted else "UPDATED (already exists)"
                print(f"\n  Upsert: {action}")
                print(f"  Event ID: {event_id}")
            else:
                log.error("Upsert failed — check logs above")
                sys.exit(1)
    else:
        if category != "not_relevant":
            print("\n  (Run with --upsert to write this event to the hub DB)")


if __name__ == "__main__":
    main()
