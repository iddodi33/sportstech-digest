"""run_breezy.py — scrape all active Breezy sources end-to-end."""

import logging
import os
import sys
from datetime import datetime, timezone

# Support running as: python jobs_pipeline/run_breezy.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.supabase_jobs_client import get_active_sources
from jobs_pipeline.adapters.breezy import BreezyAdapter

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)

PLATFORM = "breezy"


def main():
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Breezy scrape starting at {start}")

    sources = get_active_sources(PLATFORM)
    print(f"Found {len(sources)} active Breezy sources")

    if not sources:
        print("No sources found — exiting.")
        return

    print()
    adapter = BreezyAdapter()
    all_stats = []

    for i, source in enumerate(sources, 1):
        company_name = source.get("company_name", "Unknown")
        endpoint = source.get("ats_api_endpoint", "")
        print(f"[{i}/{len(sources)}] {company_name} - {endpoint}")

        stats = adapter.run(source)
        all_stats.append(stats)

        found = stats["jobs_found"]
        inserted = stats["inserted"]
        updated = stats["updated"]
        reactivated = stats["reactivated"]
        errors = stats["errors"]

        parts = [f"{found} jobs found", f"{inserted} inserted", f"{updated} updated"]
        if reactivated:
            parts.append(f"{reactivated} reactivated")
        parts.append(f"{errors} errors")
        print(f"  {', '.join(parts)}")
        print()

    total_found = sum(s["jobs_found"] for s in all_stats)
    total_inserted = sum(s["inserted"] for s in all_stats)
    total_updated = sum(s["updated"] for s in all_stats)
    total_reactivated = sum(s["reactivated"] for s in all_stats)
    total_errors = sum(s["errors"] for s in all_stats)

    print("=" * 60)
    print(f"Scrape complete. {len(sources)} sources processed.")
    print(
        f"Total jobs: {total_found} found, {total_inserted} inserted, "
        f"{total_updated} updated, {total_reactivated} reactivated, {total_errors} errors"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
