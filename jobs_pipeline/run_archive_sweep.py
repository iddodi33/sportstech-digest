"""run_archive_sweep.py — archive jobs absent from 2+ consecutive weekly scrape runs.

A job is a candidate for archival when:
  1. Its source scraped successfully within the past 8 days (health gate).
  2. Its last_seen_in_scrape_run is more than 8 days before the source's
     last_successful_scrape_at (the "2 weekly runs + 1-day drift buffer" rule).

Jobs where last_seen_in_scrape_run IS NULL (legacy rows predating this workstream)
are granted one full cycle of grace and are never archived on the first sweep.

Usage:
  python jobs_pipeline/run_archive_sweep.py            # live — archives candidates
  python jobs_pipeline/run_archive_sweep.py --dry-run  # log only, no writes
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.supabase_jobs_client import get_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

_GRACE_DAYS = 8         # covers 2 × 7-day weekly runs with 1-day cron-drift buffer
_HEALTH_DAYS = 8        # source must have scraped successfully within this window


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def main(dry_run: bool = False) -> None:
    mode = "DRY RUN" if dry_run else "live"
    log.info("Archive sweep starting [%s]", mode)

    client = get_client()
    if client is None:
        log.error("Supabase client unavailable — cannot run archive sweep")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    health_threshold = now - timedelta(days=_HEALTH_DAYS)

    # ── 1. Load sources that have at least one recorded successful scrape ──────
    try:
        result = (
            client.table("company_careers_sources")
            .select("id, last_successful_scrape_at, last_scrape_run_at")
            .not_.is_("last_successful_scrape_at", "null")
            .execute()
        )
        all_sources = result.data
    except Exception as exc:
        log.error("Failed to fetch sources: %s", exc)
        sys.exit(1)

    sources_by_id: dict[str, dict] = {row["id"]: row for row in all_sources}
    healthy_source_ids: set[str] = set()
    for sid, src in sources_by_id.items():
        last_success = _parse_ts(src["last_successful_scrape_at"])
        if last_success and last_success >= health_threshold:
            healthy_source_ids.add(sid)

    log.info(
        "Sources: %d with scrape history, %d healthy (last success within %d days)",
        len(sources_by_id), len(healthy_source_ids), _HEALTH_DAYS,
    )

    # ── 2. Load approved/pending jobs that have a source_id ───────────────────
    try:
        result = (
            client.table("jobs")
            .select("id, title, company_name, sources_source_id, last_seen_in_scrape_run, status")
            .in_("status", ["approved", "pending"])
            .not_.is_("sources_source_id", "null")
            .execute()
        )
        all_jobs = result.data
    except Exception as exc:
        log.error("Failed to fetch jobs: %s", exc)
        sys.exit(1)

    log.info("Jobs to evaluate: %d (approved/pending, source_id set)", len(all_jobs))

    # ── 3. Classify each job ──────────────────────────────────────────────────
    candidates: list[dict] = []
    skipped_no_history = 0   # source has no last_successful_scrape_at (excluded above query, but guard for orphans)
    skipped_unhealthy = 0    # source health gate failed
    skipped_not_stale = 0    # job seen recently enough, no action needed

    for job in all_jobs:
        source_id = job["sources_source_id"]
        source = sources_by_id.get(source_id)

        if source is None:
            skipped_no_history += 1
            continue

        if source_id not in healthy_source_ids:
            skipped_unhealthy += 1
            continue

        last_success = _parse_ts(source["last_successful_scrape_at"])
        cutoff = last_success - timedelta(days=_GRACE_DAYS)

        last_seen_raw = job.get("last_seen_in_scrape_run")
        if last_seen_raw:
            effective_last_seen = _parse_ts(last_seen_raw)
        else:
            # NULL legacy row: treat as last_successful_scrape_at - 1 day → always > cutoff
            # so the job is always exempt until it gets a real timestamp.
            effective_last_seen = last_success - timedelta(days=1)

        if effective_last_seen < cutoff:
            job["_last_success"] = last_success
            job["_effective_last_seen"] = effective_last_seen
            candidates.append(job)
        else:
            skipped_not_stale += 1

    log.info("Candidates for archival: %d", len(candidates))
    log.info("Skipped — no source scrape history: %d", skipped_no_history)
    log.info("Skipped — source health gate (not scraped within %d days): %d", _HEALTH_DAYS, skipped_unhealthy)
    log.info("Skipped — not yet stale: %d", skipped_not_stale)

    # ── 4. Archive candidates (or dry-run log) ────────────────────────────────
    archived_count = 0
    failed_count = 0
    by_source: dict[str, int] = defaultdict(int)

    for job in candidates:
        company_name = job.get("company_name") or "unknown"
        title = job.get("title") or "untitled"
        job_id = job["id"]
        last_seen_display = job["_effective_last_seen"].strftime("%Y-%m-%d")
        last_success_display = job["_last_success"].strftime("%Y-%m-%d")

        if dry_run:
            log.info(
                "WOULD ARCHIVE: %s %s — %s (last seen: %s, source last success: %s)",
                job_id, company_name, title, last_seen_display, last_success_display,
            )
            archived_count += 1
            by_source[company_name] += 1
        else:
            try:
                client.table("jobs").update({
                    "status": "archived",
                    "archived_at": now.isoformat(),
                }).eq("id", job_id).execute()
                log.info("ARCHIVED: %s %s — %s", job_id, company_name, title)
                archived_count += 1
                by_source[company_name] += 1
            except Exception as exc:
                log.error("Failed to archive job %s: %s", job_id, exc)
                failed_count += 1

    # ── 5. Summary ────────────────────────────────────────────────────────────
    verb = "Would archive" if dry_run else "Archived"
    log.info("=== Archive Sweep Complete ===")
    log.info("Total jobs checked: %d", len(all_jobs))
    log.info("%s: %d", verb, archived_count)
    if not dry_run and failed_count:
        log.error("Failed to archive: %d", failed_count)
    log.info("Skipped (no source history): %d", skipped_no_history)
    log.info("Skipped (source health gate): %d", skipped_unhealthy)
    log.info("Skipped (not stale): %d", skipped_not_stale)

    if by_source:
        log.info("Breakdown by source (%s):", verb.lower())
        for name, count in sorted(by_source.items(), key=lambda x: -x[1]):
            log.info("  %-40s %d", name, count)

    if failed_count:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Archive stale jobs absent from 2+ consecutive weekly scrape runs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be archived without making any changes",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
