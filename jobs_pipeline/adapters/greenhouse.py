"""greenhouse.py — Greenhouse ATS adapter."""

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

    Greenhouse returns content with HTML-entity-escaped markup (e.g. &lt;div&gt;),
    so we unescape entities first before feeding to BeautifulSoup.
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


class GreenhouseAdapter(BaseAdapter):
    """Adapter for Greenhouse public job boards (no authentication required)."""

    platform = "greenhouse"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Greenhouse jobs board API and return normalised job dicts.

        Appends ?content=true to include job descriptions.
        Raises requests.HTTPError on 4xx/5xx so run() can catch and log it.
        Returns [] when the board exists but has no open roles.
        """
        endpoint = source["ats_api_endpoint"]
        url = endpoint if "?" in endpoint else endpoint + "?content=true"

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
            loc = j.get("location")
            if isinstance(loc, dict):
                location_raw = loc.get("name") or None
            elif isinstance(loc, str):
                location_raw = loc or None
            else:
                location_raw = None

            summary = _strip_html(j.get("content") or "") or None

            jobs.append({
                "url": j.get("absolute_url", ""),
                "title": (j.get("title") or "").strip(),
                "location_raw": location_raw,
                "summary": summary,
                "salary_range": None,
            })

        return jobs
