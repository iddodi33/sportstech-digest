"""base.py — abstract base class for all ATS platform adapters."""

import logging
from abc import ABC, abstractmethod

from ..supabase_jobs_client import upsert_job

log = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """Abstract base for all ATS scraper adapters.

    Subclass must set `platform` (class attribute) and implement `fetch()`.
    `run()` orchestrates fetch → validate → upsert → stats.
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
        source_name = source.get("company_name") or source.get("id", "unknown")
        stats = {
            "source_name": source_name,
            "jobs_found": 0,
            "inserted": 0,
            "updated": 0,
            "reactivated": 0,
            "errors": 0,
        }

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
                elif result.get("was_inserted"):
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
