"""run_backfill_excerpts.py — one-off backfill of summary_excerpt for approved jobs.

Queries jobs WHERE status='approved' AND summary_excerpt IS NULL, calls Haiku 4.5
to extract a focused 2-3 sentence role excerpt, and writes it back to the jobs table.

Re-running is safe: the WHERE clause skips jobs already processed.

Usage:
    python jobs_pipeline/run_backfill_excerpts.py            # live run
    python jobs_pipeline/run_backfill_excerpts.py --dry-run  # preview only
"""

import argparse
import logging
import os
import sys
import time

# Support running from project root: python jobs_pipeline/run_backfill_excerpts.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import anthropic

from jobs_pipeline.classifier import MODEL
from jobs_pipeline.supabase_jobs_client import get_client

log = logging.getLogger(__name__)

SLEEP_BETWEEN_CALLS = 0.2   # seconds — gentle rate limit for Anthropic API
MIN_SUMMARY_CHARS   = 50    # skip jobs with trivially short summaries
COST_PER_JOB        = 0.0001  # rough $/call estimate for Haiku 4.5
LOG_EVERY           = 10

# Focused excerpt-only system prompt — matches the instruction in classifier.py.
_SYSTEM_PROMPT = (
    "You extract clean job description excerpts for SportsTech roles. "
    "Return a 2-3 sentence description of what THE ROLE involves — what the person "
    "will actually do day-to-day. Skip company introductions ('who we are', 'our mission'), "
    "administrative metadata (salary, contract length, location), and recruiter boilerplate. "
    "Lead with the actual work. "
    "If the description is too short or messy to extract a clean excerpt, return exactly: null\n"
    "Maximum 400 characters. Plain text only, no markdown, no labels, no surrounding quotes.\n"
    "Examples:\n"
    "  BAD: 'Clubforce has transformed the way sports clubs are managed since 2009...'\n"
    "  GOOD: 'Build and maintain backend services for the Clubforce platform. Work across "
    "membership management, payments, and club operations features. Collaborate with the "
    "product team to ship new functionality and own service reliability.'"
)


def _build_prompt(title: str, summary: str) -> str:
    return (
        f"JOB TITLE: {title}\n\n"
        f"DESCRIPTION:\n{summary[:1500]}"
    )


def _extract_excerpt(title: str, summary: str, ai_client) -> str | None:
    """Call Haiku and return the extracted excerpt (≤400 chars), or None."""
    msg = ai_client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_prompt(title, summary)}],
    )
    text = msg.content[0].text.strip()
    if not text or text.lower() == "null":
        return None
    return text[:400]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill summary_excerpt for approved jobs with null excerpts."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count eligible jobs and estimate cost without making any API calls or DB writes.",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if dry_run:
        print("DRY RUN — no API calls or DB writes will be made.\n")

    client = get_client()
    if client is None:
        print("ERROR: Could not connect to Supabase — check env vars.")
        return

    # Fetch eligible jobs
    print("Fetching approved jobs with no summary_excerpt...")
    jobs = (
        client.table("jobs")
        .select("id, title, summary, source")
        .eq("status", "approved")
        .is_("summary_excerpt", "null")
        .order("first_seen_at", desc=True)
        .execute()
    ).data

    total = len(jobs)
    if total == 0:
        print("No jobs need backfilling. Done.")
        return

    skippable = sum(1 for j in jobs if len(j.get("summary") or "") < MIN_SUMMARY_CHARS)
    processable = total - skippable
    estimated_cost = processable * COST_PER_JOB

    print(f"Found {total} jobs with no summary_excerpt.")
    print(f"  {skippable} will be skipped (summary absent or < {MIN_SUMMARY_CHARS} chars)")
    print(f"  {processable} will be processed")
    print(f"Estimated cost: ${estimated_cost:.2f} ({processable} jobs × ${COST_PER_JOB:.4f} per Haiku call)")
    print()

    if dry_run:
        print("Run without --dry-run to execute.")
        return

    ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    updated = 0
    skipped = 0
    failed  = 0

    for idx, job in enumerate(jobs):
        job_id  = job["id"]
        title   = job.get("title") or ""
        summary = job.get("summary") or ""

        # Progress line every LOG_EVERY jobs
        if (idx + 1) % LOG_EVERY == 0:
            print(f"  Processed {idx + 1}/{total}")

        # Skip jobs with no usable summary
        if len(summary) < MIN_SUMMARY_CHARS:
            skipped += 1
            continue

        # Extract excerpt via Haiku
        try:
            excerpt = _extract_excerpt(title, summary, ai)
        except Exception as exc:
            log.error("Haiku error for job %s '%s': %s", job_id, title[:60], exc)
            failed += 1
            time.sleep(SLEEP_BETWEEN_CALLS)
            continue

        # Write result — excerpt may be None if Haiku judged the summary too thin
        try:
            client.table("jobs").update(
                {"summary_excerpt": excerpt}
            ).eq("id", job_id).execute()
            updated += 1
        except Exception as exc:
            log.error("DB write failed for job %s: %s", job_id, exc)
            failed += 1

        time.sleep(SLEEP_BETWEEN_CALLS)

    print()
    print(f"Done. Updated {updated}. Skipped {skipped} (no summary). Failed {failed}.")


if __name__ == "__main__":
    main()
