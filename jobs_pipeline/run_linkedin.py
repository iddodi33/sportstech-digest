"""run_linkedin.py — scrape LinkedIn job listings for companies without ATS APIs.

Covers all active company_careers_sources rows where
ats_platform IN ('linkedin_only', 'none_found').

Two-stage per company:
  1. Google site:linkedin.com/jobs/view query → discover job URLs
  2. Fetch + parse each LinkedIn page

Flags:
  --dry-run          Print what would be upserted instead of calling the RPC.
  --company NAME     Process only the company whose name matches NAME (substring).
                     Combine with --dry-run for a quick smoke test.

Smoke-test example:
  python jobs_pipeline/run_linkedin.py --dry-run --company "Output Sports"
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

# Support running as: python jobs_pipeline/run_linkedin.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.supabase_jobs_client import get_linkedin_sources
from jobs_pipeline.adapters.linkedin import (
    LinkedInAdapter,
    _SerperAuthError,
    _SerperRateLimitError,
    _SerperNoResultsError,
    _RateLimitAbortError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


def main(dry_run: bool = False, company_filter: str = "") -> None:
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "DRY RUN" if dry_run else "live"
    print(f"LinkedIn scrape starting at {start} [{mode}]")

    sources = get_linkedin_sources()

    if company_filter:
        sources = [
            s for s in sources
            if company_filter.lower() in (s.get("company_name") or "").lower()
        ]
        print(f"Filtered to {len(sources)} source(s) matching '{company_filter}'")

    print(f"Found {len(sources)} LinkedIn sources to process")

    if not sources:
        print("No sources found — exiting.")
        return

    print()
    adapter = LinkedInAdapter()
    all_stats = []
    companies_done = 0
    companies_total = len(sources)

    try:
        for i, source in enumerate(sources, 1):
            company_name = source.get("company_name", "Unknown")
            platform = source.get("ats_platform", "?")
            fdi_flag = "FDI" if source.get("is_fdi") and not source.get("is_irish_founded") else "IRL"

            print(f"[{i}/{companies_total}] {company_name} ({platform}, {fdi_flag})")

            if dry_run:
                # Call fetch() directly — skip upsert entirely
                try:
                    jobs = adapter.fetch(source)
                except _SerperNoResultsError:
                    print("  [serper_no_results — no LinkedIn URLs found]")
                    companies_done += 1
                    continue
                except (_SerperAuthError, _SerperRateLimitError, _RateLimitAbortError) as exc:
                    print(f"  [ABORT] {exc}")
                    break

                audit = adapter._last_audit
                print(f"  Stage 1 (Serper):       {audit.get('serper_count', 0)} URL(s) found")
                print(f"  Stage 2 (domain filter): {audit.get('domain_accepted', 0)} accepted")
                print(f"  Stage 3 (fetch+parse):  {audit.get('fetch_succeeded', 0)} succeeded")
                print(f"  Stage 4 (name valid.):  {audit.get('validated', 0)} validated (would upsert)")

                rejections = audit.get("rejections", [])
                if rejections:
                    print(f"  Rejections ({len(rejections)}):")
                    for rej_url, reason in rejections:
                        print(f"    [{reason}] {rej_url[:72]}")

                if jobs:
                    print(f"  Jobs:")
                    for j in jobs:
                        loc = j.get("location_raw") or "location unknown"
                        url_preview = (j.get("url") or "")[:80]
                        print(f"    '{j['title']}' | {loc}")
                        print(f"     {url_preview}")

                companies_done += 1
                print()

                if adapter.abort:
                    break

            else:
                # Normal live mode
                stats = adapter.run(source)
                all_stats.append(stats)
                companies_done += 1

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

                if adapter.abort:
                    print("Aborting run due to rate-limit or block signal.")
                    break

    finally:
        adapter.close()

    if dry_run:
        print(f"Dry run complete. {companies_done}/{companies_total} companies processed.")
        return

    # Live mode summary
    total_found = sum(s["jobs_found"] for s in all_stats)
    total_inserted = sum(s["inserted"] for s in all_stats)
    total_updated = sum(s["updated"] for s in all_stats)
    total_reactivated = sum(s["reactivated"] for s in all_stats)
    total_errors = sum(s["errors"] for s in all_stats)

    print("=" * 60)
    print(f"LinkedIn scrape complete. {companies_done}/{companies_total} companies processed.")
    print(
        f"Total jobs: {total_found} found, {total_inserted} inserted, "
        f"{total_updated} updated, {total_reactivated} reactivated, {total_errors} errors"
    )
    if adapter.abort:
        print("Run was aborted early due to rate-limit or Google block.")
    print("=" * 60)

    log.info(
        "linkedin: completed %d/%d companies, %d jobs upserted, %d errors",
        companies_done, companies_total, total_inserted + total_updated, total_errors,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape LinkedIn jobs via Google discovery")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be upserted; do not write to Supabase",
    )
    parser.add_argument(
        "--company",
        default="",
        metavar="NAME",
        help="Process only the company whose name contains NAME (case-insensitive substring)",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, company_filter=args.company)
