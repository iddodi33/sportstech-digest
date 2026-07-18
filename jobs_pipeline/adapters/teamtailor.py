"""teamtailor.py — Teamtailor ATS adapter.

Both sources (Boylesports, Stats Perform) use custom domains with no ats_slug.
The JSON:API at /jobs.json is blocked by section.io CDN from non-browser
clients (returns 406). The adapter falls back to HTML scraping of careers_url
on any network-level failure so Boylesports still returns jobs.
"""

import html as html_module
import logging
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .base import BaseAdapter

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_JSON_API_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/vnd.api+json",
}

_SUMMARY_MAX  = 2000   # JSON:API body field cap
_DETAIL_MAX   = 1500   # HTML detail page cap (matches linkedin adapter)
_DETAIL_SLEEP = 0.3    # seconds between detail page fetches

# Anchor texts that are navigation links, not job titles
_NAV_TITLES = {"all jobs", "view all", "see all", "see all jobs", "back", "see more", "load more"}


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace to plain text, capped at 2000 chars."""
    if not html:
        return ""
    try:
        unescaped = html_module.unescape(html)
        soup = BeautifulSoup(unescaped, "html.parser")
        text = soup.get_text(separator=" ")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:_SUMMARY_MAX]
    except Exception:
        return html[:_SUMMARY_MAX]


def _fetch_detail_text(url: str) -> str | None:
    """Fetch a Teamtailor job detail page and extract the description text.

    Tries specific description container selectors before falling back to the
    full <main> element. Used by the HTML fallback path when the JSON:API is
    blocked by the CDN.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("Teamtailor detail fetch failed for %s: %s", url[:80], exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    for sel in (
        "[data-controller='job-description']",
        "[data-testid='job-description']",
        ".content-text",
        "[class*='description']",
        "article",
    ):
        el = soup.select_one(sel)
        if el:
            text = re.sub(r"\s+", " ", el.get_text(separator=" ")).strip()
            if len(text) > 50:
                return text[:_DETAIL_MAX] or None

    # Last resort: full <main> text (includes breadcrumb noise, but still useful)
    main = soup.select_one("main")
    if main:
        text = re.sub(r"\s+", " ", main.get_text(separator=" ")).strip()
        return text[:_DETAIL_MAX] or None

    return None


class TeamtailorAdapter(BaseAdapter):
    """Adapter for Teamtailor public job boards.

    /jobs.json serves one of two shapes depending on the tenant:
      - JSON:API (`data`/`included`/`relationships`) — the format this
        adapter was originally written against.
      - JSON Feed 1.x (`items`, each carrying a Teamtailor-specific
        `_jobposting` schema.org JobPosting) — confirmed live for
        Boylesports as of 2026-07-14. Parsing this shape against the old
        JSON:API code silently read `data.get("data") == []` every time —
        0 jobs, no exception raised, so the HTML fallback below (which only
        triggers on HTTP/connection errors) never kicked in either. 7+ weeks
        of silent zero-yield before this was caught.
    Shape is auto-detected per fetch; fallback path (HTML scraping of
    careers_url) is used only when the endpoint itself is unreachable.

    Both known sources use custom domains (ats_slug is NULL); base URL is
    derived by stripping /jobs.json from ats_api_endpoint.
    """

    platform = "teamtailor"

    def fetch(self, source: dict) -> list[dict]:
        endpoint = source["ats_api_endpoint"]
        base_url = endpoint.replace("/jobs.json", "").rstrip("/")
        company = source.get("company_name", base_url)

        try:
            shape, raw_jobs, included = self._fetch_all(base_url)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            log.warning(
                "[%s] Teamtailor endpoint returned HTTP %s — falling back to HTML",
                company, status,
            )
            return self._fetch_html(source)
        except requests.ConnectionError as exc:
            log.warning(
                "[%s] Teamtailor endpoint connection failed (%s) — falling back to HTML",
                company, str(exc)[:120],
            )
            return self._fetch_html(source)

        if shape == "json_feed":
            log.info("[%s] JSON Feed returned %d jobs", company, len(raw_jobs))
            return self._normalise_json_feed(raw_jobs)

        log.info("[%s] JSON:API returned %d jobs", company, len(raw_jobs))
        return self._normalise(raw_jobs, included, base_url)

    # ------------------------------------------------------------------ #
    # JSON:API / JSON Feed path                                            #
    # ------------------------------------------------------------------ #

    def _fetch_all(self, base_url: str) -> tuple[str, list, list]:
        """Fetch /jobs.json, auto-detecting JSON:API vs JSON Feed 1.x shape.

        Returns (shape, raw_jobs, included) where shape is 'json_api' or
        'json_feed'. JSON:API paginates via page[number]/page[size] and
        accumulates `included` for relationship lookups (the query params
        are harmless no-ops against a JSON Feed tenant, which always
        returns its full item list in one response). JSON Feed has no
        `included` equivalent — returned as [] for that shape.
        """
        page_size = 30
        all_jobs: list = []
        all_included: list = []

        for page_num in range(1, 11):
            url = (
                f"{base_url}/jobs.json"
                f"?page[number]={page_num}&page[size]={page_size}"
            )
            resp = requests.get(url, headers=_JSON_API_HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if "items" in data and "data" not in data:
                return "json_feed", data.get("items") or [], []

            page_jobs = data.get("data") or []
            page_included = data.get("included") or []
            all_jobs.extend(page_jobs)
            all_included.extend(page_included)

            if len(page_jobs) < page_size:
                break

        return "json_api", all_jobs, all_included

    def _normalise_json_feed(self, items: list) -> list[dict]:
        """Convert Teamtailor's JSON Feed 1.x items into normalised job dicts.

        Each item carries a Teamtailor-specific `_jobposting` schema.org
        JobPosting object — richer than the JSON:API shape's bare location
        id, since it includes a structured jobLocation address directly
        (no relationship lookup needed).
        """
        jobs = []
        for item in items:
            title = (item.get("title") or "").strip()
            url = item.get("url") or ""
            if not title or not url:
                continue

            posting = item.get("_jobposting") or {}
            location_raw = None
            job_locations = posting.get("jobLocation")
            if isinstance(job_locations, list) and job_locations:
                addr = (job_locations[0] or {}).get("address") or {}
                parts = [
                    addr.get("addressLocality"),
                    addr.get("addressRegion"),
                    addr.get("addressCountry"),
                ]
                location_raw = ", ".join(p for p in parts if p) or None

            summary = _strip_html(item.get("content_html") or "") or None

            jobs.append({
                "url": url,
                "title": title,
                "location_raw": location_raw,
                "summary": summary,
                "salary_range": None,
            })

        return jobs

    def _normalise(
        self, raw_jobs: list, included: list, base_url: str
    ) -> list[dict]:
        """Convert JSON:API jobs + included into normalised job dicts.

        Only jobs with status='open' are returned.
        Location is resolved from the included array via relationships.
        """
        # Build lookup: (type, id) -> attributes for relationship resolution
        lookup: dict[tuple, dict] = {
            (item["type"], item["id"]): item.get("attributes", {})
            for item in included
            if "type" in item and "id" in item
        }

        jobs = []
        for job in raw_jobs:
            attrs = job.get("attributes") or {}
            if attrs.get("status") != "open":
                continue

            # Resolve location from relationships
            rels = job.get("relationships") or {}
            loc_data = (rels.get("location") or {}).get("data") or {}
            loc_id = loc_data.get("id")
            loc_type = loc_data.get("type", "locations")
            loc_attrs = lookup.get((loc_type, loc_id), {}) if loc_id else {}

            city = loc_attrs.get("city") or ""
            country = loc_attrs.get("country-name") or ""
            location_raw = ", ".join(p for p in (city, country) if p) or None

            summary = _strip_html(attrs.get("body") or "") or None

            job_id = job.get("id", "")
            url = f"{base_url}/jobs/{job_id}"

            jobs.append({
                "url": url,
                "title": (attrs.get("title") or "").strip(),
                "location_raw": location_raw,
                "summary": summary,
                "salary_range": None,
            })

        return jobs

    # ------------------------------------------------------------------ #
    # HTML fallback path                                                   #
    # ------------------------------------------------------------------ #

    def _fetch_html(self, source: dict) -> list[dict]:
        """Scrape job links from the public careers HTML page.

        Used when the JSON:API is unreachable (CDN blocking, DNS failure, etc.).
        Only returns title + url; location and summary are unavailable from HTML.
        Returns [] if the page is JS-rendered or otherwise yields no links.
        """
        careers_url = (source.get("careers_url") or "").strip()
        if not careers_url:
            log.warning(
                "[%s] HTML fallback: no careers_url in source record",
                source.get("company_name", "unknown"),
            )
            return []

        company = source.get("company_name", careers_url)
        try:
            resp = requests.get(
                careers_url,
                headers={"User-Agent": _USER_AGENT},
                timeout=30,
                allow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.error("[%s] HTML fallback GET failed: %s", company, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        seen: set[str] = set()
        jobs = []

        for a in soup.find_all("a", href=lambda h: h and "/jobs/" in h):
            href = a.get("href", "")
            if not href.startswith("http"):
                href = urljoin(resp.url, href)

            title = a.get_text(strip=True)
            if not title or title.lower() in _NAV_TITLES:
                continue
            if href in seen:
                continue
            seen.add(href)

            summary = _fetch_detail_text(href)
            jobs.append({
                "url": href,
                "title": title,
                "location_raw": None,
                "summary": summary,
                "salary_range": None,
            })
            time.sleep(_DETAIL_SLEEP)

        log.info("[%s] HTML fallback found %d jobs from %s", company, len(jobs), resp.url)
        return jobs
