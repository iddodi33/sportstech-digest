"""test_apify_linkedin.py — unit tests for the Apify LinkedIn adapter and relevance filter.

Covers:
  - relevance_filter.check_relevance (denylist-driven noise removal)
  - adapters.apify_linkedin._parse_posted_at (freshness gate date parsing)
  - adapters.apify_linkedin._build_search_url (geography URL construction)

No pytest dependency — run directly:
    python jobs_pipeline/test_apify_linkedin.py
Exits non-zero if any assertion fails (CI-friendly).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Support running as: python jobs_pipeline/test_apify_linkedin.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.relevance_filter import check_relevance  # noqa: E402
from jobs_pipeline.adapters.apify_linkedin import (  # noqa: E402
    _build_search_url,
    _parse_posted_at,
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


# ── relevance_filter.check_relevance ────────────────────────────────────────

_NOISE_TITLES = [
    "Street Team Member",
    "Forum Coordinator",
    "Forum Moderator",
    "Community Moderator",
    "Content Moderator",
    "Brand Ambassador",
    "Customer Support Representative",
    "Customer Service Agent",
    "Retail Assistant",
    "Cashier",
]
_RELEVANT_TITLES = [
    "Backend Developer",
    "Senior Data Analyst",
    "Site Reliability Engineer",
    "Sports Scientist",
    "Customer Success Manager",
    "Product Manager",
    "Head of Marketing",
]

for title in _NOISE_TITLES:
    is_relevant, reason = check_relevance(title)
    check_true(f"check_relevance rejects {title!r}", is_relevant is False and reason is not None)

for title in _RELEVANT_TITLES:
    is_relevant, reason = check_relevance(title)
    check(f"check_relevance passes {title!r}", (is_relevant, reason), (True, None))

# Role-scope override bypasses the denylist for an exempted source id.
from jobs_pipeline import relevance_filter as _rf  # noqa: E402
_rf._ROLE_SCOPE_OVERRIDE_SOURCE_IDS = frozenset({"exempt-source-id"})
check(
    "check_relevance respects role-scope override",
    check_relevance("Street Team Member", source_id="exempt-source-id"),
    (True, None),
)
check(
    "check_relevance still rejects for non-exempt source",
    check_relevance("Street Team Member", source_id="other-source-id")[0],
    False,
)
_rf._ROLE_SCOPE_OVERRIDE_SOURCE_IDS = frozenset()


# ── _parse_posted_at ─────────────────────────────────────────────────────────

_now = datetime.now(timezone.utc)
_iso_5d_ago = (_now - timedelta(days=5)).isoformat().replace("+00:00", "Z")

check("_parse_posted_at ISO 5 days ago", _parse_posted_at(_iso_5d_ago), 5)
check("_parse_posted_at 'Today'", _parse_posted_at("Today"), 0)
check("_parse_posted_at 'Just now'", _parse_posted_at("Just now"), 0)
check("_parse_posted_at 'Yesterday'", _parse_posted_at("Yesterday"), 1)
check("_parse_posted_at '3 days ago'", _parse_posted_at("3 days ago"), 3)
check("_parse_posted_at '2 weeks ago'", _parse_posted_at("2 weeks ago"), 14)
check("_parse_posted_at '1 month ago'", _parse_posted_at("1 month ago"), 30)
check("_parse_posted_at '5 hours ago'", _parse_posted_at("5 hours ago"), 0)
check("_parse_posted_at None", _parse_posted_at(None), None)
check("_parse_posted_at empty string", _parse_posted_at(""), None)
check("_parse_posted_at unparseable", _parse_posted_at("Actively recruiting"), None)


# ── _build_search_url ───────────────────────────────────────────────────────

url = _build_search_url("Off The Ball", "Ireland")
check_true("_build_search_url includes keywords", "keywords=Off%20The%20Ball" in url)
check_true("_build_search_url includes location", "location=Ireland" in url)
check_true("_build_search_url includes recency filter", "f_TPR=r2592000" in url)
check_true("_build_search_url targets LinkedIn jobs search", url.startswith("https://www.linkedin.com/jobs/search/"))


print()
if _failed == 0:
    print(f"All {_passed} assertions passed.")
else:
    print(f"{_failed} FAILED, {_passed} passed.")
    raise SystemExit(1)
