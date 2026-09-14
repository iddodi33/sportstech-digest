"""run_custom_html.py — scrape all active custom_html careers pages end-to-end.

Covers company_careers_sources rows where ats_platform='custom_html': companies
publishing roles on their own site rather than through an ATS.

Flags:
  --dry-run          Print what would be upserted instead of calling the RPC.
  --company NAME     Process only companies whose name matches NAME (substring).

Smoke-test example:
  python jobs_pipeline/run_custom_html.py --dry-run --company "TeamFeePay"
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

# Support running as: python jobs_pipeline/run_custom_html.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.supabase_jobs_client import get_active_sources
from jobs_pipeline.adapters.custom_html import CustomHTMLAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

PLATFORM = "custom_html"


def main(dry_run: bool = False, company_filter: str = "") -> None:
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "DRY RUN" if dry_run else "live"
    print(f"custom_html scrape starting at {start} [{mode}]")

    sources = get_active_sources(PLATFORM)

    if company_filter:
        sources = [
            s for s in sources
            if company_filter.lower() in (s.get("company_name") or "").lower()
        ]
        print(f"Filtered to {len(sources)} source(s) matching '{company_filter}'")

    print(f"Found {len(sources)} active custom_html sources")
    if not sources:
        print("No sources found - exiting.")
        return

    print()
    adapter = CustomHTMLAdapter()
    all_stats = []

    for i, source in enumerate(sources, 1):
        company_name = source.get("company_name", "Unknown")
        careers_url = source.get("careers_url", "")
        print(f"[{i}/{len(sources)}] {company_name} - {careers_url}")

        if dry_run:
            try:
                jobs = adapter.fetch(source)
            except Exception as exc:
                print(f"  ERROR {type(exc).__name__}: {exc}")
                print()
                continue
            print(f"  would upsert {len(jobs)} job(s)")
            for job in jobs:
                loc = job.get("location_raw") or "-"
                print(f"    - {job['title']}  [{loc}]  {job['url']}")
            print()
            continue

        stats = adapter.run(source)
        all_stats.append(stats)

        parts = [
            f"{stats['jobs_found']} jobs found",
            f"{stats['inserted']} inserted",
            f"{stats['updated']} updated",
        ]
        if stats["reactivated"]:
            parts.append(f"{stats['reactivated']} reactivated")
        parts.append(f"{stats['errors']} errors")
        print(f"  {', '.join(parts)}")
        print()

    if dry_run:
        print("=" * 60)
        print(f"DRY RUN complete. {len(sources)} sources inspected, nothing written.")
        print("=" * 60)
        return

    print("=" * 60)
    print(f"Scrape complete. {len(sources)} sources processed.")
    print(
        f"Total jobs: {sum(s['jobs_found'] for s in all_stats)} found, "
        f"{sum(s['inserted'] for s in all_stats)} inserted, "
        f"{sum(s['updated'] for s in all_stats)} updated, "
        f"{sum(s['reactivated'] for s in all_stats)} reactivated, "
        f"{sum(s['errors'] for s in all_stats)} errors"
    )
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Do not write to the DB")
    parser.add_argument("--company", default="", help="Substring filter on company name")
    args = parser.parse_args()
    main(dry_run=args.dry_run, company_filter=args.company)
