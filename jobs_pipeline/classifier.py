"""classifier.py — rules-based pre-filters and Haiku classification for job listings."""

import json
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

# ── Rule 1: Junior keyword reject ─────────────────────────────────────────────
# NOTE: 'associate' is intentionally excluded — too ambiguous; Haiku handles it.

_JUNIOR_KEYWORDS = [
    "junior",
    "intern",
    "internship",
    "graduate",
    "entry level",
    "entry-level",
    "trainee",
    "apprentice",
]

# Pre-compiled word-boundary pattern — avoids "internal" matching "intern"
_JUNIOR_RE = re.compile(
    r"\b(" + "|".join(re.escape(kw) for kw in _JUNIOR_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _is_junior(title: str) -> bool:
    return bool(_JUNIOR_RE.search(title))


# ── Rule 2: FDI geography ─────────────────────────────────────────────────────
# Applied only when is_fdi=True AND is_irish_founded=False.
# Ireland-eligible check runs first; anything that matches passes.
# If no Ireland match and no ambiguous match, reject patterns are checked.
# Unknown locations default to 'pending' (admin reviews).

_IRELAND_ELIGIBLE = [
    # Cities / counties
    "dublin", "cork", "galway", "limerick", "belfast", "waterford",
    "kilkenny", "newry", "ireland", "irish", ", ie", "co. ",
    "skerries", "skibbereen", "navan", "swords", "castlebar", "carlow",
    "cavan", "sixmilebridge", "sandyford", "castleknock", "killarney",
    "donegal", "dundalk", "enniscorthy", "louth", "ennis", "scarriff",
    # Remote with European/EMEA scope
    "remote - emea", "remote - europe", "remote - eu", "remote emea", "remote europe",
]

# Matches "6 Locations", "12 locations", "1 Location", etc. — any numeric count.
_N_LOCATIONS_RE = re.compile(r"\b\d+ locations?\b", re.IGNORECASE)

# Ambiguous multi-location literals that are empirically non-Ireland for FDI companies.
# Numeric variants ("2 locations", "6 Locations", …) are caught by _N_LOCATIONS_RE above.
# Checked BEFORE _AMBIGUOUS_LOC so these auto-reject instead of going to admin.
_REJECT_AMBIGUOUS_FDI = [
    "multiple locations",
    "multiple cities",
    "various locations",
]

# Genuinely ambiguous — leave pending, don't reject
_AMBIGUOUS_LOC = [
    "locations",   # "2 Locations", "3 Locations" etc.
    "multiple",
]

_GEOGRAPHY_REJECTS = [
    # Remote that explicitly excludes Ireland
    "remote - us", "remote - usa", "remote - canada", "remote - latam",
    "remote - apac", "remote - anz", "remote - uk", "remote - ukraine",
    "remote - bulgaria", "remote ca",
    # US country markers
    "united states", ", usa",
    # US state abbreviations (comma-space prefix to avoid false matches)
    ", ma", ", ny", ", ca", ", tx", ", wa", ", il", ", ne", ", nc",
    ", fl", ", ga", ", oh", ", pa", ", nj", ", az", ", or", ", id",
    ", nh", ", wv", ", nv", ", me", ", ky", ", tn", ", mn", ", mo",
    ", va", ", md", ", sc", ", la", ", al", ", co",
    # Major US cities
    "boston", "los angeles", "new york", "san francisco", "chicago",
    "seattle", "austin", "nashville", "denver", "atlanta", "raleigh",
    "las vegas", "pueblo", "boise", "tempe", "portland", "sacramento",
    "bakersfield", "chico", "oroville", "susanville", "richvale",
    "redding", "jackson, c", "tracy, c", "eureka, c", "noblesville",
    "lincoln, ne", "omaha", "mercer island", "redwood city",
    "miami", "dallas", "kansas city", "north andover",
    "newburgh", "nashua", "north berwick", "white hall", "reynoldsburg",
    # UK
    "london", "manchester", "edinburgh", "united kingdom", "england",
    # Strava's non-Ireland custom location names
    "strava sf", "strava berlin", "strava london", "strava paris",
    # Continental Europe (non-Ireland)
    "sofia", "plovdiv", "tel aviv", "barcelona", "berlin", "paris",
    "amsterdam", "madrid", "tallinn", "estonia", "belgium",
    "netherlands", "den bosch", "chiavari",
    # Asia Pacific
    "singapore", "shanghai", "guangzhou", "beijing", "tokyo", "japan",
    "korea", "seoul", "india", "mumbai", "jakarta", "indonesia",
    "philippines", "manila", "sydney", "melbourne", "australia",
    # Americas (non-US)
    "canada", "colombia",
    # Middle East / Africa / Other
    "doha", "dubai", "abu dhabi", "ukraine", "bulgaria",
]


def _check_fdi_geography(location_raw: str | None) -> str:
    """Return 'pass', 'reject', or 'pending' for an FDI company's location string."""
    if not location_raw:
        return "pending"
    loc = location_raw.lower()

    # Ireland-eligible: check first so a location like "Dublin, IE" always passes
    if any(marker in loc for marker in _IRELAND_ELIGIBLE):
        return "pass"

    # Ambiguous multi-location patterns that are virtually always non-Ireland for FDIs.
    # _N_LOCATIONS_RE covers "N locations/location" for any integer N.
    # _REJECT_AMBIGUOUS_FDI covers non-numeric literals (multiple/various).
    if _N_LOCATIONS_RE.search(location_raw) or any(pattern in loc for pattern in _REJECT_AMBIGUOUS_FDI):
        log.debug("FDI ambiguous multi-location auto-reject: %s", location_raw)
        return "reject"

    # Remaining genuinely ambiguous patterns — leave for admin
    if any(pattern in loc for pattern in _AMBIGUOUS_LOC):
        return "pending"

    # Definitive non-Ireland
    if any(reject in loc for reject in _GEOGRAPHY_REJECTS):
        return "reject"

    # Unknown — safe default is pending, not reject
    return "pending"


# ── Haiku classification ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You classify sportstech job listings for an Irish SportsTech intelligence platform. "
    "Output ONLY valid JSON matching the schema provided. No markdown, no preamble, no "
    "explanation outside the JSON. You must be accurate about seniority — do not inflate "
    "titles. You must be strict about sportstech_relevance — pure back-office roles "
    "(general accounting, HR ops, facilities) are 'not_sportstech' even if at a sportstech company."
)

_VERTICALS = [
    "Performance Analytics",
    "Wearables & Hardware",
    "Fan Engagement",
    "Media & Broadcasting",
    "Health, Fitness and Wellbeing",
    "Scouting & Recruitment",
    "Esports & Gaming",
    "Betting & Fantasy",
    "Stadium & Event Tech",
    "Club Management Software",
    "Sports Education & Coaching",
    "Other / Emerging",
]

_VERTICAL_LIST = ", ".join(_VERTICALS)


def _build_user_prompt(job: dict, company: dict) -> str:
    return (
        "Classify this job listing:\n\n"
        f"COMPANY: {company.get('name', '')}\n"
        f"COMPANY_VERTICAL: {company.get('vertical') or 'Unknown'}\n"
        f"IS_FDI: {str(bool(company.get('is_fdi'))).lower()}\n"
        f"COMPANY_DESCRIPTION: {(company.get('description') or '')[:300]}\n\n"
        f"JOB_TITLE: {job.get('title', '')}\n"
        f"JOB_LOCATION: {job.get('location_raw') or 'Unknown'}\n"
        f"JOB_SUMMARY: {(job.get('summary') or '')[:1500]}\n\n"
        "Return JSON with exactly these fields: seniority, employment_type, remote_status, "
        "vertical, location_normalised, sportstech_relevance, sportstech_relevance_reason, "
        "job_function, classification_reasoning.\n\n"
        "Use null for any field that can't be determined confidently.\n\n"
        "Seniority mapping:\n"
        "- mid: 2-5 yrs, Engineer, Analyst, Specialist, Consultant, Associate (no senior modifier)\n"
        "- senior: 5-8 yrs, Senior X, X II, Senior Associate\n"
        "- lead: 8+ yrs, Lead, Staff, Principal, Manager, Head of, Senior Manager\n"
        "- executive: Director, VP, Chief, C-level\n\n"
        f"Vertical must be one of this closed list or null: {_VERTICAL_LIST}.\n"
        "Default to COMPANY_VERTICAL unless the specific role is clearly in a different vertical.\n\n"
        "sportstech_relevance rules:\n"
        "- 'relevant': engineering, product, design, data, sports science, commercial/sales, "
        "partnerships, BD, sport-specific marketing, customer success for sports clients, "
        "sport-specific legal/compliance, internal finance at the sportstech company\n"
        "- 'not_sportstech': general accounting/AP/AR, HR ops (payroll/benefits admin), "
        "facilities, office management, general IT support, admin assistants\n"
        "- 'ambiguous': unsure — default here if unclear\n\n"
        "location_normalised: convert location_raw to clean display. Examples:\n"
        "  'Dublin, Ireland' → 'Dublin'  |  'Limerick' → 'Limerick'\n"
        "  'Remote - EMEA' → 'Remote - EMEA'  |  '2 Locations' → 'Multiple Locations'\n"
        "  'Boston, MA' → 'Boston, MA'  |  'London, UK' → 'London, UK'\n\n"
        "job_function: Return one of: Engineering (software/hardware/devops/QA/infrastructure roles), "
        "Data & Analytics (data science, data engineering, analytics, BI, ML/AI roles), "
        "Product & Design (product management, UX, UI, graphic/visual design, research), "
        "Sales & Business Development (sales, BD, partnerships, account management, account executives), "
        "Marketing & Content (marketing, content, social media, brand, PR, communications, copywriting), "
        "Operations (project management, ops, supply chain, logistics, admin/EA, finance, legal, HR/talent), "
        "Customer Success (customer success, support, account management focused on retention), "
        "Other (anything that genuinely doesn't fit — legal counsel, executive roles, niche specialists), "
        "or null if you genuinely cannot determine the function from the job title and summary. "
        "Do not default to Other — only use it for explicit cross-functional or hard-to-categorise roles. "
        "Use null when truly unsure."
    )


def run_rules(job: dict, company: dict) -> dict:
    """Run rule-based pre-filters. First match wins.

    Returns:
        {
            "rejected": bool,
            "reason": str | None,
            "rules": {"junior_reject": bool, "fdi_geography_reject": bool, "not_sportstech_reject": bool},
            "geo_check": "pass" | "reject" | "pending" | "n/a",
        }
    """
    rules = {
        "junior_reject": False,
        "fdi_geography_reject": False,
        "not_sportstech_reject": False,
    }

    # Rule 1: Junior keyword
    if _is_junior(job.get("title") or ""):
        rules["junior_reject"] = True
        return {"rejected": True, "reason": "too_junior", "rules": rules, "geo_check": "n/a"}

    # Rule 2: FDI geography — only for pure FDIs with no Irish founding
    is_fdi = bool(company.get("is_fdi"))
    is_irish_founded = bool(company.get("is_irish_founded"))

    if is_fdi and not is_irish_founded:
        geo = _check_fdi_geography(job.get("location_raw"))
        if geo == "reject":
            rules["fdi_geography_reject"] = True
            return {"rejected": True, "reason": "fdi_geography", "rules": rules, "geo_check": geo}
        # "pass" or "pending" both continue to Haiku
        return {"rejected": False, "reason": None, "rules": rules, "geo_check": geo}

    return {"rejected": False, "reason": None, "rules": rules, "geo_check": "n/a"}


def classify_with_haiku(job: dict, company: dict, anthropic_client) -> dict:
    """Call Haiku to classify a job listing. Returns the parsed JSON response.

    Raises anthropic.APIError or json.JSONDecodeError on failure — callers
    should catch and handle (log + skip, retry on next run).
    """
    prompt = _build_user_prompt(job, company)
    msg = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()

    # Strip markdown code fences if Haiku wraps the JSON
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)

    return json.loads(text)


# ── Value normalisation ───────────────────────────────────────────────────────
# Haiku sometimes returns variant spellings. Map to the exact values the DB
# check constraints accept. Unknown values → None (stored as null).

_SENIORITY_MAP = {
    "mid": "mid",
    "senior": "senior",
    "lead": "lead",
    "executive": "executive",
    # Variants Haiku may produce — clamp to nearest valid tier
    "associate": "mid",
    "junior": None,     # out-of-scope; rule filter should have caught it
    "graduate": None,
    "entry": None,
    "intern": None,
    "staff": "lead",
    "principal": "lead",
}

_EMPLOYMENT_TYPE_MAP = {
    "full_time": "full_time",
    "part_time": "part_time",
    "contract": "contract",
    "internship": "internship",
    "temporary": "temporary",
    # Haiku variants
    "full-time": "full_time",
    "part-time": "part_time",
    "permanent": "full_time",
    "permanent_full_time": "full_time",
    "fixed_term": "contract",
    "fixed_term_contract": "contract",
    "fixed-term": "contract",
    "temp": "temporary",
    "freelance": "contract",
}

_REMOTE_STATUS_MAP = {
    "onsite": "onsite",
    "hybrid": "hybrid",
    "remote": "remote",
    # Haiku variants
    "on-site": "onsite",
    "on_site": "onsite",
    "office": "onsite",
    "in-office": "onsite",
    "in_office": "onsite",
    "remote_hybrid": "hybrid",
    "partially remote": "hybrid",
    "flexible": "hybrid",
}


def _norm(value: str | None, mapping: dict) -> str | None:
    if value is None:
        return None
    return mapping.get(str(value).lower().strip())


_JOB_FUNCTION_VALID = {
    "Engineering",
    "Data & Analytics",
    "Product & Design",
    "Sales & Business Development",
    "Marketing & Content",
    "Operations",
    "Customer Success",
    "Other",
}

_JOB_FUNCTION_NULL_LITERALS = {"null", "unclassified", "none", "n/a", ""}


def _norm_job_function(value: str | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    if v in _JOB_FUNCTION_VALID:
        return v
    if v.lower() in _JOB_FUNCTION_NULL_LITERALS:
        return None
    log.warning("Unexpected job_function value from Haiku: %r — normalising to None", v)
    return None


def normalise_haiku_fields(haiku: dict) -> dict:
    """Return a copy of haiku with DB-safe values for enum columns."""
    return {
        **haiku,
        "seniority": _norm(haiku.get("seniority"), _SENIORITY_MAP),
        "employment_type": _norm(haiku.get("employment_type"), _EMPLOYMENT_TYPE_MAP),
        "remote_status": _norm(haiku.get("remote_status"), _REMOTE_STATUS_MAP),
        "job_function": _norm_job_function(haiku.get("job_function")),
    }


def build_classification_record(
    haiku_result: dict | None,
    rules_result: dict,
) -> dict:
    """Assemble the classification JSONB record for storage."""
    return {
        "haiku": haiku_result,
        "rules": rules_result["rules"],
        "geo_check": rules_result.get("geo_check", "n/a"),
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL if haiku_result else "rules",
    }


if __name__ == "__main__":
    _FDI_CO = {"is_fdi": True, "is_irish_founded": False}

    def _job(location_raw: str) -> dict:
        return {"title": "Software Engineer", "location_raw": location_raw}

    cases = [
        ("6 Locations",       True,  "new regex — numeric N"),
        ("12 locations",      True,  "new regex — double-digit N"),
        ("1 Location",        True,  "new regex — singular form"),
        ("Multiple Locations", True, "literal, case-insensitive"),
        ("Various Locations", True,  "literal, case-insensitive"),
        ("Dublin, Ireland",   False, "Ireland whitelist — must pass"),
        ("Boston, MA",        True,  "FDI geography reject — sanity check"),
    ]

    passed = 0
    failed = 0
    for location_raw, expect_reject, label in cases:
        result = run_rules(_job(location_raw), _FDI_CO)
        actual_reject = result["rejected"]
        ok = actual_reject == expect_reject
        status = "PASS" if ok else "FAIL"
        print(f"{status}  {location_raw!r:25s}  expect_reject={expect_reject}  got={actual_reject}  [{label}]")
        if ok:
            passed += 1
        else:
            failed += 1

    print()
    if failed == 0:
        print(f"All {passed} assertions passed.")
    else:
        print(f"{failed} FAILED, {passed} passed.")
        raise SystemExit(1)
