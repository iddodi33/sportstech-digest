"""
One-off script to import career_pages.csv into hub Supabase
as company_careers_sources rows.

Run once after the SQL migration. Safe to re-run — uses upsert on (company_id).
"""
import csv
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SERVICE_ROLE_KEY:
    sys.exit("Missing NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env")

supabase = create_client(SUPABASE_URL, SERVICE_ROLE_KEY)

CSV_PATH = Path(__file__).parent / "career_pages.csv"


WORKDAY_RE = re.compile(
    r"https?://([a-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:wday/cxs/[^/]+/)?([^/?]+)"
)


def parse_workday(endpoint: str):
    """Extract (tenant, pod, site) from a Workday endpoint URL."""
    if not endpoint:
        return None, None, None
    m = WORKDAY_RE.search(endpoint)
    if not m:
        return None, None, None
    return m.group(1), m.group(2), m.group(3)


def extract_slug(row: dict) -> str | None:
    """Pull the ATS slug from the endpoint URL for non-Workday platforms."""
    ep = row.get("ats_api_endpoint") or ""
    platform = row.get("ats_platform") or ""

    patterns = {
        "greenhouse": r"/boards/([^/]+)/jobs",
        "lever": r"/postings/([^?/]+)",
        "workable": r"/accounts/([^/]+)/jobs",
        "ashby": r"/job-board/([^/?]+)",
        "teamtailor": r"https?://([a-z0-9-]+)\.teamtailor\.com|https?://careers\.([a-z0-9.-]+)/jobs",
        "bamboohr": r"https?://([a-z0-9-]+)\.bamboohr\.com",
        "personio": r"https?://([a-z0-9-]+)\.jobs\.personio\.",
        "recruitee": r"https?://([a-z0-9-]+)\.recruitee\.com",
        "breezy": r"https?://([a-z0-9-]+)\.breezy\.hr",
        "smartrecruiters": r"/companies/([^/]+)/postings|https?://([a-z0-9-]+)\.smartrecruiters\.com",
    }

    if platform not in patterns:
        return None
    m = re.search(patterns[platform], ep)
    if not m:
        return None
    # Return first non-None group
    for g in m.groups():
        if g:
            return g
    return None


def parse_ts(ts_str: str) -> str | None:
    """Return an ISO timestamp string or None."""
    if not ts_str or not ts_str.strip():
        return None
    try:
        # Already ISO, just validate
        datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return ts_str
    except ValueError:
        return None


def row_to_record(row: dict) -> dict:
    platform = row["ats_platform"]
    endpoint = row.get("ats_api_endpoint") or None

    record = {
        "company_id": row["company_id"],
        "careers_url": row.get("careers_url") or None,
        "ats_platform": platform,
        "ats_api_endpoint": endpoint,
        "ats_slug": extract_slug(row),
        "scrapability": row["scrapability"],
        "is_active": True,
        "confidence": row.get("confidence") or None,
        "discovered_at": parse_ts(row.get("discovered_at", "")),
        "last_verified_at": parse_ts(row.get("discovered_at", "")),
        "notes": row.get("notes") or None,
    }

    # Populate Workday fields if applicable
    if platform == "workday":
        tenant, pod, site = parse_workday(endpoint)
        record["workday_tenant"] = tenant
        record["workday_pod"] = pod
        record["workday_site"] = site

    return record


def main():
    if not CSV_PATH.exists():
        sys.exit(f"CSV not found at {CSV_PATH}")

    with open(CSV_PATH) as f:
        rows = list(csv.DictReader(f))

    print(f"Loaded {len(rows)} rows from {CSV_PATH.name}")

    # Deactivate existing active rows for these companies before inserting fresh ones
    # This preserves historical rows (audit trail) but ensures only the new import is active
    company_ids = [r["company_id"] for r in rows]
    print(f"Deactivating existing active rows for {len(company_ids)} companies...")
    resp = supabase.table("company_careers_sources") \
        .update({"is_active": False}) \
        .in_("company_id", company_ids) \
        .eq("is_active", True) \
        .execute()
    print(f"Deactivated {len(resp.data)} existing rows")

    # Insert fresh records
    records = [row_to_record(r) for r in rows]
    
    # Report Workday resolution before inserting
    workday_rows = [r for r in records if r["ats_platform"] == "workday"]
    print(f"\nWorkday rows ({len(workday_rows)}):")
    for r in workday_rows:
        status = "OK" if r["workday_tenant"] else "MISSING TENANT"
        print(f"  {r['company_id'][:8]}  tenant={r['workday_tenant']} pod={r['workday_pod']} site={r['workday_site']}  [{status}]")

    # Batch insert
    batch_size = 50
    inserted = 0
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        resp = supabase.table("company_careers_sources").insert(batch).execute()
        inserted += len(resp.data)
        print(f"Inserted batch {i // batch_size + 1}: {len(resp.data)} rows")

    print(f"\nDone. Inserted {inserted} rows.")

    # Summary
    from collections import Counter
    platforms = Counter(r["ats_platform"] for r in records)
    scrapability = Counter(r["scrapability"] for r in records)
    print(f"\nBy ATS platform:")
    for p, n in platforms.most_common():
        print(f"  {p:20s} {n}")
    print(f"\nBy scrapability:")
    for s, n in scrapability.most_common():
        print(f"  {s:10s} {n}")


if __name__ == "__main__":
    main()