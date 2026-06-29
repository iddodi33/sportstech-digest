"""test_linkedin_gate.py — unit tests for the LinkedIn posted-age gate.

Covers the strict-recency helpers added to adapters/linkedin.py:
  - _extract_posted_days_ago  (JSON-LD datePosted + relative-timestamp regex)
  - _extract_job_id           (trailing numeric ID, incl. URLs with query/refId)

No pytest dependency — run directly:
    python jobs_pipeline/test_linkedin_gate.py
Exits non-zero if any assertion fails (CI-friendly).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Support running as: python jobs_pipeline/test_linkedin_gate.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.adapters.linkedin import (  # noqa: E402
    MIN_LINKEDIN_JOB_ID,
    _extract_job_id,
    _extract_posted_days_ago,
)

_passed = 0
_failed = 0


def check(label: str, actual, expected) -> None:
    global _passed, _failed
    ok = actual == expected
    status = "PASS" if ok else "FAIL"
    print(f"{status}  {label}: got={actual!r} expected={expected!r}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def check_true(label: str, cond: bool) -> None:
    global _passed, _failed
    status = "PASS" if cond else "FAIL"
    print(f"{status}  {label}: {cond}")
    if cond:
        _passed += 1
    else:
        _failed += 1


# ── _extract_posted_days_ago ──────────────────────────────────────────────────

def _jsonld(date_posted: str) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"@type": "JobPosting", "title": "X", "datePosted": "{date_posted}"}}'
        "</script></head><body></body></html>"
    )


# JSON-LD path: a date 10 days ago should read back as ~10 (tolerate ±1 for the
# tiny gap between the two now() reads possibly straddling a day boundary).
_ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
_jsonld_result = _extract_posted_days_ago(_jsonld(_ten_days_ago))
check_true(
    "jsonld datePosted 10 days ago ~ 10",
    _jsonld_result is not None and abs(_jsonld_result - 10) <= 1,
)

# JSON-LD with a 'Z' suffix instead of an offset must still parse.
_z_date = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
_z_result = _extract_posted_days_ago(_jsonld(_z_date))
check_true(
    "jsonld datePosted with Z suffix ~ 3",
    _z_result is not None and abs(_z_result - 3) <= 1,
)

# Relative-timestamp regex path (no JSON-LD date present).
check("relative 'Posted 3 weeks ago'", _extract_posted_days_ago("Posted 3 weeks ago"), 21)
check("relative 'Reposted 5 days ago'", _extract_posted_days_ago("Reposted 5 days ago"), 5)
check("relative 'Posted 2 months ago'", _extract_posted_days_ago("Posted 2 months ago"), 60)
check("relative 'Posted 1 year ago'", _extract_posted_days_ago("Posted 1 year ago"), 365)
check("relative 'Posted 4 hours ago' -> 0", _extract_posted_days_ago("Posted 4 hours ago"), 0)

# No usable date anywhere -> None (caller then falls back to the job-ID floor).
check("no date present -> None", _extract_posted_days_ago("<html><body>no date here</body></html>"), None)
check("empty string -> None", _extract_posted_days_ago(""), None)


# ── _extract_job_id ───────────────────────────────────────────────────────────

# Bare-ID slug.
check(
    "bare-id slug",
    _extract_job_id("https://www.linkedin.com/jobs/view/4400000000/"),
    4_400_000_000,
)
# Title slug with trailing ID.
check(
    "title slug with trailing id",
    _extract_job_id("https://www.linkedin.com/jobs/view/software-engineer-at-acme-4400000000/"),
    4_400_000_000,
)
# Trailing query string after the ID — must read the ID, not the query.
check(
    "id with ?refId query string",
    _extract_job_id("https://www.linkedin.com/jobs/view/4400000000/?refId=abc"),
    4_400_000_000,
)
# refId whose value itself contains digits must NOT be mistaken for the job ID.
check(
    "id with ?refId=abc123 (digits in refId)",
    _extract_job_id("https://www.linkedin.com/jobs/view/4400000000?refId=abc123"),
    4_400_000_000,
)
# trackingId + multiple params after a title slug.
check(
    "title slug + ?trackingId&refId params",
    _extract_job_id(
        "https://www.linkedin.com/jobs/view/data-analyst-3812345678"
        "?trackingId=Xy9%3D&refId=zz99"
    ),
    3_812_345_678,
)
# Fragment after the ID.
check(
    "id with #fragment",
    _extract_job_id("https://www.linkedin.com/jobs/view/4400000000/#applied"),
    4_400_000_000,
)
# Legacy 8-digit (~2015) ID — parses, and is below the floor.
_legacy = _extract_job_id("https://www.linkedin.com/jobs/view/12345678/")
check("legacy 8-digit id parses", _legacy, 12_345_678)
check_true("legacy 8-digit id is below floor", _legacy is not None and _legacy < MIN_LINKEDIN_JOB_ID)
# Current-era id is at/above the floor.
check_true("current-era id (4.40e9) >= floor", 4_400_000_000 >= MIN_LINKEDIN_JOB_ID)
# No numeric ID anywhere -> None (caller treats as posted_age_unknown).
check("slug with no digits -> None", _extract_job_id("https://www.linkedin.com/jobs/view/no-id-here/"), None)


# ── summary ───────────────────────────────────────────────────────────────────

print()
if _failed == 0:
    print(f"All {_passed} assertions passed.")
else:
    print(f"{_failed} FAILED, {_passed} passed.")
    raise SystemExit(1)
