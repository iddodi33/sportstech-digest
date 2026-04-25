"""personio.py — Personio ATS adapter."""

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

_SUMMARY_MAX = 2000

# Extract subdomain slug from endpoint URL, e.g. "output-sports" from
# "https://output-sports.jobs.personio.com/search.json"
_SLUG_RE = re.compile(r"https?://([^.]+)\.jobs\.personio\.")


def _slug_from_endpoint(endpoint: str) -> str:
    m = _SLUG_RE.match(endpoint)
    return m.group(1) if m else ""


class PersonioAdapter(BaseAdapter):
    """Adapter for Personio public job boards (no authentication required).

    The search.json endpoint returns a flat array of job objects.
    Job descriptions are not included in the list response (they render
    server-side), so summary will be None until a future detail-fetch pass.
    The public posting URL is constructed as:
        https://{slug}.jobs.personio.com/job/{id}
    """

    platform = "personio"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Personio search.json and return normalised job dicts.

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

        raw_jobs = resp.json()
        if not isinstance(raw_jobs, list):
            log.warning("Personio: unexpected response shape from %s", endpoint)
            return []

        if not raw_jobs:
            log.warning(
                "Personio board for '%s' returned 0 jobs — board may be empty or "
                "using a non-default language code.",
                source.get("company_name", endpoint),
            )
            return []

        slug = source.get("ats_slug") or _slug_from_endpoint(endpoint)

        jobs = []
        for j in raw_jobs:
            job_id = j.get("id")
            url = f"https://{slug}.jobs.personio.com/job/{job_id}"

            # search.json returns description as a plain string that is empty
            # for most boards (descriptions are rendered server-side only)
            description = (j.get("description") or "").strip()
            summary = description[:_SUMMARY_MAX] or None

            jobs.append({
                "url": url,
                "title": (j.get("name") or "").strip(),
                "location_raw": j.get("office") or None,
                "summary": summary,
                "salary_range": None,
            })

        return jobs
