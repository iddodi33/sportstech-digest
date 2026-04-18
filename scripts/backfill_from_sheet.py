"""
scripts/backfill_from_sheet.py

One-off backfill: imports historical curated articles from a Google Sheet CSV
into the hub's Supabase news_items table, enriching each row with Claude.

Usage:
    python scripts/backfill_from_sheet.py --dry-run   # first 5 rows, no DB write
    python scripts/backfill_from_sheet.py             # all rows, writes to Supabase
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow imports from the repo root (supabase_client, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
from dotenv import load_dotenv

from supabase_client import extract_publisher, fetch_og_metadata, upsert_news_item

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CSV_PATH     = Path(__file__).parent / "data" / "google_sheet_backfill.csv"
MODEL        = "claude-sonnet-4-5-20250929"
DEFAULT_SCORE = 4
DEFAULT_SCORE_REASON = "Historical backfill from curated newsletter list"

_ENRICHMENT_PROMPT = """\
You are enriching a curated Irish sportstech article for a newsletter database.

Given the article title and URL below, return a JSON object with these fields:
  "summary": <Exactly 2 sentences, 40-60 words total. Sentence 1: what happened, who did it, where (include Irish angle if present). Sentence 2: why it matters, what it enables, or what context helps the reader understand significance. Never just restate the headline. Factual, Irish-ecosystem-builder voice, no hype, never starts with "Exciting news" or "Delighted".
  BAD (too short, just restates headline): "Output Sports launches HYROX365 Athlete Readiness Test."
  GOOD (gives context and why-it-matters): "Dublin-based Output Sports has partnered with HYROX365 to launch a standardised Athlete Readiness Test using its sensor platform to measure strength, endurance, and recovery benchmarks. The partnership extends Output's reach into mass-participation fitness testing across the global HYROX network.">
  "tags": <list of 3-5 keyword strings: company names, themes, event types>
  "verticals": <list of 1-2 from: Performance Analytics | Wearables & Hardware | Fan Engagement | Media & Broadcasting | Health, Fitness and Wellbeing | Scouting & Recruitment | Esports & Gaming | Betting & Fantasy | Stadium & Event Tech | Club Management Software | Sports Education & Coaching | Other / Emerging>
  "mentioned_companies": <list of company names actually mentioned in the article title>

Return ONLY the JSON object, no markdown, no explanation.

Article title: {title}
Article URL: {url}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str:
    """Parse a date string to ISO 8601. Returns now() as fallback."""
    if not raw or not raw.strip():
        return datetime.now(timezone.utc).isoformat()
    raw = raw.strip()
    for fmt in (
        "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y",
        "%d-%m-%Y", "%B %d, %Y", "%b %d, %Y",
        "%d %B %Y", "%d %b %Y",
    ):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass
    # 2-digit year formats: assume 20XX
    for fmt in ("%d/%m/%y", "%m/%d/%y", "%d-%m-%y", "%y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year < 100:
                dt = dt.replace(year=2000 + dt.year)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    log.warning("Could not parse date %r — using now()", raw)
    return datetime.now(timezone.utc).isoformat()


def _enrich_with_claude(client: anthropic.Anthropic, title: str, url: str) -> dict | None:
    """Call Claude to generate summary, tags, verticals, mentioned_companies."""
    prompt = _ENRICHMENT_PROMPT.format(title=title, url=url)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Claude JSON parse failed for %r: %s — raw: %s", title[:60], exc, raw[:200])
        return None
    except Exception as exc:
        log.error("Claude API call failed for %r: %s", title[:60], exc)
        return None


def _print_dry_run_row(n: int, total: int, item: dict) -> None:
    print(f"\n{'='*70}")
    print(f"[{n}/{total}] DRY RUN — would upsert:")
    for k, v in item.items():
        val = repr(v) if not isinstance(v, str) or len(v) < 100 else repr(v[:97] + "...")
        print(f"  {k:<22} {val}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool) -> None:
    if not CSV_PATH.exists():
        log.error("CSV not found at %s — place the export there and retry.", CSV_PATH)
        sys.exit(1)

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key:
        log.error("ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=anthropic_key)

    # --- read CSV ---
    with open(CSV_PATH, encoding="utf-8-sig") as f:  # utf-8-sig strips Excel BOM
        reader = csv.DictReader(f)
        rows = list(reader)

    total_rows  = len(rows)
    limit       = 5 if dry_run else total_rows
    rows        = rows[:limit]

    log.info("Loaded %d rows from CSV (processing %d)%s",
             total_rows, len(rows), " [DRY RUN]" if dry_run else "")

    skipped      = []
    failed_urls  = []
    upserted     = 0
    enriched     = 0

    for n, row in enumerate(rows, start=1):
        raw_url = (row.get("Link") or "").strip()
        title   = (row.get("Title") or "").strip()
        date    = (row.get("Date Found") or "").strip()

        # Multi-URL cells: take only the first URL, log a warning for spot-check
        url_parts = raw_url.split()
        url = url_parts[0] if url_parts else ""
        if len(url_parts) > 1:
            log.warning("[%d/%d] Multi-URL cell — using first URL only: %s", n, len(rows), url[:80])

        # Normalise title: collapse newlines/tabs/multiple spaces into a single space
        title = re.sub(r"\s+", " ", title).strip()

        # --- validation ---
        if not url:
            skipped.append((n, title or "(no title)", "empty URL"))
            log.info("[%d/%d] SKIP — empty URL: %r", n, len(rows), title[:60])
            continue
        if not url.startswith("http"):
            skipped.append((n, title, f"URL doesn't start with http: {url[:60]}"))
            log.info("[%d/%d] SKIP — bad URL scheme: %s", n, len(rows), url[:60])
            continue
        if not title or "unknown" in title.lower():
            skipped.append((n, title, "empty or 'Unknown' title"))
            log.info("[%d/%d] SKIP — bad title: %r", n, len(rows), title[:60])
            continue

        print(f"[{n}/{len(rows)}] Processing: {title[:80]}")

        # --- Claude enrichment ---
        enrichment = _enrich_with_claude(client, title, url)
        if enrichment is None:
            failed_urls.append(url)
            log.error("[%d/%d] Claude failed — skipping row: %s", n, len(rows), url[:80])
            time.sleep(1)
            continue
        enriched += 1

        # --- OG metadata ---
        og = fetch_og_metadata(url)
        og_title = og.get("og_title") or ""
        image_url = og.get("image_url")

        # Use og_title as display title if it's meaningfully different and long enough
        display_title = (
            og_title
            if og_title and og_title != title and len(og_title) >= 15
            else title
        )

        # --- build item ---
        item = {
            "url":                 url,
            "title":               display_title,
            "original_title":      title,
            "source":              extract_publisher(url),
            "published_at":        _parse_date(date),
            "score":               DEFAULT_SCORE,
            "score_reason":        DEFAULT_SCORE_REASON,
            "summary":             enrichment.get("summary", ""),
            "tags":                enrichment.get("tags", []) or [],
            "verticals":           enrichment.get("verticals", []) or [],
            "mentioned_companies": enrichment.get("mentioned_companies", []) or [],
            "image_url":           image_url,
            "status":              "pending",
        }

        # --- dry run: print only ---
        if dry_run:
            _print_dry_run_row(n, len(rows), item)
            time.sleep(1)
            continue

        # --- upsert ---
        result = upsert_news_item(item)
        if result is not None:
            upserted += 1
            source = item["source"] or "?"
            print(f"[{n}/{len(rows)}] Upserted: {source} — {display_title[:70]}")
        else:
            failed_urls.append(url)
            log.error("[%d/%d] Supabase upsert failed: %s", n, len(rows), url[:80])

        time.sleep(1)

    # --- summary ---
    print(f"\n{'='*70}")
    if dry_run:
        print(f"DRY RUN complete — {len(rows)} rows previewed, nothing written to Supabase.")
    else:
        print(
            f"Total rows: {total_rows} | "
            f"Skipped: {len(skipped)} | "
            f"Enriched: {enriched} | "
            f"Upserted: {upserted} | "
            f"Failed: {len(failed_urls)}"
        )
    if skipped:
        print(f"\nSkipped ({len(skipped)}):")
        for row_n, t, reason in skipped:
            print(f"  [{row_n}] {t[:60]} — {reason}")
    if failed_urls:
        print(f"\nFailed URLs ({len(failed_urls)}):")
        for u in failed_urls:
            print(f"  {u}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical articles into Supabase.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Process first 5 rows and print output; do NOT write to Supabase.",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
