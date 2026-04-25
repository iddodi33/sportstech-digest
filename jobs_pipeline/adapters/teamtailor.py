"""teamtailor.py — Teamtailor ATS adapter.

Both sources (Boylesports, Stats Perform) use custom domains with no ats_slug.
The JSON:API at /jobs.json is blocked by section.io CDN from non-browser
clients (returns 406). The adapter falls back to HTML scraping of careers_url
on any network-level failure so Boylesports still returns jobs.
"""

import html as html_module
import logging
import re
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

_SUMMARY_MAX = 2000

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


class TeamtailorAdapter(BaseAdapter):
    """Adapter for Teamtailor public job boards.

    Primary path: JSON:API at /jobs.json (JSON:API format with relationships).
    Fallback path: HTML scraping of careers_url when the JSON:API is unreachable.

    Both sources use custom domains (ats_slug is NULL); base URL is derived
    by stripping /jobs.json from ats_api_endpoint.
    """

    platform = "teamtailor"

    def fetch(self, source: dict) -> list[dict]:
        endpoint = source["ats_api_endpoint"]
        base_url = endpoint.replace("/jobs.json", "").rstrip("/")
        company = source.get("company_name", base_url)

        try:
            raw_jobs, included = self._fetch_all_pages(base_url)
            log.info("[%s] JSON:API returned %d jobs", company, len(raw_jobs))
            return self._normalise(raw_jobs, included, base_url)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            log.warning(
                "[%s] Teamtailor JSON:API returned HTTP %s — falling back to HTML",
                company, status,
            )
        except requests.ConnectionError as exc:
            log.warning(
                "[%s] Teamtailor JSON:API connection failed (%s) — falling back to HTML",
                company, str(exc)[:120],
            )

        return self._fetch_html(source)

    # ------------------------------------------------------------------ #
    # JSON:API path                                                        #
    # ------------------------------------------------------------------ #

    def _fetch_all_pages(self, base_url: str) -> tuple[list, list]:
        """Fetch all paginated pages from the Teamtailor JSON:API.

        Returns (all job dicts, all included dicts) accumulated across pages.
        Stops when a page returns fewer records than page_size (last page).
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

            page_jobs = data.get("data") or []
            page_included = data.get("included") or []
            all_jobs.extend(page_jobs)
            all_included.extend(page_included)

            if len(page_jobs) < page_size:
                break

        return all_jobs, all_included

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

            jobs.append({
                "url": href,
                "title": title,
                "location_raw": None,
                "summary": None,
                "salary_range": None,
            })

        log.info("[%s] HTML fallback found %d jobs from %s", company, len(jobs), resp.url)
        return jobs
