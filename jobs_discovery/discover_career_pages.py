"""
Career page discovery tool for Irish sportstech companies.
Probes ATS APIs, then falls back to website HTML fingerprinting.
"""

import asyncio
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import warnings

import aiohttp
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

SCRIPT_DIR = Path(__file__).parent
INPUT_CSV = SCRIPT_DIR / "companies_to_discover.csv"
OUTPUT_CSV = SCRIPT_DIR / "career_pages.csv"
PROBE_LOG = SCRIPT_DIR / "probe_log.txt"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=15)
CONCURRENCY = 10

CAREERS_PATHS = [
    "/careers", "/careers/", "/jobs", "/jobs/",
    "/join-us", "/work-with-us",
    "/company/careers", "/about/careers",
    "/company/jobs", "/about/jobs",
]

ATS_FINGERPRINTS = [
    ("greenhouse",      ["boards.greenhouse.io", "greenhouse.io/embed", "greenhouse_iframe"]),
    ("lever",           ["jobs.lever.co", "lever-jobs-embed"]),
    ("workable",        ["apply.workable.com", "workable.com/widget"]),
    ("ashby",           ["jobs.ashbyhq.com", "ashbyhq.com/embed"]),
    ("teamtailor",      ["teamtailor.com"]),
    ("smartrecruiters", ["smartrecruiters.com"]),
    ("bamboohr",        ["bamboohr.com/careers"]),
    ("personio",        ["personio.com"]),
    ("recruitee",       ["recruitee.com"]),
    ("breezy",          ["breezy.hr"]),
]

# Pre-seeded known answers — skip probing for these company_ids
KNOWN = {
    "b108f4b5-4b6d-42dd-a8ee-72a9dc377db7": {  # Kitman Labs
        "ats_platform": "lever",
        "ats_api_endpoint": "https://api.lever.co/v0/postings/kitmanlabs?mode=json",
        "careers_url": "https://jobs.lever.co/kitmanlabs",
        "scrapability": "easy",
        "confidence": "high",
        "needs_manual_review": "false",
        "notes": "Pre-seeded",
    },
    "246209bc-7f37-4374-b6d1-9ec16ffdd7e1": {  # Output Sports
        "ats_platform": "personio",
        "ats_api_endpoint": "https://output-sports.jobs.personio.com/search.json",
        "careers_url": "https://output-sports.jobs.personio.com",
        "scrapability": "easy",
        "confidence": "high",
        "needs_manual_review": "false",
        "notes": "Pre-seeded",
    },
    "8bf66e75-bf4b-4d49-b3c2-c0db9f983117": {  # Orreco
        "ats_platform": "none_found",
        "ats_api_endpoint": "",
        "careers_url": "https://orreco.com/careers",
        "scrapability": "none",
        "confidence": "high",
        "needs_manual_review": "false",
        "notes": "Contact form only, hires via LinkedIn",
    },
    "a4ffa495-6eec-4ba5-bf5e-283937dfe2e3": {  # Hexis
        "ats_platform": "none_found",
        "ats_api_endpoint": "",
        "careers_url": "https://www.hexis.live/careers",
        "scrapability": "none",
        "confidence": "high",
        "needs_manual_review": "false",
        "notes": "Generic 'send us your CV' page, no ATS",
    },
    "fadd6a55-6628-4a95-9699-8017a276806d": {  # Clubforce
        "ats_platform": "none_found",
        "ats_api_endpoint": "",
        "careers_url": "",
        "scrapability": "none",
        "confidence": "high",
        "needs_manual_review": "false",
        "notes": "Hires via hr@clubforce.com and LinkedIn",
    },
}


def slugs_for(name: str, website: str) -> list[str]:
    """Generate slug variants to try against ATS APIs."""
    name_clean = re.sub(r"[^a-z0-9]", "", name.lower())
    name_hyphen = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    domain_root = urlparse(website).hostname or ""
    domain_root = re.sub(r"^www\.", "", domain_root)
    domain_root = domain_root.split(".")[0]  # e.g. kitmanlabs
    variants = [name_clean, name_hyphen, domain_root]
    # also try domain without TLD stripping for compound names
    full_domain_slug = re.sub(r"[^a-z0-9]", "", domain_root.lower())
    if full_domain_slug not in variants:
        variants.append(full_domain_slug)
    return list(dict.fromkeys(v for v in variants if v))


def ats_endpoints(slug: str) -> list[tuple[str, str, str]]:
    """Return (platform, url, api_endpoint) tuples for a slug."""
    return [
        ("greenhouse",      f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",          f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
        ("lever",           f"https://api.lever.co/v0/postings/{slug}?mode=json",               f"https://api.lever.co/v0/postings/{slug}?mode=json"),
        ("workable",        f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",           f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"),
        ("ashby",           f"https://api.ashbyhq.com/posting-api/job-board/{slug}",            f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
        ("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",     f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"),
        ("teamtailor",      f"https://{slug}.teamtailor.com/jobs.json",                          f"https://{slug}.teamtailor.com/jobs.json"),
        ("recruitee",       f"https://{slug}.recruitee.com/api/offers/",                         f"https://{slug}.recruitee.com/api/offers/"),
        ("personio",        f"https://{slug}.jobs.personio.com/search.json",                     f"https://{slug}.jobs.personio.com/search.json"),
        ("bamboohr",        f"https://{slug}.bamboohr.com/careers/list",                         f"https://{slug}.bamboohr.com/careers/list"),
        ("breezy",          f"https://{slug}.breezy.hr/json",                                    f"https://{slug}.breezy.hr/json"),
    ]


def is_valid_ats_response(platform: str, data) -> bool:
    """Return True if the JSON response looks like a real ATS hit."""
    if not isinstance(data, (dict, list)):
        return False
    if isinstance(data, list):
        return True  # Lever / Recruitee / Breezy return arrays
    # dict-based responses
    if platform == "greenhouse":
        return "jobs" in data
    if platform == "workable":
        return "results" in data or "jobs" in data
    if platform == "ashby":
        return "jobPostings" in data or "jobs" in data or "results" in data
    if platform == "smartrecruiters":
        # SR always returns 200 + empty JSON for unknown slugs — require at least one posting
        return isinstance(data.get("totalFound"), int) and data["totalFound"] > 0
    if platform == "teamtailor":
        return "data" in data or "jobs" in data
    if platform in ("personio", "bamboohr"):
        return True
    return True


def extract_slug_from_html(platform: str, html: str) -> str | None:
    """Extract an ATS slug from fingerprint URLs found in page HTML."""
    patterns = {
        "greenhouse":      r"boards\.greenhouse\.io/(?:embed/job_board\?for=|)([a-zA-Z0-9_-]+)",
        "lever":           r"jobs\.lever\.co/([a-zA-Z0-9_-]+)",
        "workable":        r"apply\.workable\.com/([a-zA-Z0-9_-]+)",
        "ashby":           r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)",
        "teamtailor":      r"([a-zA-Z0-9_-]+)\.teamtailor\.com",
        "smartrecruiters": r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)",
        "bamboohr":        r"([a-zA-Z0-9_-]+)\.bamboohr\.com",
        "personio":        r"([a-zA-Z0-9_-]+)\.jobs\.personio\.(?:com|de)",
        "recruitee":       r"([a-zA-Z0-9_-]+)\.recruitee\.com",
        "breezy":          r"([a-zA-Z0-9_-]+)\.breezy\.hr",
    }
    pat = patterns.get(platform)
    if not pat:
        return None
    m = re.search(pat, html)
    return m.group(1) if m else None


def careers_url_from_ats(platform: str, slug: str) -> str:
    """Return the human-facing careers URL for a confirmed ATS hit."""
    mapping = {
        "greenhouse":      f"https://boards.greenhouse.io/{slug}",
        "lever":           f"https://jobs.lever.co/{slug}",
        "workable":        f"https://apply.workable.com/{slug}",
        "ashby":           f"https://jobs.ashbyhq.com/{slug}",
        "teamtailor":      f"https://{slug}.teamtailor.com",
        "smartrecruiters": f"https://careers.smartrecruiters.com/{slug}",
        "bamboohr":        f"https://{slug}.bamboohr.com/careers",
        "personio":        f"https://{slug}.jobs.personio.com",
        "recruitee":       f"https://{slug}.recruitee.com",
        "breezy":          f"https://{slug}.breezy.hr",
    }
    return mapping.get(platform, "")


async def probe_ats_api(session: aiohttp.ClientSession, platform: str, url: str, log_fh) -> bool:
    """Try one ATS endpoint. Returns True on a valid hit."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            log_fh.write(f"ATS {platform} {url} -> {resp.status}\n")
            if resp.status != 200:
                return False
            try:
                data = await resp.json(content_type=None)
            except Exception:
                return False
            return is_valid_ats_response(platform, data)
    except asyncio.TimeoutError:
        # retry once
        log_fh.write(f"ATS {platform} {url} -> TIMEOUT (retrying)\n")
        try:
            async with session.get(url, allow_redirects=True) as resp:
                log_fh.write(f"ATS {platform} {url} -> {resp.status} (retry)\n")
                if resp.status != 200:
                    return False
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    return False
                return is_valid_ats_response(platform, data)
        except Exception as e:
            log_fh.write(f"ATS {platform} {url} -> RETRY_ERROR {e}\n")
            return False
    except Exception as e:
        log_fh.write(f"ATS {platform} {url} -> ERROR {e}\n")
        return False


async def phase1(session, name, website, log_fh):
    """Try all ATS slugs in parallel. Returns (platform, api_endpoint, slug) or None."""
    slugs = slugs_for(name, website)
    tasks = []
    meta = []
    for slug in slugs:
        for platform, url, api_ep in ats_endpoints(slug):
            tasks.append(probe_ats_api(session, platform, url, log_fh))
            meta.append((platform, url, api_ep, slug))

    results = await asyncio.gather(*tasks)
    for ok, (platform, url, api_ep, slug) in zip(results, meta):
        if ok:
            return platform, api_ep, slug
    return None


async def fetch_page(session: aiohttp.ClientSession, url: str, log_fh) -> tuple[int, str]:
    """Fetch a URL, return (status, html_text). Returns (0, '') on error."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            log_fh.write(f"PAGE {url} -> {resp.status}\n")
            if resp.status == 200:
                text = await resp.text(errors="replace")
                return resp.status, text
            return resp.status, ""
    except asyncio.TimeoutError:
        log_fh.write(f"PAGE {url} -> TIMEOUT\n")
        return 0, ""
    except Exception as e:
        log_fh.write(f"PAGE {url} -> ERROR {e}\n")
        return 0, ""


async def phase2(session, website, log_fh):
    """
    Try career paths on company website.
    Returns (careers_url, html, ats_hint_platform) or (None, '', None).
    """
    base = website.rstrip("/")
    for path in CAREERS_PATHS:
        url = base + path
        status, html = await fetch_page(session, url, log_fh)
        if status == 200 and html:
            # Check for ATS fingerprints
            for platform, markers in ATS_FINGERPRINTS:
                for marker in markers:
                    if marker in html:
                        return url, html, platform
            # No ATS fingerprint but page exists — may be custom HTML
            return url, html, None
    return None, "", None


def classify_html_page(html: str) -> tuple[str, str]:
    """
    Heuristically classify a custom careers HTML page.
    Returns (scrapability, notes).
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    job_indicators = [
        "apply now", "open positions", "open roles", "current openings",
        "job title", "full-time", "part-time", "remote", "hybrid",
        "we're hiring", "we are hiring", "join our team",
    ]
    score = sum(1 for kw in job_indicators if kw in text)
    if score >= 3:
        return "medium", "Custom HTML with structured job listings"
    elif score >= 1:
        return "hard", "Careers page exists but limited structure"
    return "hard", "Careers page found but no clear job listings"


async def discover_company(
    session: aiohttp.ClientSession,
    row: dict,
    idx: int,
    total: int,
    log_fh,
    sem: asyncio.Semaphore,
) -> dict:
    async with sem:
        cid = row["company_id"]
        name = row["name"]
        website = row["website"]
        now = datetime.now(timezone.utc).isoformat()

        base_result = {
            "company_id": cid,
            "name": name,
            "website": website,
            "careers_url": "",
            "ats_platform": "none_found",
            "ats_api_endpoint": "",
            "scrapability": "none",
            "confidence": "low",
            "needs_manual_review": "false",
            "notes": "",
            "discovered_at": now,
        }

        # Pre-seeded
        if cid in KNOWN:
            result = {**base_result, **KNOWN[cid], "discovered_at": now}
            print(f"[{idx}/{total}] OK {name}: {result['ats_platform']} (pre-seeded)")
            return result

        print(f"[{idx}/{total}] Checking {name}...", end=" ", flush=True)
        log_fh.write(f"\n=== [{idx}/{total}] {name} ({website}) ===\n")

        # Phase 1: ATS API probe
        hit = await phase1(session, name, website, log_fh)
        if hit:
            platform, api_ep, slug = hit
            result = {
                **base_result,
                "ats_platform": platform,
                "ats_api_endpoint": api_ep,
                "careers_url": careers_url_from_ats(platform, slug),
                "scrapability": "easy",
                "confidence": "high",
                "needs_manual_review": "false",
                "notes": "",
            }
            print(f"OK {name}: {platform} (high confidence)")
            return result

        # Phase 2: Website careers page discovery
        careers_url, html, ats_hint = await phase2(session, website, log_fh)

        if ats_hint and html:
            # Found ATS fingerprint in HTML — extract slug and retry Phase 1
            slug_from_html = extract_slug_from_html(ats_hint, html)
            if slug_from_html:
                for platform, url, api_ep in ats_endpoints(slug_from_html):
                    if platform == ats_hint:
                        ok = await probe_ats_api(session, platform, url, log_fh)
                        if ok:
                            result = {
                                **base_result,
                                "ats_platform": platform,
                                "ats_api_endpoint": api_ep,
                                "careers_url": careers_url_from_ats(platform, slug_from_html),
                                "scrapability": "easy",
                                "confidence": "high",
                                "needs_manual_review": "false",
                                "notes": f"Slug extracted from HTML fingerprint on {careers_url}",
                            }
                            print(f"OK {name}: {platform} via HTML fingerprint (high confidence)")
                            return result
            # Fingerprint found but API probe failed — mark medium confidence
            result = {
                **base_result,
                "ats_platform": ats_hint,
                "ats_api_endpoint": "",
                "careers_url": careers_url or "",
                "scrapability": "medium",
                "confidence": "medium",
                "needs_manual_review": "true",
                "notes": f"ATS fingerprint ({ats_hint}) found in HTML but API probe failed",
            }
            print(f"~~ {name}: {ats_hint} fingerprint only (medium confidence)")
            return result

        if careers_url and html:
            scrapability, notes = classify_html_page(html)
            result = {
                **base_result,
                "ats_platform": "custom_html",
                "ats_api_endpoint": "",
                "careers_url": careers_url,
                "scrapability": scrapability,
                "confidence": "medium",
                "needs_manual_review": "false",
                "notes": notes,
            }
            print(f"~~ {name}: custom_html at {careers_url} (medium confidence)")
            return result

        # Nothing found
        result = {
            **base_result,
            "needs_manual_review": "true",
            "notes": "No careers page or ATS found",
        }
        print(f"XX {name}: none_found (needs manual review)")
        return result


async def main():
    companies = []
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            companies.append(row)

    total = len(companies)
    results = []
    sem = asyncio.Semaphore(CONCURRENCY)

    connector = aiohttp.TCPConnector(ssl=False)
    headers = {"User-Agent": USER_AGENT}

    with open(PROBE_LOG, "w", encoding="utf-8") as log_fh:
        log_fh.write(f"Probe run started {datetime.now(timezone.utc).isoformat()}\n")
        async with aiohttp.ClientSession(
            connector=connector,
            headers=headers,
            timeout=TIMEOUT,
        ) as session:
            tasks = [
                discover_company(session, row, i + 1, total, log_fh, sem)
                for i, row in enumerate(companies)
            ]
            results = await asyncio.gather(*tasks)

    # Write output CSV
    fieldnames = [
        "company_id", "name", "website", "careers_url",
        "ats_platform", "ats_api_endpoint", "scrapability",
        "confidence", "needs_manual_review", "notes", "discovered_at",
    ]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nWrote {len(results)} rows to {OUTPUT_CSV}")

    # Summary stats
    from collections import Counter
    platform_counts = Counter(r["ats_platform"] for r in results)
    confidence_counts = Counter(r["confidence"] for r in results)
    manual_review = [r["name"] for r in results if r["needs_manual_review"] == "true"]

    print("\n--- ATS platform breakdown ---")
    for platform, count in sorted(platform_counts.items(), key=lambda x: -x[1]):
        print(f"  {platform}: {count}")

    print("\n--- Confidence breakdown ---")
    for conf, count in sorted(confidence_counts.items(), key=lambda x: -x[1]):
        print(f"  {conf}: {count}")

    if manual_review:
        print(f"\n--- Needs manual review ({len(manual_review)}) ---")
        for name in manual_review:
            print(f"  - {name}")
    else:
        print("\nNo companies need manual review.")


if __name__ == "__main__":
    asyncio.run(main())
