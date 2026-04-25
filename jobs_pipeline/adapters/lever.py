"""lever.py — Lever ATS adapter."""

import logging

import requests

from .base import BaseAdapter

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SUMMARY_MAX = 2000


class LeverAdapter(BaseAdapter):
    """Adapter for Lever public job boards (no authentication required)."""

    platform = "lever"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Lever postings API and return normalised job dicts.

        Lever returns a top-level JSON array (not wrapped in a {jobs: []} object).
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
            log.warning("Lever: unexpected response shape from %s", endpoint)
            return []

        jobs = []
        for j in raw_jobs:
            categories = j.get("categories") or {}

            # Lever's `description` field is HTML; `descriptionPlain` and
            # `descriptionBodyPlain` are the plain-text equivalents.
            # Combine intro + body for a richer summary.
            intro = (j.get("descriptionPlain") or "").strip()
            body = (j.get("descriptionBodyPlain") or "").strip()
            summary_text = "\n\n".join(p for p in (intro, body) if p)
            summary = summary_text[:_SUMMARY_MAX] or None

            # salaryRange is an optional object {min, max, currency, interval}
            salary_range = None
            sr = j.get("salaryRange")
            if sr:
                currency = sr.get("currency") or ""
                min_val = sr.get("min") or ""
                max_val = sr.get("max") or ""
                interval = sr.get("interval") or ""
                salary_range = f"{currency} {min_val} - {max_val} {interval}".strip() or None

            jobs.append({
                "url": j.get("hostedUrl", ""),
                "title": (j.get("text") or "").strip(),
                "location_raw": categories.get("location") or None,
                "summary": summary,
                "salary_range": salary_range,
            })

        return jobs
