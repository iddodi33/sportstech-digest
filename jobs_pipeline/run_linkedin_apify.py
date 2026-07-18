"""run_linkedin_apify.py — scrape LinkedIn job listings for linkedin_only companies via Apify.

Covers all active company_careers_sources rows where ats_platform='linkedin_only'.
Companion to run_linkedin.py, which covers ats_platform='none_found' via Serper.

Requires APIFY_TOKEN in the environment. If it's not set, every company logs
a clean "APIFY_TOKEN missing" message and the run exits without writing
anything — no crash, no silent no-op.

Flags:
  --dry-run          Print what would be upserted instead of calling the RPC.
  --company NAME     Process only the company whose name matches NAME (substring).
                     Combine with --dry-run for a quick smoke test.

Smoke-test example:
  python jobs_pipeline/run_linkedin_apify.py --dry-run --company "Off The Ball"
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

# Support running as: python jobs_pipeline/run_linkedin_apify.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.supabase_jobs_client import get_apify_linkedin_sources
from jobs_pipeline.adapters.apify_linkedin import (
    ApifyLinkedInAdapter,
    _ApifyTokenMissingError,
    _ApifyRequestError,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)


def main(dry_run: bool = False, company_filter: str = "") -> None:
    start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "DRY RUN" if dry_run else "live"
    print(f"LinkedIn (Apify) scrape starting at {start} [{mode}]")

    sources = get_apify_linkedin_sources()

    if company_filter:
        sources = [
            s for s in sources
            if company_filter.lower() in (s.get("company_name") or "").lower()
        ]
        print(f"Filtered to {len(sources)} source(s) matching '{company_filter}'")

    print(f"Found {len(sources)} linkedin_only sources to process")

    if not sources:
        print("No sources found — exiting.")
        return

    if not os.getenv("APIFY_TOKEN"):
        print(
            "APIFY_TOKEN is not set — every company below will fail cleanly "
            "(logged, last_scrape_error recorded, no crash). Add APIFY_TOKEN "
            "to .env to run this for real."
        )

    print()
    adapter = ApifyLinkedInAdapter()
    all_stats = []
    companies_done = 0
    companies_total = len(sources)

    for i, source in enumerate(sources, 1):
        company_name = source.get("company_name", "Unknown")
        fdi_flag = "FDI" if source.get("is_fdi") and not source.get("is_irish_founded") else "IRL"

        print(f"[{i}/{companies_total}] {company_name} ({fdi_flag})")

        if dry_run:
            try:
                jobs = adapter.fetch(source)
            except _ApifyTokenMissingError as exc:
                print(f"  [ABORT] {exc}")
                break
            except _ApifyRequestError as exc:
                print(f"  [request failed] {exc}")
                companies_done += 1
                print()
                continue

            audit = adapter._last_audit
            print(f"  URLs built:              {audit.get('urls_built', 0)}")
            print(f"  Fetched from Apify:       {audit.get('fetched', 0)}")
            print(f"  Dropped (freshness):      {audit.get('dropped_freshness', 0)}")
            print(f"  Dropped (relevance):      {audit.get('dropped_relevance', 0)}")
            print(f"  Dropped (name mismatch):  {audit.get('dropped_name_mismatch', 0)}")
            print(f"  Would write:              {audit.get('validated', 0)}")

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
                print("Aborting run — APIFY_TOKEN missing.")
                break

    if dry_run:
        print(f"Dry run complete. {companies_done}/{companies_total} companies processed.")
        return

    total_found = sum(s["jobs_found"] for s in all_stats)
    total_inserted = sum(s["inserted"] for s in all_stats)
    total_updated = sum(s["updated"] for s in all_stats)
    total_reactivated = sum(s["reactivated"] for s in all_stats)
    total_errors = sum(s["errors"] for s in all_stats)

    print("=" * 60)
    print(f"LinkedIn (Apify) scrape complete. {companies_done}/{companies_total} companies processed.")
    print(
        f"Total jobs: {total_found} found, {total_inserted} inserted, "
        f"{total_updated} updated, {total_reactivated} reactivated, {total_errors} errors"
    )
    if adapter.abort:
        print("Run was aborted early — APIFY_TOKEN missing.")
    print("=" * 60)

    log.info(
        "apify_linkedin: completed %d/%d companies, %d jobs upserted, %d errors",
        companies_done, companies_total, total_inserted + total_updated, total_errors,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape linkedin_only companies via Apify")
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
