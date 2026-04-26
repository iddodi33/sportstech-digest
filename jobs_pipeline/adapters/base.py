"""base.py — abstract base class for all ATS platform adapters."""

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from ..supabase_jobs_client import (
    mark_job_seen,
    mark_source_attempted,
    mark_source_successful,
    upsert_job,
)

log = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """Abstract base for all ATS scraper adapters.

    Subclass must set `platform` (class attribute) and implement `fetch()`.
    `run()` orchestrates fetch → validate → upsert → stats → source tracking.
    """

    platform: str = ""

    @abstractmethod
    def fetch(self, source: dict) -> list[dict]:
        """Fetch jobs from the ATS API for a single source.

        Returns a list of normalised job dicts:
        {
            'url': str,               # absolute HTTPS URL, required
            'title': str,             # stripped job title, required
            'location_raw': str | None,
            'summary': str | None,    # plain text, truncated to 2000 chars
            'salary_range': str | None,
        }

        Raise on unrecoverable HTTP errors so run() can catch and log them.
        Return [] for genuinely empty boards.
        """

    def run(self, source: dict) -> dict:
        """Orchestrate fetch + validate + upsert for one source.

        Records run_started_at at entry. After completion, stamps
        last_scrape_run_at on the source row (always) and
        last_successful_scrape_at when at least one job was upserted.
        Each successful upsert also stamps last_seen_in_scrape_run on the job row.

        Returns stats dict:
        {
            'source_name': str,
            'jobs_found': int,
            'inserted': int,
            'updated': int,
            'reactivated': int,
            'errors': int,
        }
        """
        run_started_at = datetime.now(timezone.utc)
        source_name = source.get("company_name") or source.get("id", "unknown")
        source_id = source.get("id")
        stats = {
            "source_name": source_name,
            "jobs_found": 0,
            "inserted": 0,
            "updated": 0,
            "reactivated": 0,
            "errors": 0,
        }
        upserted_count = 0

        try:
            try:
                jobs = self.fetch(source)
            except Exception as exc:
                log.error("[%s] fetch() failed: %s", source_name, exc)
                stats["errors"] += 1
                return stats

            stats["jobs_found"] = len(jobs)

            for job in jobs:
                url = (job.get("url") or "").strip()
                title = (job.get("title") or "").strip()

                if not url or not title:
                    log.warning(
                        "[%s] Skipping job with missing url or title: %r",
                        source_name, job,
                    )
                    stats["errors"] += 1
                    continue

                try:
                    result = upsert_job(
                        url=url,
                        title=title,
                        source=self.platform,
                        sources_source_id=source["id"],
                        company_id=source["company_id"],
                        company_name=source.get("company_name", ""),
                        location_raw=job.get("location_raw"),
                        summary=job.get("summary"),
                        salary_range=job.get("salary_range"),
                    )
                    if not result:
                        # upsert_job returns {} on RPC failure (already logged)
                        stats["errors"] += 1
                    else:
                        upserted_count += 1
                        job_id = result.get("id")
                        if job_id:
                            mark_job_seen(job_id, run_started_at)
                        if result.get("was_inserted"):
                            stats["inserted"] += 1
                        elif result.get("was_reactivated"):
                            stats["reactivated"] += 1
                        else:
                            stats["updated"] += 1
                except Exception as exc:
                    log.error(
                        "[%s] upsert_job failed for '%s': %s",
                        source_name, url[:80], exc,
                    )
                    stats["errors"] += 1

            return stats

        finally:
            if source_id:
                if upserted_count > 0:
                    mark_source_successful(source_id, run_started_at)
                else:
                    mark_source_attempted(source_id, run_started_at)
