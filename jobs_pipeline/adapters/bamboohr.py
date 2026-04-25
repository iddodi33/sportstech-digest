"""bamboohr.py — BambooHR ATS adapter.

Despite the spec suggesting HTML parsing, the /careers/list endpoint returns
JSON directly: {"meta": {"totalCount": N}, "result": [...]}.
"""

import logging
import re

import requests

from .base import BaseAdapter

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SLUG_RE = re.compile(r"https?://([^.]+)\.bamboohr\.com")


def _slug_from_endpoint(endpoint: str) -> str:
    m = _SLUG_RE.match(endpoint)
    return m.group(1) if m else ""


def _build_location(job: dict) -> str | None:
    if job.get("isRemote"):
        return "Remote"
    loc = job.get("location") or {}
    city = loc.get("city") or ""
    state = loc.get("state") or ""
    parts = [p for p in (city, state) if p]
    return ", ".join(parts) or None


class BambooHRAdapter(BaseAdapter):
    """Adapter for BambooHR public job boards.

    The /careers/list endpoint returns JSON (not HTML), with shape:
        {"meta": {"totalCount": N}, "result": [{id, jobOpeningName, ...}]}
    Job detail URLs follow: https://{slug}.bamboohr.com/careers/{id}
    Descriptions are only on individual job pages — summary is None for now.
    """

    platform = "bamboohr"

    def fetch(self, source: dict) -> list[dict]:
        """GET the BambooHR careers list and return normalised job dicts.

        Raises requests.HTTPError on 4xx/5xx so run() can catch and log it.
        Returns [] when the board has no open roles.
        """
        endpoint = source["ats_api_endpoint"]

        resp = requests.get(
            endpoint,
            headers={"User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()

        data = resp.json()
        raw_jobs = data.get("result") or []

        if not raw_jobs:
            log.info(
                "BambooHR board for '%s' returned 0 jobs (totalCount=%s)",
                source.get("company_name", endpoint),
                data.get("meta", {}).get("totalCount", "?"),
            )
            return []

        slug = source.get("ats_slug") or _slug_from_endpoint(endpoint)

        jobs = []
        for j in raw_jobs:
            job_id = j.get("id")
            url = f"https://{slug}.bamboohr.com/careers/{job_id}"

            jobs.append({
                "url": url,
                "title": (j.get("jobOpeningName") or "").strip(),
                "location_raw": _build_location(j),
                "summary": None,
                "salary_range": None,
            })

        return jobs
