"""rippling.py — Rippling ATS adapter.

Rippling exposes a clean public listings API per board slug:
    GET https://ats.rippling.com/api/v2/board/{slug}/jobs?page=0&pageSize=50

Response: {"items": [...], "page": N, "pageSize": N, "totalItems": N, "totalPages": N}

Each item has: id, name, url, department (dict), locations (array of dicts), language.
Descriptions are NOT in the listing response — summary is None for all rows.
Per-job detail fetches can be added later if descriptions become important.

Verified against: pff-careers (1 job), thriveglobal (4 jobs) — 2026-04-25.
"""

import logging

import requests

from .base import BaseAdapter

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_PAGE_SIZE = 50
_MAX_PAGES = 10


def _build_location(locations: list) -> str | None:
    """Return location_raw from Rippling's locations array.

    Takes the first location's 'name' field (e.g. 'Remote (United States)').
    Falls back to building from country + state if name is absent.
    """
    if not locations or not isinstance(locations, list):
        return None
    loc = locations[0]
    name = loc.get("name") or ""
    if name:
        return name
    country = loc.get("country") or ""
    state = loc.get("state") or ""
    parts = [p for p in (state, country) if p]
    return ", ".join(parts) or None


class RipplingAdapter(BaseAdapter):
    """Adapter for Rippling ATS public job boards.

    Paginates using the page parameter (0-indexed).
    Stops when page >= totalPages or when a page returns fewer items than pageSize.
    Descriptions are skipped (not in listing response) — summary is always None.
    """

    platform = "rippling"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Rippling jobs API and return normalised job dicts.

        Raises requests.HTTPError on 4xx/5xx so run() can catch and log it.
        Returns [] when the board has no open roles.
        """
        endpoint = source["ats_api_endpoint"]
        slug = source.get("ats_slug") or ""
        company = source.get("company_name", endpoint)

        all_items = []

        for page_num in range(_MAX_PAGES):
            resp = requests.get(
                endpoint,
                params={"page": page_num, "pageSize": _PAGE_SIZE},
                headers={
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                },
                timeout=30,
            )
            resp.raise_for_status()

            data = resp.json()
            items = data.get("items") or []
            total_items = data.get("totalItems", 0)
            total_pages = data.get("totalPages", 1)

            log.info(
                "[%s] Rippling page %d: got=%d totalItems=%d totalPages=%d",
                company, page_num, len(items), total_items, total_pages,
            )

            all_items.extend(items)

            if page_num >= total_pages - 1 or len(items) < _PAGE_SIZE:
                break

        jobs = []
        for item in all_items:
            item_id = item.get("id", "")
            url = item.get("url") or ""
            if not url and item_id and slug:
                url = f"https://ats.rippling.com/{slug}/jobs/{item_id}"

            jobs.append({
                "url": url,
                "title": (item.get("name") or "").strip(),
                "location_raw": _build_location(item.get("locations")),
                "summary": None,
                "salary_range": None,
            })

        return jobs
