"""workday.py — Workday ATS adapter.

Uses Workday's undocumented but stable POST /wday/cxs/{tenant}/{site}/jobs endpoint,
which is the same API Workday's own careers site JavaScript calls.

LOCATION FILTERING NOTE
-----------------------
The spec suggests using a `locationCountry` facet with a hardcoded Ireland UUID to
filter server-side. In practice, DraftKings' Workday tenant does not expose a
`locationCountry` facet (their available facets are jobFamilyGroup, workerSubType,
timeType, and locations). The spec's UUID bc33aa3152ec42d4995f4791a106ed09 returns
all jobs unfiltered for this tenant.

Decision: no location filter. We scrape all external postings (97 for DraftKings)
and rely on the downstream classifier to determine Ireland/remote relevance.
This is correct behaviour — a `locations` filter on the Dublin city ID would return
only 1 job and miss roles tagged "Remote" or "Multiple Locations".

Country UUIDs for reference if a future tenant does support locationCountry:
    Ireland:        bc33aa3152ec42d4995f4791a106ed09  (spec value — unverified)
    United Kingdom: 29247e57dbaf10568d32fbe8dbe0009e
    United States:  bc33aa3152ec42d4995f4791a1a0c4b9
"""

import html as html_module
import json
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from .base import BaseAdapter

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": _USER_AGENT,
}

_PAGE_SIZE    = 20
_MAX_PAGES    = 10
_DETAIL_MAX   = 1500
_DETAIL_SLEEP = 0.3


def _strip_html(raw: str) -> str:
    """Strip HTML tags and collapse whitespace, capped at _DETAIL_MAX chars."""
    if not raw:
        return ""
    try:
        unescaped = html_module.unescape(raw)
        soup = BeautifulSoup(unescaped, "html.parser")
        text = soup.get_text(separator=" ")
        text = re.sub(r"\s+", " ", text).strip()
        return text[:_DETAIL_MAX]
    except Exception:
        return raw[:_DETAIL_MAX]


def _fetch_detail_description(url: str) -> str | None:
    """Fetch a Workday job detail HTML page and extract description from JSON-LD.

    The Workday list API (POST /wday/cxs/.../jobs) does not include description
    in its response. The public HTML job page embeds a JSON-LD block with a
    'description' field containing the full HTML job description.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.debug("Workday detail fetch failed for %s: %s", url[:80], exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            ld = json.loads(script.string or "")
            desc = ld.get("description") or ""
            if desc:
                return _strip_html(desc) or None
        except Exception:
            continue

    return None


class WorkdayAdapter(BaseAdapter):
    """Adapter for Workday public job boards.

    Uses a POST request to the Workday CXS jobs endpoint, which returns paginated
    JSON with title, externalPath, locationsText, and postedOn per posting.
    Descriptions are not included in the list response — summary is None.
    """

    platform = "workday"

    def fetch(self, source: dict) -> list[dict]:
        """POST to the Workday jobs API and return all paginated results.

        Derives the endpoint URL from the source's workday_tenant, workday_pod,
        and workday_site fields. Raises ValueError if any are missing.
        Raises requests.HTTPError on 403/429/5xx.

        Description extraction: the list API does not include descriptions.
        A separate GET to each job's public HTML page extracts description
        from the embedded JSON-LD <script type="application/ld+json">.
        This adds one HTTP request per posting; throttled by _DETAIL_SLEEP.
        """
        tenant = source.get("workday_tenant")
        pod = source.get("workday_pod")
        site = source.get("workday_site")

        if not tenant or pod is None or not site:
            raise ValueError(
                f"Missing Workday fields for source {source.get('id')!r}: "
                f"tenant={tenant!r}, pod={pod!r}, site={site!r}"
            )

        endpoint = (
            f"https://{tenant}.wd{pod}.myworkdayjobs.com"
            f"/wday/cxs/{tenant}/{site}/jobs"
        )
        base_url = f"https://{tenant}.wd{pod}.myworkdayjobs.com/en-US/{site}"

        company = source.get("company_name", tenant)
        all_postings = []

        for page_num in range(1, _MAX_PAGES + 1):
            offset = (page_num - 1) * _PAGE_SIZE
            body = {
                "appliedFacets": {},
                "limit": _PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            }

            resp = requests.post(endpoint, json=body, headers=_HEADERS, timeout=30)

            if resp.status_code in (403, 429):
                raise requests.HTTPError(
                    f"Workday returned {resp.status_code} for {company} — "
                    f"rate-limited or blocked. Do not retry immediately.",
                    response=resp,
                )
            resp.raise_for_status()

            data = resp.json()
            page_postings = data.get("jobPostings") or []
            total = data.get("total", 0)

            all_postings.extend(page_postings)
            log.info(
                "[%s] page %d: offset=%d, got %d, total=%d, accumulated=%d",
                company, page_num, offset, len(page_postings), total, len(all_postings),
            )

            # Stop when last page (fewer results than requested, or reached total)
            if len(page_postings) < _PAGE_SIZE or len(all_postings) >= total:
                break

        jobs = []
        for p in all_postings:
            ext_path = p.get("externalPath") or ""
            url = f"{base_url}{ext_path}"

            summary = _fetch_detail_description(url)
            time.sleep(_DETAIL_SLEEP)

            jobs.append({
                "url": url,
                "title": (p.get("title") or "").strip(),
                "location_raw": p.get("locationsText") or None,
                "summary": summary,
                "salary_range": None,
            })

        return jobs
