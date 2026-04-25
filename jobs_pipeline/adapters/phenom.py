"""phenom.py — Phenom People ATS adapter.

Phenom serves tenant-specific job boards at custom career site URLs.
The standard listing API is:
    GET {careers_url}/api/apply/v2/jobs?lang=en_us&pagesize=50&from=0

The response wraps data in: {"status": "success", "data": {"totalHits": N, "results": [...]}}

KNOWN ISSUE — BLIZZARD: careers.blizzard.com returns {"status": "failure",
"errorMsg": "Tenant not identified"} for all standard GET requests. Blizzard's
Phenom instance uses a private widget-based API that requires server-side tenant
identification not exposed via the public REST endpoint. The adapter raises on
this failure so the run script logs it clearly. Investigation needed before
Blizzard can be scraped via this adapter.
"""

import html as html_module
import logging
import re

import requests
from bs4 import BeautifulSoup

from .base import BaseAdapter

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SUMMARY_MAX = 2000
_PAGE_SIZE = 50
_MAX_PAGES = 10


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


class PhenomAdapter(BaseAdapter):
    """Adapter for Phenom People public job boards.

    Uses the standard tenant-hosted GET API:
        {ats_api_endpoint}?lang=en_us&pagesize={PAGE_SIZE}&from={offset}

    Paginates using the 'from' offset parameter until all results are fetched
    or 10 pages have been retrieved (safety cap).

    Response shape: {"status": "success", "data": {"totalHits": N, "results": [...]}}
    Each result: Id, title, applyUrl, locationName, city, country, description, jobSeqNo
    """

    platform = "phenom"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Phenom jobs API and return normalised job dicts.

        Raises requests.HTTPError on 4xx/5xx.
        Raises RuntimeError on Phenom application-level failure (e.g. 'Tenant not identified').
        Returns [] when the board has no open roles.
        """
        endpoint = source["ats_api_endpoint"]
        company = source.get("company_name", endpoint)

        all_results = []

        for page_num in range(1, _MAX_PAGES + 1):
            offset = (page_num - 1) * _PAGE_SIZE
            resp = requests.get(
                endpoint,
                params={"lang": "en_us", "pagesize": _PAGE_SIZE, "from": offset},
                headers={
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                    "Referer": source.get("careers_url", ""),
                },
                timeout=30,
            )
            resp.raise_for_status()

            body = resp.json()
            status = body.get("status")

            if status != "success":
                error_msg = body.get("errorMsg") or body.get("error") or "unknown error"
                raise RuntimeError(
                    f"[{company}] Phenom API returned status={status!r}: {error_msg}. "
                    f"If 'Tenant not identified', this tenant uses a non-standard "
                    f"widget-based configuration not supported by this adapter."
                )

            data = body.get("data") or {}
            total_hits = data.get("totalHits", 0)
            results = data.get("results") or data.get("jobs") or []

            log.info(
                "[%s] Phenom page %d: offset=%d got=%d total=%d accumulated=%d",
                company, page_num, offset, len(results), total_hits, len(all_results) + len(results),
            )

            all_results.extend(results)

            if len(results) < _PAGE_SIZE or len(all_results) >= total_hits:
                break

        jobs = []
        for j in all_results:
            job_id = j.get("Id") or j.get("id") or ""

            apply_url = j.get("applyUrl") or j.get("apply_url") or ""
            if not apply_url and job_id:
                slug = source.get("ats_slug", "")
                careers_base = (source.get("careers_url") or "").rstrip("/")
                apply_url = f"{careers_base}/job/{job_id}" if careers_base else ""

            # Location: prefer locationName, fall back to city + country
            location_raw = j.get("locationName") or None
            if not location_raw:
                city = j.get("city") or ""
                country = j.get("country") or ""
                combined = ", ".join(p for p in (city, country) if p)
                location_raw = combined or None

            summary = _strip_html(j.get("description") or "") or None

            jobs.append({
                "url": apply_url,
                "title": (j.get("title") or "").strip(),
                "location_raw": location_raw,
                "summary": summary,
                "salary_range": None,
            })

        return jobs
