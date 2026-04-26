"""run_reclassify_all.py — one-off backfill: set job_function on all existing jobs.

Selects all jobs where job_function IS NULL (regardless of status — approved,
pending, rejected, archived are all included). For each job, calls Haiku to
classify job_function, then writes only that column back to Supabase.

Status, classification JSONB, and all other fields are left unchanged.
The rule-based pre-filter is skipped — we are retrofitting a new field, not
re-evaluating the original accept/reject decision.

Idempotent: re-running skips jobs that already have job_function set because
the initial SELECT filters on IS NULL.
"""

import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import anthropic

from jobs_pipeline.classifier import classify_with_haiku, normalise_haiku_fields
from jobs_pipeline.supabase_jobs_client import get_client

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

HAIKU_SLEEP = 0.5


def _fetch_companies(client, company_ids: list[str]) -> dict[str, dict]:
    companies = {}
    for i in range(0, len(company_ids), 50):
        batch = company_ids[i : i + 50]
        rows = (
            client.table("companies")
            .select("id, name, vertical, is_fdi, is_irish_founded, description")
            .in_("id", batch)
            .execute()
        ).data
        for c in rows:
            companies[c["id"]] = c
    return companies


def main():
    client = get_client()
    if client is None:
        print("ERROR: Could not connect to Supabase — check env vars.")
        sys.exit(1)

    ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    start_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Backfill starting at {start_ts}")

    # ── Fetch all jobs where job_function is unset ─────────────────────────────
    print("Fetching jobs where job_function IS NULL...")
    jobs = (
        client.table("jobs")
        .select("*")
        .is_("job_function", "null")
        .execute()
    ).data
    total = len(jobs)

    if not total:
        print("Nothing to do — all jobs already have job_function set.")
        return

    # Status breakdown for the confirmation prompt
    status_counts: Counter = Counter(j.get("status", "?") for j in jobs)
    print(f"\nFound {total} jobs without job_function:")
    for status, n in sorted(status_counts.items()):
        print(f"  {status:<10} {n}")

    answer = input(f"\nProcess {total} jobs? [y/N]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)
    print()

    # ── Pre-fetch company data ─────────────────────────────────────────────────
    company_ids = list({j["company_id"] for j in jobs if j.get("company_id")})
    companies = _fetch_companies(client, company_ids)
    print(f"Loaded {len(companies)} company records\n")

    # ── Process jobs ───────────────────────────────────────────────────────────
    stats: Counter = Counter()

    for idx, job in enumerate(jobs):
        company = companies.get(job.get("company_id") or "") or {}
        company_name = (job.get("company_name") or company.get("name") or "?")[:20]
        title = (job.get("title") or "?")[:48]
        status = job.get("status", "?")

        prefix = f"  [{idx + 1:4d}/{total}] {company_name:<20} | {title:<48} | [{status:<8}]"

        # ── Haiku call ────────────────────────────────────────────────────────
        try:
            haiku_raw = classify_with_haiku(job, company, ai)
            haiku = normalise_haiku_fields(haiku_raw)
            fn = haiku.get("job_function")
        except Exception as exc:
            log.error("Haiku error for job %s '%s': %s", job["id"], title, exc)
            print(f"{prefix} → failed (haiku error)")
            stats["failed"] += 1
            time.sleep(HAIKU_SLEEP)
            continue

        # ── Write only job_function ───────────────────────────────────────────
        try:
            client.table("jobs").update({"job_function": fn}).eq("id", job["id"]).execute()
        except Exception as exc:
            log.error("DB update failed for job %s: %s", job["id"], exc)
            print(f"{prefix} → failed (db error)")
            stats["failed"] += 1
            time.sleep(HAIKU_SLEEP)
            continue

        result_label = fn if fn is not None else "null"
        print(f"{prefix} → {result_label}")

        if fn is not None:
            stats["classified"] += 1
        else:
            stats["returned_null"] += 1

        time.sleep(HAIKU_SLEEP)

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"Backfill complete.")
    print(f"  Jobs processed:                            {total}")
    print(f"  Successfully classified (function set):    {stats['classified']}")
    print(f"  Returned null (genuinely ambiguous):       {stats['returned_null']}")
    print(f"  Failed Haiku or DB error (retry later):    {stats['failed']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
