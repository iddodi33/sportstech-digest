"""ashby.py — Ashby ATS adapter."""

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


def _strip_html(html: str) -> str:
    """Strip HTML tags and collapse whitespace to plain text, capped at 2000 chars.

    Used only as a fallback when descriptionPlain is absent. Ashby descriptionHtml
    is real HTML (not entity-escaped like Greenhouse), but we unescape first for safety.
    """
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


def _build_location(job: dict) -> str | None:
    """Return location_raw from the flat location string, falling back to address fields."""
    loc = job.get("location")
    if loc and isinstance(loc, str):
        return loc.strip() or None

    addr = (job.get("address") or {}).get("postalAddress") or {}
    parts = [
        addr.get("addressLocality"),
        addr.get("addressRegion"),
        addr.get("addressCountry"),
    ]
    combined = ", ".join(p for p in parts if p)
    return combined or None


class AshbyAdapter(BaseAdapter):
    """Adapter for Ashby public job boards (no authentication required)."""

    platform = "ashby"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Ashby job board API and return normalised job dicts.

        Appends ?includeCompensation=true to get salary data.
        Raises requests.HTTPError on 4xx/5xx so run() can catch and log it.
        Returns [] when the board exists but has no open roles.
        """
        endpoint = source["ats_api_endpoint"]
        url = endpoint if "?" in endpoint else endpoint + "?includeCompensation=true"

        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()

        data = resp.json()
        raw_jobs = data.get("jobs") or []

        jobs = []
        for j in raw_jobs:
            # Plain text preferred; HTML strip is a fallback
            description_plain = (j.get("descriptionPlain") or "").strip()
            if description_plain:
                summary = description_plain[:_SUMMARY_MAX] or None
            else:
                summary = _strip_html(j.get("descriptionHtml") or "") or None

            # Capture salary only when the company has opted in to displaying it
            salary_range = None
            if j.get("shouldDisplayCompensationOnJobPostings"):
                comp = j.get("compensation") or {}
                salary_range = comp.get("compensationTierSummary") or None

            jobs.append({
                "url": j.get("jobUrl", ""),
                "title": (j.get("title") or "").strip(),
                "location_raw": _build_location(j),
                "summary": summary,
                "salary_range": salary_range,
            })

        return jobs
