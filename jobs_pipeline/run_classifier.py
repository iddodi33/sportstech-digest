"""run_classifier.py — classify all pending unclassified jobs in the hub DB.

Flow per job:
  1. Rules-based pre-filters (junior keywords, FDI geography)
  2. If passes rules → Haiku classification
  3. If Haiku returns not_sportstech → reject
  4. Otherwise → update job with classification and leave as pending for admin review

Run after scrapers, before the archive sweep.
"""

import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone

# Support: python jobs_pipeline/run_classifier.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import anthropic

from jobs_pipeline.classifier import (
    MODEL,
    build_classification_record,
    classify_with_haiku,
    normalise_haiku_fields,
    run_rules,
)
from jobs_pipeline.supabase_jobs_client import get_client

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

HAIKU_SLEEP = 0.5     # seconds between Haiku calls
LOG_EVERY = 10        # print progress every N jobs
BATCH_LOG_EVERY = 50  # print batch header every N jobs


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


def _update_job(client, job_id: str, update: dict) -> None:
    try:
        client.table("jobs").update(update).eq("id", job_id).execute()
    except Exception as exc:
        log.error("Supabase update failed for job %s: %s", job_id, exc)


def main():
    client = get_client()
    if client is None:
        print("ERROR: Could not connect to Supabase — check env vars.")
        return

    ai = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    start_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Classifier starting at {start_ts}")

    # ── Fetch all pending, unclassified jobs ──────────────────────────────────
    print("Fetching pending unclassified jobs...")
    jobs = (
        client.table("jobs")
        .select("*")
        .eq("status", "pending")
        .is_("classification", "null")
        .execute()
    ).data
    total = len(jobs)
    print(f"Found {total} jobs to classify")

    if not total:
        print("Nothing to do.")
        return

    # ── Pre-fetch company data ────────────────────────────────────────────────
    company_ids = list({j["company_id"] for j in jobs if j.get("company_id")})
    companies = _fetch_companies(client, company_ids)
    print(f"Loaded {len(companies)} company records")
    print()

    # ── Process jobs ──────────────────────────────────────────────────────────
    stats = Counter()

    for idx, job in enumerate(jobs):
        company = companies.get(job.get("company_id") or "") or {}

        # Batch header
        if idx % BATCH_LOG_EVERY == 0:
            print(f"--- Batch {idx // BATCH_LOG_EVERY + 1} --- jobs {idx + 1}-{min(idx + BATCH_LOG_EVERY, total)} of {total}")

        # Per-job progress
        if idx % LOG_EVERY == 0:
            print(
                f"  [{idx + 1:4d}/{total}] {job.get('company_name', '?')[:18]:18s} "
                f"| {(job.get('title') or '?')[:50]}"
            )

        # ── Rules pass ───────────────────────────────────────────────────────
        rules_result = run_rules(job, company)

        if rules_result["rejected"]:
            reason = rules_result["reason"]
            _update_job(client, job["id"], {
                "status": "rejected",
                "rejected_reason": reason,
                "classification": build_classification_record(None, rules_result),
            })
            stats[f"rejected_{reason}"] += 1
            continue

        # ── Haiku classification ──────────────────────────────────────────────
        try:
            haiku_raw = classify_with_haiku(job, company, ai)
        except Exception as exc:
            log.error(
                "Haiku error for job %s '%s': %s",
                job["id"], (job.get("title") or "")[:60], exc,
            )
            stats["haiku_errors"] += 1
            time.sleep(HAIKU_SLEEP)
            continue

        # Normalise enum fields to values the DB check constraints accept
        haiku = normalise_haiku_fields(haiku_raw)

        # Update rules dict with Haiku's sportstech verdict
        is_not_sportstech = haiku_raw.get("sportstech_relevance") == "not_sportstech"
        rules_result["rules"]["not_sportstech_reject"] = is_not_sportstech

        # Store raw Haiku output in classification JSON (pre-normalisation)
        classification = build_classification_record(haiku_raw, rules_result)

        shared_fields = {
            "classification": classification,
            "seniority": haiku.get("seniority"),
            "employment_type": haiku.get("employment_type"),
            "remote_status": haiku.get("remote_status"),
            "vertical": haiku.get("vertical"),
            "location_normalised": haiku.get("location_normalised"),
            "job_function": haiku.get("job_function"),
        }

        if is_not_sportstech:
            _update_job(client, job["id"], {
                **shared_fields,
                "status": "rejected",
                "rejected_reason": "not_sportstech",
            })
            stats["rejected_not_sportstech"] += 1
        else:
            _update_job(client, job["id"], {
                **shared_fields,
                "status": "pending",
            })
            stats["passed"] += 1

        time.sleep(HAIKU_SLEEP)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"Classification complete. {total} jobs processed.")
    print(f"  Passed (pending, ready for admin review): {stats['passed']}")
    print(f"  Rejected - too_junior:                    {stats['rejected_too_junior']}")
    print(f"  Rejected - fdi_geography:                 {stats['rejected_fdi_geography']}")
    print(f"  Rejected - not_sportstech:                {stats['rejected_not_sportstech']}")
    print(f"  Haiku errors (skipped, retry later):      {stats['haiku_errors']}")
    print("=" * 60)

    # ── Distribution query ────────────────────────────────────────────────────
    print()
    print("Running distribution query...")
    dist_rows = (
        client.table("jobs")
        .select("status, rejected_reason")
        .not_.is_("classification", "null")
        .execute()
    ).data

    counts = Counter((r["status"], r.get("rejected_reason")) for r in dist_rows)
    print()
    print(f"{'status':<16}  {'rejected_reason':<28}  {'n':>5}")
    print("-" * 56)
    for (status, reason), n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {status:<14}  {(reason or '—'):<28}  {n:>5}")


if __name__ == "__main__":
    main()
