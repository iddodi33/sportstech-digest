"""relevance_filter.py — rule-based noise filter for LinkedIn job titles.

Applied by both LinkedIn adapters (adapters/linkedin.py, adapters/apify_linkedin.py)
before a job reaches upsert_job. NOT a substitute for the Haiku classifier
(classifier.py) — this only catches title patterns that are unambiguous noise
regardless of company, so street-team/moderation/generic-support roles never
reach the pending review queue.

Deliberately conservative: only _NOISE_RE causes a drop. A title matching
neither list still passes through — the Haiku classifier remains the
authority on ambiguous titles. _FUNCTION_ALLOWLIST_KEYWORDS is reference-only
(documents the job_function categories this filter is aimed at) and does NOT
drive rejection: gating on "outside the allowlist" would false-positive on
legitimate titles that just don't happen to contain a listed keyword (e.g.
"Backend Developer", "SRE").

Company-level scoping is data, not code: to exclude a company entirely, set
company_careers_sources.is_active=false. _ROLE_SCOPE_OVERRIDE_SOURCE_IDS
exists so a source can be exempted from the noise denylist without a
per-company `if` buried in the filter logic — empty today.
"""

import re

# Roles that are almost never sportstech-relevant regardless of company —
# derived from observed rejection noise (Off The Ball street-team roles,
# EA Sports forum-coordinator/moderation roles, generic front-line support).
# Word-boundary matched, case-insensitive.
_NOISE_PATTERNS = [
    r"street team",
    r"forum (coordinator|moderator)",
    r"community moderat\w*",
    r"content moderat\w*",
    r"brand ambassador",
    r"promotional? staff",
    r"customer (service|support) (rep|representative|agent)s?",
    r"call cent(er|re)",
    r"retail (associate|assistant)",
    r"cashier",
    r"merchandis(er|ing) assistant",
    r"warehouse (operative|assistant)",
]
_NOISE_RE = re.compile(r"\b(" + "|".join(_NOISE_PATTERNS) + r")\b", re.IGNORECASE)

# Reference-only — the job_function categories this filter is aimed at
# (mirrors classifier.py's 8-value job_function enum). NOT used to reject.
_FUNCTION_ALLOWLIST_KEYWORDS = [
    "engineer", "engineering", "developer", "software", "devops", "qa",
    "data", "analyst", "analytics", "scientist", "ml", "ai",
    "product", "design", "ux", "ui",
    "performance", "sports science", "biomechani", "physiotherap",
    "strength and conditioning",
    "sales", "business development", "partnerships", "account",
    "marketing", "content", "social media", "brand", "communications",
    "customer success",
]

# Source IDs (company_careers_sources.id) exempted from the noise denylist,
# because the company's core business genuinely includes these functions.
# Empty today — add a source id + comment here if a legitimate case appears;
# do not add a company-name `if` branch instead.
_ROLE_SCOPE_OVERRIDE_SOURCE_IDS: frozenset = frozenset()


def check_relevance(title: str, *, source_id: str | None = None) -> tuple[bool, str | None]:
    """Return (is_relevant, reason). reason is None when is_relevant is True.

    Conservative: only a denylist hit causes a drop. Everything else passes
    so the Haiku classifier remains the authority on ambiguous titles.
    """
    if source_id and source_id in _ROLE_SCOPE_OVERRIDE_SOURCE_IDS:
        return True, None
    m = _NOISE_RE.search(title or "")
    if m:
        return False, f"relevance_noise ({m.group(1).lower()})"
    return True, None


if __name__ == "__main__":
    cases = [
        ("Street Team Member", False),
        ("Forum Coordinator", False),
        ("Community Moderator", False),
        ("Customer Support Representative", False),
        ("Retail Assistant", False),
        ("Cashier", False),
        ("Backend Developer", True),
        ("Senior Data Analyst", True),
        ("Site Reliability Engineer", True),
        ("Sports Scientist", True),
        ("Customer Success Manager", True),
    ]
    passed = failed = 0
    for title, expect_relevant in cases:
        is_relevant, reason = check_relevance(title)
        ok = is_relevant == expect_relevant
        status = "PASS" if ok else "FAIL"
        print(f"{status}  {title!r:35s}  expect_relevant={expect_relevant}  got={is_relevant}  reason={reason}")
        passed += ok
        failed += not ok
    print()
    if failed == 0:
        print(f"All {passed} assertions passed.")
    else:
        print(f"{failed} FAILED, {passed} passed.")
        raise SystemExit(1)
