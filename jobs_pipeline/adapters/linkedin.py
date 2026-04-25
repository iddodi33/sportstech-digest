"""linkedin.py — LinkedIn job scraper adapter.

Four-stage process per company:
  1. Serper API  — POST to google.serper.dev/search to discover LinkedIn job URLs
  2. Domain filter — reject wrong-country subdomains (prevents cross-company confusion)
  3. LinkedIn fetch — GET each page; rotate UA, throttle, handle 999s
  4. Name validation — compare hiringOrganization.name from JSON-LD to source name

Abort signals (propagate through run() → run_linkedin.py breaks):
  _SerperAuthError    : HTTP 401/403 from Serper — key bad/revoked
  _SerperRateLimitError: HTTP 429 from Serper — free tier exhausted
  _RateLimitAbortError : 3 consecutive LinkedIn 999/429 — rate limited

Skip signals (per-company only, do not abort run):
  _SerperNoResultsError: Serper returned 0 LinkedIn job URLs for this company

Session management:
  - Single requests.Session reused across companies
  - Refreshed (with 60-90 s sleep) every 25 LinkedIn page fetches

Throttle:
  - 1.5-2.5 s between LinkedIn fetches
  - Serper calls are synchronous; no per-call throttle needed at the volumes we use
"""

import html as html_module
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from .base import BaseAdapter
from ..supabase_jobs_client import get_client, upsert_job

log = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_SUMMARY_MAX = 1500

_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
]

_LINKEDIN_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Referer": "https://www.google.com/",
    "Upgrade-Insecure-Requests": "1",
}

# Matches any linkedin.com/jobs/view/ URL regardless of subdomain
_JOB_VIEW_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/view/([^/?#]+)",
    re.IGNORECASE,
)

# Sentinel: _fetch_page returns this when a 999/429 happens below the abort threshold
_RATE_LIMITED = object()

# Common legal suffixes to strip when normalising company names for comparison.
# Ordered longest-first so ' co.' is checked before ' co'.
_STRIP_SUFFIXES: list[str] = sorted(
    [
        " limited", " ltd", " inc.", " inc", " llc", " gmbh",
        " bv", " plc", " group", " company", " co.", " co",
    ],
    key=len, reverse=True,
)


# ── Abort / skip signals ──────────────────────────────────────────────────────

class _SerperAuthError(Exception):
    """Serper returned 401/403 — API key bad or revoked. Abort run."""


class _SerperRateLimitError(Exception):
    """Serper returned 429 — free tier exhausted. Abort run."""


class _SerperNoResultsError(Exception):
    """Serper returned no LinkedIn job URLs for this company. Skip only."""


class _RateLimitAbortError(Exception):
    """Three consecutive LinkedIn 999/429 responses. Abort run."""


# ── Module-level helpers ──────────────────────────────────────────────────────

def _update_source_error(source_id: str, error: str) -> None:
    """Write last_scrape_error + last_verified_at to company_careers_sources."""
    client = get_client()
    if client is None:
        return
    try:
        client.table("company_careers_sources").update({
            "last_scrape_error": error,
            "last_verified_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", source_id).execute()
        log.debug("source %s → last_scrape_error=%r", source_id[:8], error)
    except Exception as exc:
        log.warning("could not update source error for %s: %s", source_id[:8], exc)


def _canonical_url(raw: str) -> str:
    """Return https://www.linkedin.com/jobs/view/{slug}/ stripping query params."""
    m = _JOB_VIEW_RE.match(raw)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return raw.split("?")[0].rstrip("/") + "/"


def _extract_subdomain(url: str) -> str:
    """Return the subdomain of a LinkedIn URL (e.g. 'ie', 'www', 'am')."""
    m = re.match(r"https?://([^.]+)\.linkedin\.com/", url, re.IGNORECASE)
    return m.group(1).lower() if m else "www"


def _strip_html(html_str: str, max_chars: int = _SUMMARY_MAX) -> str:
    """Unescape entities, strip HTML tags, collapse whitespace, truncate."""
    if not html_str:
        return ""
    try:
        text = BeautifulSoup(
            html_module.unescape(html_str), "html.parser"
        ).get_text(separator=" ")
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            return text[: max_chars - 3] + "..."
        return text
    except Exception:
        raw = html_str[:max_chars]
        return (raw[: max_chars - 3] + "...") if len(html_str) > max_chars else raw


def _format_salary(base_salary: object) -> str | None:
    """Format a JSON-LD baseSalary object → human-readable string or None."""
    if not isinstance(base_salary, dict):
        return None
    currency = base_salary.get("currency", "")
    value = base_salary.get("value") or {}
    if not isinstance(value, dict):
        return None
    min_v = value.get("minValue")
    max_v = value.get("maxValue")
    unit = value.get("unitText", "")
    if min_v and max_v:
        return f"{currency} {min_v}–{max_v} {unit}".strip()
    if min_v:
        return f"{currency} {min_v}+ {unit}".strip()
    return None


def _normalise_company_name(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation, collapse whitespace.

    Does NOT strip domain-meaningful words like 'consulting', 'sports',
    'technology', etc. — those carry signal for matching.
    """
    s = name.lower().strip()
    s = re.sub(r"\s+", " ", s)
    for suffix in _STRIP_SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip()
            break
    # Strip trailing punctuation (comma, period, semicolon)
    s = s.rstrip(".,;:")
    return s.strip()


def _names_match(norm_source: str, norm_linkedin: str) -> bool:
    """Return True if the two normalised company names are equivalent.

    Handles exact match and single trailing-s variation
    (e.g. 'danu sport' == 'danu sports').
    """
    if norm_source == norm_linkedin:
        return True
    # Strip one trailing 's' from each and compare — handles Sport/Sports etc.
    s1 = norm_source[:-1] if norm_source.endswith("s") else norm_source
    s2 = norm_linkedin[:-1] if norm_linkedin.endswith("s") else norm_linkedin
    return s1 == s2 and s1 != ""


# ── Adapter ───────────────────────────────────────────────────────────────────

class LinkedInAdapter(BaseAdapter):
    """Scrape job listings from LinkedIn via Serper discovery + direct page fetch.

    Overrides BaseAdapter.run() to intercept LinkedIn-specific abort signals.
    Populates self._last_audit after each fetch() call for dry-run reporting.
    """

    platform = "linkedin"

    def __init__(self) -> None:
        api_key = os.getenv("SERPER_API_KEY", "")
        if not api_key:
            raise ValueError(
                "SERPER_API_KEY is not set — cannot initialize LinkedInAdapter. "
                "Add it to your .env file."
            )
        self._api_key: str = api_key
        self._session: requests.Session | None = None
        self._fetch_count: int = 0       # LinkedIn page GETs since last session init
        self.consecutive_999s: int = 0   # resets on any 200; shared across companies
        self.abort: bool = False         # set True on abort signals
        self._last_audit: dict = {}      # per-company detail for dry-run reporting

    # ── Session management ────────────────────────────────────────────────────

    def _get_session(self) -> requests.Session:
        """Return active Session; refresh (with sleep) every 25 LinkedIn fetches."""
        if self._session is None:
            self._session = requests.Session()
            self._fetch_count = 0
        elif self._fetch_count >= 25:
            log.info(
                "linkedin: session refresh after %d fetches — sleeping 60-90s",
                self._fetch_count,
            )
            self._session.close()
            time.sleep(random.uniform(60, 90))
            self._session = requests.Session()
            self._fetch_count = 0
        return self._session

    def close(self) -> None:
        """Release the underlying HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    # ── Stage 1: Serper discovery ─────────────────────────────────────────────

    def _discover_urls(self, source: dict) -> list[str]:
        """POST to Serper API; return up to 10 unique LinkedIn job-view URLs.

        Uses source['linkedin_search_name'] when set, otherwise company_name.
        Appends 'Ireland' to query for FDI companies.

        Raises _SerperAuthError on 401/403 (abort run).
        Raises _SerperRateLimitError on 429 (abort run).
        Raises _SerperNoResultsError when no LinkedIn job URLs found (skip company).
        """
        search_name = (
            source.get("linkedin_search_name")
            or source.get("company_name", "")
        )
        is_fdi = bool(source.get("is_fdi", False))
        is_irish_founded = bool(source.get("is_irish_founded", False))
        is_indigenous = not is_fdi or is_irish_founded

        if is_indigenous:
            query = f'site:linkedin.com/jobs/view "{search_name}"'
        else:
            query = f'site:linkedin.com/jobs/view "{search_name}" Ireland'

        log.debug("linkedin: Serper query: %r", query)

        try:
            resp = requests.post(
                _SERPER_URL,
                headers={
                    "X-API-KEY": self._api_key,
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": 10, "gl": "ie", "hl": "en"},
                timeout=15,
            )
        except requests.exceptions.RequestException as exc:
            log.warning("linkedin: Serper network error for '%s': %s", search_name, exc)
            raise _SerperNoResultsError(f"network error: {exc}") from exc

        if resp.status_code in (401, 403):
            raise _SerperAuthError(
                f"Serper HTTP {resp.status_code} — check SERPER_API_KEY"
            )
        if resp.status_code == 429:
            raise _SerperRateLimitError(
                "Serper HTTP 429 — free tier quota exhausted"
            )
        if resp.status_code != 200:
            log.warning(
                "linkedin: Serper HTTP %d for '%s' — skipping",
                resp.status_code, search_name,
            )
            raise _SerperNoResultsError(f"Serper HTTP {resp.status_code}")

        body = resp.json()
        organic = body.get("organic")

        if not organic:
            raise _SerperNoResultsError(f"no organic results for '{search_name}'")

        seen: set[str] = set()
        urls: list[str] = []
        for item in organic:
            raw_url = item.get("link", "")
            if not _JOB_VIEW_RE.match(raw_url):
                continue
            canonical = _canonical_url(raw_url)
            # Deduplicate by the canonical form (strips query params)
            if canonical in seen:
                continue
            seen.add(canonical)
            # Preserve original URL for subdomain extraction in stage 2
            urls.append(raw_url)
            if len(urls) >= 10:
                break

        if not urls:
            raise _SerperNoResultsError(
                f"Serper returned {len(organic)} organic results but none matched "
                f"linkedin.com/jobs/view pattern for '{search_name}'"
            )

        return urls

    # ── Stage 2: Domain filter ────────────────────────────────────────────────

    def _filter_by_domain(
        self, raw_urls: list[str], is_indigenous: bool
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Filter raw Serper URLs by LinkedIn subdomain.

        Indigenous companies: accept ie.linkedin.com and www.linkedin.com.
        FDI companies:        accept ie.linkedin.com only.

        Returns (accepted_canonical_urls, rejections)
        where rejections = [(original_url, reason_string), ...]
        """
        accepted: list[str] = []
        rejections: list[tuple[str, str]] = []

        for raw_url in raw_urls:
            subdomain = _extract_subdomain(raw_url)
            if is_indigenous:
                ok = subdomain in ("ie", "www")
            else:
                ok = subdomain == "ie"

            if ok:
                accepted.append(_canonical_url(raw_url))
            else:
                reason = f"wrong_subdomain ({subdomain}.linkedin.com)"
                log.debug("linkedin: REJECT domain: %s — %s", raw_url[:80], reason)
                rejections.append((raw_url, reason))

        return accepted, rejections

    # ── Stage 3: LinkedIn page fetch ──────────────────────────────────────────

    def _fetch_page(self, url: str) -> str | object | None:
        """GET one LinkedIn job page.

        Returns:
          - str: raw HTML on HTTP 200
          - _RATE_LIMITED sentinel: on 999/429 below abort threshold
          - None: on 404, timeout, connection error, other non-200
        Raises _RateLimitAbortError after 3 consecutive 999/429.
        Resets consecutive_999s on any 200.
        """
        session = self._get_session()
        headers = {**_LINKEDIN_HEADERS, "User-Agent": random.choice(_USER_AGENTS)}

        try:
            resp = session.get(url, headers=headers, timeout=10, allow_redirects=True)
        except requests.exceptions.Timeout:
            log.warning("linkedin: timeout %s", url)
            return None
        except requests.exceptions.RequestException as exc:
            log.warning("linkedin: connection error %s: %s", url, exc)
            return None

        self._fetch_count += 1
        time.sleep(random.uniform(1.5, 2.5))

        status = resp.status_code

        if status in (999, 429):
            self.consecutive_999s += 1
            log.warning(
                "linkedin: HTTP %d for %s (consecutive=%d)",
                status, url, self.consecutive_999s,
            )
            if self.consecutive_999s >= 3:
                raise _RateLimitAbortError(
                    f"{self.consecutive_999s} consecutive HTTP {status} responses"
                )
            return _RATE_LIMITED

        if status == 404:
            log.info("linkedin: 404 (job removed) %s", url)
            return None

        if status != 200:
            log.warning("linkedin: unexpected HTTP %d for %s", status, url)
            return None

        self.consecutive_999s = 0
        return resp.text

    # ── Stage 3 parsing ───────────────────────────────────────────────────────

    def _parse_page(self, url: str, html_text: str) -> dict | None:
        """Extract structured job data from LinkedIn HTML.

        Tries JSON-LD JobPosting schema first; falls back to BS4 for any fields
        not found in JSON-LD (including hiringOrganization name).

        Returns dict including '_hiring_org' key (internal, stripped before upsert)
        or None if title cannot be determined.
        """
        soup = BeautifulSoup(html_text, "html.parser")
        title = location_raw = summary = salary_range = hiring_org = None

        # ── Primary: JSON-LD JobPosting ───────────────────────────────────────
        ld_tag = soup.find("script", type="application/ld+json")
        if ld_tag and ld_tag.string:
            try:
                data = json.loads(ld_tag.string)
                if data.get("@type") == "JobPosting":
                    title = (data.get("title") or "").strip() or None

                    org = data.get("hiringOrganization") or {}
                    hiring_org = (org.get("name") or "").strip() or None

                    loc = data.get("jobLocation") or {}
                    if isinstance(loc, list):
                        loc = loc[0] if loc else {}
                    addr = loc.get("address") or {}
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("addressLocality"),
                            addr.get("addressRegion"),
                            addr.get("addressCountry"),
                        ]
                        location_raw = ", ".join(p for p in parts if p) or None
                    elif isinstance(addr, str):
                        location_raw = addr or None

                    desc_html = data.get("description") or ""
                    summary = _strip_html(desc_html) or None
                    salary_range = _format_salary(data.get("baseSalary"))

            except (json.JSONDecodeError, Exception) as exc:
                log.debug("linkedin: JSON-LD parse error for %s: %s", url, exc)

        # ── Fallback: BS4 selectors ───────────────────────────────────────────
        if not title:
            h1 = soup.find(
                "h1", class_=re.compile(r"top-card.*title|topcard.*title", re.I)
            ) or soup.find("h1")
            title = h1.get_text(strip=True) if h1 else None

        if not hiring_org:
            org_el = soup.find("a", class_=re.compile(r"topcard__org-name-link", re.I))
            if org_el:
                hiring_org = org_el.get_text(strip=True) or None

        if not location_raw:
            loc_el = soup.find(
                "span", class_=re.compile(r"topcard__flavor--bullet", re.I)
            ) or soup.find("span", class_=re.compile(r"topcard__flavor", re.I))
            if loc_el:
                location_raw = loc_el.get_text(strip=True) or None

        if not summary:
            desc_el = soup.find(
                "div",
                class_=re.compile(r"show-more-less-html__markup|description__text", re.I),
            )
            if desc_el:
                summary = _strip_html(desc_el.get_text(separator=" ", strip=True)) or None

        if not title:
            log.warning("linkedin: parse_error (no title) for %s", url)
            return None

        return {
            "url": url,
            "title": title.strip(),
            "location_raw": location_raw,
            "summary": summary,
            "salary_range": salary_range,
            "_hiring_org": hiring_org,  # internal — stripped before upsert
        }

    # ── Stage 4: Name validation ──────────────────────────────────────────────

    def _validate_name(
        self, search_name: str, hiring_org: str | None, url: str, *, override: bool = False
    ) -> tuple[bool, str]:
        """Return (True, '') if hiring_org matches search_name, else (False, reason).

        When override=True (linkedin_search_name was set on the source row):
          - Still rejects if hiring_org is absent or blank.
          - Skips the normalised equality check; trusts the operator override.
          - Logs the bypass at INFO level.

        When override=False: normalised equality with trailing-s tolerance (unchanged).
        """
        if not hiring_org or not hiring_org.strip():
            return False, "no_hiring_org"

        if override:
            log.info(
                "linkedin: name validation bypassed (override set): %r vs LinkedIn %r — %s",
                search_name, hiring_org, url,
            )
            return True, "bypassed"

        norm_source = _normalise_company_name(search_name)
        norm_linkedin = _normalise_company_name(hiring_org)

        if _names_match(norm_source, norm_linkedin):
            return True, ""

        reason = f"name_mismatch ({norm_source!r} != {norm_linkedin!r})"
        log.info("linkedin: REJECT %s — %s", url, reason)
        return False, reason

    # ── fetch() ───────────────────────────────────────────────────────────────

    def fetch(self, source: dict) -> list[dict]:
        """Four-stage fetch. Populates self._last_audit with per-URL detail.

        Propagates _SerperAuthError, _SerperRateLimitError → abort run.
        Propagates _SerperNoResultsError → skip company.
        Propagates _RateLimitAbortError → abort run.
        Returns list of validated, upsert-ready job dicts.
        """
        company_name = source.get("company_name", "")
        search_name = source.get("linkedin_search_name") or company_name
        is_fdi = bool(source.get("is_fdi", False))
        is_irish_founded = bool(source.get("is_irish_founded", False))
        is_indigenous = not is_fdi or is_irish_founded

        override = bool(source.get("linkedin_search_name"))

        audit: dict = {
            "serper_count": 0,
            "domain_accepted": 0,
            "fetch_succeeded": 0,
            "validated": 0,
            "bypassed": 0,       # validated via override (subset of validated)
            "rejections": [],    # list of (url, reason_string)
        }

        # Stage 1: Serper discovery
        # Raises _SerperAuthError, _SerperRateLimitError, _SerperNoResultsError
        raw_urls = self._discover_urls(source)
        audit["serper_count"] = len(raw_urls)

        # Stage 2: Domain filter
        filtered_urls, domain_rejections = self._filter_by_domain(raw_urls, is_indigenous)
        audit["domain_accepted"] = len(filtered_urls)
        audit["rejections"].extend(domain_rejections)

        # Stage 3 + 4: Fetch, parse, validate
        failed_999 = failed_http = failed_parse = failed_name = 0
        n_bypassed = 0
        jobs: list[dict] = []

        for url in filtered_urls:
            # Fetch — raises _RateLimitAbortError on 3 consecutive 999s
            result = self._fetch_page(url)

            if result is _RATE_LIMITED:
                failed_999 += 1
                audit["rejections"].append((url, "http_999"))
                continue
            if result is None:
                failed_http += 1
                audit["rejections"].append((url, "http_error"))
                continue

            # Parse
            parsed = self._parse_page(url, result)
            if parsed is None:
                failed_parse += 1
                audit["rejections"].append((url, "parse_error"))
                continue

            audit["fetch_succeeded"] += 1

            # Name validation (Stage 4)
            hiring_org = parsed.pop("_hiring_org", None)
            ok, outcome = self._validate_name(search_name, hiring_org, url, override=override)
            if not ok:
                failed_name += 1
                audit["rejections"].append((url, outcome))
                continue

            jobs.append(parsed)
            audit["validated"] += 1
            if outcome == "bypassed":
                n_bypassed += 1

        audit["bypassed"] = n_bypassed
        self._last_audit = audit

        log.info(
            "linkedin: '%s' serper=%d domain_filter=%d fetched=%d validated=%d "
            "errors: 999=%d parse=%d name_mismatch=%d bypassed=%d",
            company_name,
            audit["serper_count"],
            audit["domain_accepted"],
            audit["fetch_succeeded"],
            audit["validated"],
            failed_999,
            failed_parse,
            failed_name,
            n_bypassed,
        )

        return jobs

    # ── run() override ────────────────────────────────────────────────────────

    def run(self, source: dict) -> dict:
        """Fetch + upsert with LinkedIn-specific abort signal handling.

        _SerperAuthError / _SerperRateLimitError / _RateLimitAbortError
            → set self.abort=True, update source error, return stats
        _SerperNoResultsError
            → update source error, return stats (does NOT abort)
        """
        source_name = source.get("company_name") or source.get("id", "unknown")
        stats = {
            "source_name": source_name,
            "jobs_found": 0,
            "inserted": 0,
            "updated": 0,
            "reactivated": 0,
            "errors": 0,
        }

        try:
            jobs = self.fetch(source)

        except _SerperAuthError as exc:
            log.error("linkedin: Serper auth failure — aborting run: %s", exc)
            _update_source_error(source["id"], "serper_auth_failed")
            self.abort = True
            stats["errors"] += 1
            return stats

        except _SerperRateLimitError as exc:
            log.error("linkedin: Serper rate limit — aborting run: %s", exc)
            _update_source_error(source["id"], "serper_rate_limited")
            self.abort = True
            stats["errors"] += 1
            return stats

        except _SerperNoResultsError as exc:
            log.info("linkedin: '%s' — no results from Serper: %s", source_name, exc)
            _update_source_error(source["id"], "serper_no_results")
            return stats

        except _RateLimitAbortError as exc:
            log.error("linkedin: LinkedIn rate-limit abort during '%s': %s", source_name, exc)
            _update_source_error(source["id"], "linkedin_999")
            self.abort = True
            stats["errors"] += 1
            return stats

        except Exception as exc:
            log.error("linkedin: unexpected error for '%s': %s", source_name, exc)
            stats["errors"] += 1
            return stats

        stats["jobs_found"] = len(jobs)

        for job in jobs:
            url = (job.get("url") or "").strip()
            title = (job.get("title") or "").strip()

            if not url or not title:
                log.warning("linkedin: [%s] skipping job missing url/title", source_name)
                stats["errors"] += 1
                continue

            try:
                result = upsert_job(
                    url=url,
                    title=title,
                    source=self.platform,
                    sources_source_id=source["id"],
                    company_id=source["company_id"],
                    company_name=source.get("company_name", ""),
                    location_raw=job.get("location_raw"),
                    summary=job.get("summary"),
                    salary_range=job.get("salary_range"),
                )
                if not result:
                    stats["errors"] += 1
                elif result.get("was_inserted"):
                    stats["inserted"] += 1
                elif result.get("was_reactivated"):
                    stats["reactivated"] += 1
                else:
                    stats["updated"] += 1
            except Exception as exc:
                log.error("linkedin: upsert_job failed for '%s': %s", url[:80], exc)
                stats["errors"] += 1

        return stats
