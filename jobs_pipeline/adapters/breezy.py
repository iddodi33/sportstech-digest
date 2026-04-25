"""breezy.py — Breezy ATS adapter."""

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


def _build_location(loc: dict) -> str | None:
    """Build a location string from Breezy's nested location object.

    Structure: {city: str, state: {name}, country: {name}}
    """
    if not loc or not isinstance(loc, dict):
        return None
    parts = []
    if loc.get("city"):
        parts.append(loc["city"])
    state = loc.get("state") or {}
    if isinstance(state, dict) and state.get("name"):
        parts.append(state["name"])
    country = loc.get("country") or {}
    if isinstance(country, dict) and country.get("name"):
        parts.append(country["name"])
    return ", ".join(parts) or None


class BreezyAdapter(BaseAdapter):
    """Adapter for Breezy public job boards (no authentication required).

    The /json endpoint returns only published positions (no state filtering needed).
    The description field is not always present in the list response.
    """

    platform = "breezy"

    def fetch(self, source: dict) -> list[dict]:
        """GET the Breezy /json endpoint and return normalised job dicts.

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
            log.warning("Breezy: unexpected response shape from %s", endpoint)
            return []

        jobs = []
        for j in raw_jobs:
            description_html = j.get("description") or ""
            summary = _strip_html(description_html) or None

            jobs.append({
                "url": j.get("url", ""),
                "title": (j.get("name") or "").strip(),
                "location_raw": _build_location(j.get("location")),
                "summary": summary,
                "salary_range": None,
            })

        return jobs
