"""
Second-pass career page discovery.
Rechecks only none_found rows with confidence != high.
Adds: Workday detection, careers/jobs subdomains, extended path variants,
redirect following, and slug fallback safety net.
"""

import asyncio
import csv
import re
import warnings
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

SCRIPT_DIR = Path(__file__).parent
CAREER_PAGES_CSV = SCRIPT_DIR / "career_pages.csv"
COMPANIES_CSV = SCRIPT_DIR / "companies_to_discover.csv"
PROBE_LOG = SCRIPT_DIR / "probe_log.txt"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=15)
CONCURRENCY = 8

# Manually verified — never reprocess regardless of ats_platform or confidence
PROTECTED_IDS = {
    "8bf66e75-bf4b-4d49-b3c2-c0db9f983117",  # Orreco
    "a4ffa495-6eec-4ba5-bf5e-283937dfe2e3",  # Hexis
    "fadd6a55-6628-4a95-9699-8017a276806d",  # Clubforce
}

# ---------------------------------------------------------------------------
# Workday: tenant.wdN.myworkdayjobs.com/{site}
# Handles /en-US/, /en-GB/ locale prefixes in URLs
# ---------------------------------------------------------------------------
WORKDAY_RE = re.compile(
    r'https?://([a-z0-9][a-z0-9-]*)\.wd(\d+)\.myworkdayjobs\.com'
    r'(?:/[a-z]{2}-[A-Z]{2})?'          # optional /en-US locale
    r'/([^/\s"\'<>?#]+)',                # site slug
    re.IGNORECASE,
)

# ATS fingerprint patterns — (platform, compiled_regex)
# Each regex captures the slug in group 1
ATS_PATTERNS = [
    ("greenhouse",      re.compile(r'boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_-]+)', re.I)),
    ("lever",           re.compile(r'jobs\.lever\.co/([a-zA-Z0-9_-]+)', re.I)),
    ("workable",        re.compile(r'apply\.workable\.com/([a-zA-Z0-9_-]+)', re.I)),
    ("ashby",           re.compile(r'jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)', re.I)),
    ("teamtailor",      re.compile(r'([a-zA-Z0-9-]+)\.teamtailor\.com', re.I)),
    ("bamboohr",        re.compile(r'([a-zA-Z0-9-]+)\.bamboohr\.com', re.I)),
    ("personio",        re.compile(r'([a-zA-Z0-9-]+)\.jobs\.personio\.(?:com|de)', re.I)),
    ("recruitee",       re.compile(r'([a-zA-Z0-9-]+)\.recruitee\.com', re.I)),
    ("breezy",          re.compile(r'([a-zA-Z0-9-]+)\.breezy\.hr', re.I)),
    # SmartRecruiters subdomain form (not the always-200 company API)
    ("smartrecruiters", re.compile(r'https?://([a-zA-Z0-9_-]+)\.smartrecruiters\.com', re.I)),
    ("smartrecruiters", re.compile(r'careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)', re.I)),
]

TEAMTAILOR_MARKERS = [
    "career site by teamtailor",
    "cdn.teamtailor.com",
    "teamtailor-jobs",
    "teamtailor.com/t/",
]

# Slugs that aren't real ATS slugs
_SLUG_BLOCKLIST = {"embed", "widget", "jobs", "careers", "apply", "external", "api", "en", "us", "oops", "error", "404", "notfound"}

# URL path segments that indicate a redirect to a login page or homepage — not a real careers page
_REJECT_PATH_SUFFIXES = {"/signin", "/login", "/auth", "/register", "/signup"}
_REJECT_PATH_EXACT = {"/", "/en/", "/en-us/", "/en-gb/", "/home", "/home/", "/index", "/index.html"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def root_domain(website: str) -> str:
    host = urlparse(website).hostname or ""
    return re.sub(r"^www\.", "", host)


def slugs_for(name: str, website: str) -> list[str]:
    name_clean = re.sub(r"[^a-z0-9]", "", name.lower())
    name_hyphen = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    d = root_domain(website).split(".")[0]
    d_nohyphen = d.replace("-", "")
    return list(dict.fromkeys(v for v in [name_clean, name_hyphen, d, d_nohyphen] if v))


def is_usable_careers_page(probe_url: str, final_url: str, rd: str) -> bool:
    """
    Return False if the final URL looks like a homepage redirect, login wall,
    or any other non-careers destination that we should ignore.
    """
    parsed = urlparse(final_url)
    path = parsed.path.rstrip("") or "/"

    # Login / auth walls
    for suffix in _REJECT_PATH_SUFFIXES:
        if path.endswith(suffix) or path == suffix:
            return False

    # Exact homepage patterns
    if path in _REJECT_PATH_EXACT:
        return False

    # Redirected back to the root domain homepage (e.g. /careers/ -> /)
    final_host = re.sub(r"^www\.", "", parsed.hostname or "")
    if final_host == rd and path in ("/", ""):
        return False

    # Redirected to a completely different unrelated domain (not a subdomain and not an ATS)
    known_ats_domains = {
        "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com",
        "teamtailor.com", "bamboohr.com", "personio.com", "personio.de",
        "recruitee.com", "breezy.hr", "smartrecruiters.com",
        "myworkdayjobs.com",
    }
    if not (
        final_host == rd
        or final_host.endswith("." + rd)
        or any(final_host.endswith(d) for d in known_ats_domains)
    ):
        # Redirected off-domain to something we don't recognise — might still be ok
        # (e.g. Blizzard -> careers.blizzard.com) — only reject if path looks like homepage
        if path in _REJECT_PATH_EXACT:
            return False

    return True


def generate_probe_urls(rd: str) -> list[str]:
    return [
        f"https://careers.{rd}",
        f"https://careers.{rd}/jobs",
        f"https://jobs.{rd}",
        f"https://jobs.{rd}/all",
        f"https://www.{rd}/careers/",
        f"https://www.{rd}/jobs/",
        f"https://www.{rd}/join/",
        f"https://www.{rd}/join-us/",
        f"https://www.{rd}/work-with-us/",
        f"https://www.{rd}/open-positions/",
        f"https://www.{rd}/hiring/",
        f"https://www.{rd}/team/careers/",
        f"https://www.{rd}/about/careers/",
        f"https://www.{rd}/company/careers/",
    ]


def careers_url_from_ats(platform: str, slug: str) -> str:
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


def ats_api_url(platform: str, slug: str) -> str:
    mapping = {
        "greenhouse":      f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "lever":           f"https://api.lever.co/v0/postings/{slug}?mode=json",
        "workable":        f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
        "ashby":           f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "teamtailor":      f"https://{slug}.teamtailor.com/jobs.json",
        "recruitee":       f"https://{slug}.recruitee.com/api/offers/",
        "personio":        f"https://{slug}.jobs.personio.com/search.json",
        "bamboohr":        f"https://{slug}.bamboohr.com/careers/list",
        "breezy":          f"https://{slug}.breezy.hr/json",
        # SmartRecruiters excluded — always returns 200 for any slug
    }
    return mapping.get(platform, "")


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

async def fetch(session: aiohttp.ClientSession, url: str, log_fh) -> tuple[int, str, str]:
    """GET url, follow redirects. Returns (status, final_url, html). Never raises."""
    try:
        async with session.get(url, allow_redirects=True) as resp:
            final = str(resp.url)
            log_fh.write(f"GET {url} -> {resp.status}  final={final}\n")
            if resp.status == 200:
                text = await resp.text(errors="replace")
                return resp.status, final, text
            return resp.status, final, ""
    except asyncio.TimeoutError:
        log_fh.write(f"GET {url} -> TIMEOUT (retrying)\n")
        try:
            async with session.get(url, allow_redirects=True) as resp:
                final = str(resp.url)
                log_fh.write(f"GET {url} -> {resp.status} (retry)  final={final}\n")
                if resp.status == 200:
                    text = await resp.text(errors="replace")
                    return resp.status, final, text
                return resp.status, final, ""
        except Exception as e:
            log_fh.write(f"GET {url} -> RETRY_ERROR {type(e).__name__}: {e}\n")
            return 0, url, ""
    except Exception as e:
        log_fh.write(f"GET {url} -> ERROR {type(e).__name__}: {e}\n")
        return 0, url, ""


async def verify_ats_get(session: aiohttp.ClientSession, platform: str, slug: str, log_fh) -> bool:
    """Verify an ATS GET endpoint. Returns True on confirmed hit."""
    api = ats_api_url(platform, slug)
    if not api:
        return False
    try:
        async with session.get(api, allow_redirects=True) as resp:
            log_fh.write(f"API {platform} {api} -> {resp.status}\n")
            if resp.status != 200:
                return False
            data = await resp.json(content_type=None)
            return _valid_ats_json(platform, data)
    except Exception as e:
        log_fh.write(f"API {platform} {api} -> ERROR {e}\n")
        return False


async def verify_workday(
    session: aiohttp.ClientSession,
    tenant: str, wd_n: str, site: str,
    log_fh,
) -> bool:
    """POST to Workday jobs API. Returns True if jobPostings key present."""
    api = f"https://{tenant}.wd{wd_n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    try:
        async with session.post(api, json=body) as resp:
            log_fh.write(f"POST (workday) {api} -> {resp.status}\n")
            if resp.status == 200:
                data = await resp.json(content_type=None)
                return isinstance(data, dict) and "jobPostings" in data
    except Exception as e:
        log_fh.write(f"POST (workday) {api} -> ERROR {e}\n")
    return False


def _valid_ats_json(platform: str, data) -> bool:
    if not isinstance(data, (dict, list)):
        return False
    if isinstance(data, list):
        return True
    if platform == "greenhouse":
        return "jobs" in data
    if platform == "workable":
        return "results" in data or "jobs" in data
    if platform == "ashby":
        return "jobPostings" in data or "jobs" in data
    return True


# ---------------------------------------------------------------------------
# ATS detection in HTML
# ---------------------------------------------------------------------------

def detect_workday_in_html(html: str, final_url: str) -> tuple[str, str, str] | None:
    """Return (tenant, wd_n, site) if a Workday URL is present."""
    for source in (final_url, html):
        m = WORKDAY_RE.search(source)
        if m:
            tenant = m.group(1).lower()
            wd_n = m.group(2)
            site = m.group(3).split("?")[0].split("#")[0].strip("/")
            # Skip login/auth/search pages that aren't job boards
            if site.lower() in ("login", "auth", "home", "search", "external"):
                continue
            return tenant, wd_n, site
    return None


def detect_ats_in_html(html: str, final_url: str) -> tuple[str, str] | None:
    """
    Return (platform, slug) for the first ATS fingerprint found.
    Returns None if nothing matched.
    """
    combined = html + "\n" + final_url

    for platform, pattern in ATS_PATTERNS:
        m = pattern.search(combined)
        if m:
            slug = m.group(1).lower()
            if slug in _SLUG_BLOCKLIST or len(slug) < 2:
                continue
            return platform, slug

    # Teamtailor by inline marker (no slug in URL)
    html_lower = html.lower()
    for marker in TEAMTAILOR_MARKERS:
        if marker in html_lower:
            # Try to extract a proper subdomain slug
            m = re.search(r'https?://([a-zA-Z0-9-]+)\.teamtailor\.com', html)
            slug = m.group(1).lower() if m else ""
            return "teamtailor", slug

    return None


def classify_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    hits = sum(1 for kw in [
        "apply now", "open positions", "open roles", "current openings",
        "job title", "full-time", "part-time", "remote", "hybrid",
        "we're hiring", "we are hiring", "join our team",
    ] if kw in text)
    if hits >= 3:
        return "medium", "Custom HTML with structured job listings"
    if hits >= 1:
        return "hard", "Careers page exists but limited structure"
    return "hard", "Careers page found but no clear job listings"


# ---------------------------------------------------------------------------
# Per-company probe logic
# ---------------------------------------------------------------------------

async def probe_company(
    session: aiohttp.ClientSession,
    row: dict,
    idx: int,
    total: int,
    log_fh,
    sem: asyncio.Semaphore,
) -> dict | None:
    """
    Returns an updated row dict if something new was found.
    Returns None to signal "leave this row unchanged".
    """
    async with sem:
        cid = row["company_id"]
        name = row["name"]
        website = row["website"]
        now = datetime.now(timezone.utc).isoformat()

        # Safety guard (belt-and-suspenders beyond the caller filter)
        if cid in PROTECTED_IDS or row["confidence"] == "high":
            return None

        print(f"[{idx}/{total}] Checking {name}...", end=" ", flush=True)
        log_fh.write(f"\n=== SECOND PASS [{idx}/{total}] {name} ({website}) ===\n")

        rd = root_domain(website)
        first_page_200: tuple[str, str] | None = None  # (final_url, html) of first 200 hit

        # ------------------------------------------------------------------
        # Step A: probe subdomains and extended path variants
        # ------------------------------------------------------------------
        for probe_url in generate_probe_urls(rd):
            status, final_url, html = await fetch(session, probe_url, log_fh)
            if status != 200 or not html:
                continue

            # Reject login walls and homepage redirects
            if not is_usable_careers_page(probe_url, final_url, rd):
                log_fh.write(f"  SKIP (usability check failed): {final_url}\n")
                continue

            if first_page_200 is None:
                first_page_200 = (final_url, html)

            # Workday detection first (takes priority)
            wd = detect_workday_in_html(html, final_url)
            if wd:
                tenant, wd_n, site = wd
                log_fh.write(f"  Workday candidate: tenant={tenant} wd={wd_n} site={site}\n")
                api_ep = f"https://{tenant}.wd{wd_n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
                careers_u = f"https://{tenant}.wd{wd_n}.myworkdayjobs.com/{site}"

                verified = await verify_workday(session, tenant, wd_n, site, log_fh)
                if verified:
                    print(f"OK {name}: workday (high confidence)")
                    return {**row,
                        "careers_url": careers_u,
                        "ats_platform": "workday",
                        "ats_api_endpoint": api_ep,
                        "scrapability": "medium",
                        "confidence": "high",
                        "needs_manual_review": "false",
                        "notes": f"Workday tenant={tenant} site={site}",
                        "discovered_at": now,
                    }
                # POST failed but we have the URL — medium confidence
                print(f"~~ {name}: workday fingerprint (medium confidence)")
                return {**row,
                    "careers_url": careers_u,
                    "ats_platform": "workday",
                    "ats_api_endpoint": api_ep,
                    "scrapability": "medium",
                    "confidence": "medium",
                    "needs_manual_review": "true",
                    "notes": f"Workday fingerprint on {probe_url}; API POST unverified",
                    "discovered_at": now,
                }

            # Other ATS fingerprints
            ats = detect_ats_in_html(html, final_url)
            if ats:
                platform, slug = ats
                log_fh.write(f"  ATS fingerprint: {platform} slug={slug!r}\n")

                if slug:
                    verified = await verify_ats_get(session, platform, slug, log_fh)
                    api_ep = ats_api_url(platform, slug)
                    careers_u = careers_url_from_ats(platform, slug)
                    if verified:
                        print(f"OK {name}: {platform} via HTML fingerprint (high confidence)")
                        return {**row,
                            "careers_url": careers_u,
                            "ats_platform": platform,
                            "ats_api_endpoint": api_ep,
                            "scrapability": "easy",
                            "confidence": "high",
                            "needs_manual_review": "false",
                            "notes": f"Fingerprint on {probe_url}",
                            "discovered_at": now,
                        }
                    print(f"~~ {name}: {platform} fingerprint (medium confidence)")
                    return {**row,
                        "careers_url": final_url,
                        "ats_platform": platform,
                        "ats_api_endpoint": "",
                        "scrapability": "medium",
                        "confidence": "medium",
                        "needs_manual_review": "true",
                        "notes": f"ATS fingerprint ({platform}/{slug}) on {probe_url} but API failed",
                        "discovered_at": now,
                    }
                else:
                    # Teamtailor marker without extractable slug
                    print(f"~~ {name}: {platform} marker, no slug (medium confidence)")
                    return {**row,
                        "careers_url": final_url,
                        "ats_platform": platform,
                        "ats_api_endpoint": "",
                        "scrapability": "medium",
                        "confidence": "medium",
                        "needs_manual_review": "true",
                        "notes": f"{platform} marker on {probe_url}; slug not extractable",
                        "discovered_at": now,
                    }

            # Page exists but no ATS fingerprint — keep going through URL list
            # (a later URL might expose the ATS embed)

        # ------------------------------------------------------------------
        # Step B: slug fallback — safety net for JS-rendered ATS embeds.
        # Also probes Workday tenants directly (many companies render the
        # Workday embed via JS so it's invisible to raw HTML fetches).
        # SmartRecruiters excluded (always-200 false positive).
        # ------------------------------------------------------------------
        if first_page_200 is None:
            for slug in slugs_for(name, website):
                # Workday tenant probe: try wd1–wd5 with slug as both tenant and site
                for wd_n in ("1", "2", "3", "5"):
                    wd_site_candidates = [slug, f"{slug}jobs", f"{slug}-jobs", "careers", "jobs"]
                    for wd_site in wd_site_candidates:
                        verified = await verify_workday(session, slug, wd_n, wd_site, log_fh)
                        if verified:
                            api_ep = f"https://{slug}.wd{wd_n}.myworkdayjobs.com/wday/cxs/{slug}/{wd_site}/jobs"
                            careers_u = f"https://{slug}.wd{wd_n}.myworkdayjobs.com/{wd_site}"
                            print(f"OK {name}: workday/{slug} via tenant probe (high confidence)")
                            return {**row,
                                "careers_url": careers_u,
                                "ats_platform": "workday",
                                "ats_api_endpoint": api_ep,
                                "scrapability": "medium",
                                "confidence": "high",
                                "needs_manual_review": "false",
                                "notes": f"Workday tenant probe: tenant={slug} wd={wd_n} site={wd_site}",
                                "discovered_at": now,
                            }

                # Other ATS slug probes
                for platform in [
                    "greenhouse", "lever", "ashby", "workable",
                    "teamtailor", "recruitee", "personio", "bamboohr", "breezy",
                ]:
                    verified = await verify_ats_get(session, platform, slug, log_fh)
                    if verified:
                        api_ep = ats_api_url(platform, slug)
                        careers_u = careers_url_from_ats(platform, slug)
                        print(f"OK {name}: {platform}/{slug} via slug fallback (medium confidence)")
                        return {**row,
                            "careers_url": careers_u,
                            "ats_platform": platform,
                            "ats_api_endpoint": api_ep,
                            "scrapability": "easy",
                            "confidence": "medium",
                            "needs_manual_review": "false",
                            "notes": "Discovered via slug fallback; no careers page found directly",
                            "discovered_at": now,
                        }

        # ------------------------------------------------------------------
        # Step C: classify whatever page we did find (if any)
        # ------------------------------------------------------------------
        if first_page_200 is not None:
            final_url, html = first_page_200
            scrapability, notes = classify_html(html)
            print(f"~~ {name}: custom_html at {final_url} (medium confidence)")
            return {**row,
                "careers_url": final_url,
                "ats_platform": "custom_html",
                "ats_api_endpoint": "",
                "scrapability": scrapability,
                "confidence": "medium",
                "needs_manual_review": "false",
                "notes": notes,
                "discovered_at": now,
            }

        # Nothing found at all
        print(f"XX {name}: still none_found")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    # Read career_pages.csv preserving all rows
    rows: list[dict] = []
    fieldnames: list[str] = []
    with open(CAREER_PAGES_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    # Select rows to recheck: none_found + not high-confidence + not protected
    to_process: list[tuple[int, dict]] = [
        (i, row) for i, row in enumerate(rows)
        if row["ats_platform"] == "none_found"
        and row["confidence"] != "high"
        and row["company_id"] not in PROTECTED_IDS
    ]

    total = len(to_process)
    print(f"Second pass: {total} none_found companies to recheck\n")

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(ssl=False)
    headers = {"User-Agent": USER_AGENT}

    updated_rows = list(rows)

    with open(PROBE_LOG, "a", encoding="utf-8") as log_fh:
        log_fh.write(
            f"\n\n{'='*60}\n"
            f"SECOND PASS started {datetime.now(timezone.utc).isoformat()}\n"
            f"{'='*60}\n"
        )
        async with aiohttp.ClientSession(
            connector=connector,
            headers=headers,
            timeout=TIMEOUT,
        ) as session:
            tasks = [
                probe_company(session, row, i + 1, total, log_fh, sem)
                for i, (_, row) in enumerate(to_process)
            ]
            results = await asyncio.gather(*tasks)

    # Apply updates in place
    recovered: list[dict] = []
    for (orig_idx, _), result in zip(to_process, results):
        if result is not None:
            updated_rows[orig_idx] = result
            recovered.append(result)

    # Write CSV back
    with open(CAREER_PAGES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    # Summary
    still_none = sum(
        1 for (orig_idx, _) in to_process
        if updated_rows[orig_idx]["ats_platform"] == "none_found"
    )
    platform_counts = Counter(r["ats_platform"] for r in recovered)

    print(f"\nSecond pass complete.")
    print(f"Rechecked:      {total} companies (were none_found)")
    print(f"Recovered:      {len(recovered)} companies (now have ATS or careers URL)")
    print(f"Still none_found: {still_none} companies")

    if recovered:
        print(f"\nPlatform breakdown of recovered:")
        for platform, count in sorted(platform_counts.items(), key=lambda x: -x[1]):
            print(f"  {platform}: {count}")

        print(f"\nRecovered companies:")
        for r in sorted(recovered, key=lambda x: (x["ats_platform"], x["name"])):
            flag = " [manual review]" if r["needs_manual_review"] == "true" else ""
            print(f"  {r['name']:30s}  {r['ats_platform']:15s}  {r['careers_url']}{flag}")

    if still_none:
        still_names = [
            updated_rows[orig_idx]["name"]
            for (orig_idx, _) in to_process
            if updated_rows[orig_idx]["ats_platform"] == "none_found"
        ]
        print(f"\nStill none_found ({still_none}):")
        for n in still_names:
            print(f"  - {n}")


if __name__ == "__main__":
    asyncio.run(main())
