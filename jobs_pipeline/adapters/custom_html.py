"""custom_html.py — generic careers-page scraper for sources with no ATS.

Covers active company_careers_sources rows where ats_platform='custom_html':
companies that publish roles on their own site rather than through an ATS or
LinkedIn. Before this adapter existed the discovery script could emit a
`custom_html` classification but nothing in jobs_pipeline could consume it,
so those rows were scraped by nothing at all.

Deliberately conservative. A generic HTML page has no schema, so a loose
parser invents jobs out of nav links and footers. Two passes, most
trustworthy first, and the first pass that yields anything wins:

  1. JSON-LD — schema.org/JobPosting blocks. Structured and unambiguous.
  2. Anchors — links whose href looks like a job-detail URL AND whose text
               reads like a job title.

If neither yields anything, return []. An empty list is the correct answer
for a careers page with no current openings, and is far cheaper than a false
positive: every job here flows on to the Haiku classifier and then to a human
review queue.

Titles are run through relevance_filter.check_relevance, matching the two
LinkedIn adapters, so page furniture that survives parsing ("Join our talent
community") is still dropped before upsert.

Anything the page renders client-side is invisible here — this uses a plain
HTTP fetch, no JS execution. Such a source is better marked
needs_manual_review than silently reported as an empty board.
"""

import html as html_module
import json
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .base import BaseAdapter
from ..relevance_filter import check_relevance

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_TIMEOUT = 25
_SUMMARY_MAX = 2000

# Cap per company. A careers page yielding more than this is almost certainly
# being mis-parsed (nav/blog links), so it is safer to log and truncate.
_MAX_JOBS = 40

# href fragments that mark a link as pointing at a single job posting.
_JOB_HREF_RE = re.compile(
    r"(?:/|-|_)(?:job|jobs|career|careers|vacancy|vacancies|position|positions|"
    r"opening|openings|role|roles|opportunit\w*)(?:/|-|_|\?|$)",
    re.IGNORECASE,
)

# Known ATS detail URLs that can appear embedded in an otherwise custom page.
_ATS_HREF_RE = re.compile(
    r"(boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com|"
    r"\.teamtailor\.com|\.recruitee\.com|\.breezy\.hr|\.bamboohr\.com|"
    r"jobs\.personio\.|smartrecruiters\.com|seemehired\.com)",
    re.IGNORECASE,
)

# Link text that is navigation, not a job title.
_NAV_TEXT_RE = re.compile(
    r"^(?:careers?|jobs?|open roles?|open positions?|vacancies|all jobs|"
    r"view all|see all|apply|apply now|learn more|read more|back|home|"
    r"contact|contact us|about|about us|our team|join us|work with us|"
    r"life at .+|benefits|culture|sign in|log ?in|register|next|previous)$",
    re.IGNORECASE,
)

# A plausible job title: not a sentence, not a single stray word.
_MIN_TITLE_LEN = 3
_MAX_TITLE_LEN = 120


def _strip_html(value: str) -> str:
    """Strip tags and collapse whitespace, capped at _SUMMARY_MAX chars."""
    if not value:
        return ""
    try:
        soup = BeautifulSoup(html_module.unescape(value), "html.parser")
        text = soup.get_text(separator=" ")
    except Exception:
        text = value
    return re.sub(r"\s+", " ", text).strip()[:_SUMMARY_MAX]


def _clean_title(text: str) -> str:
    """Collapse whitespace in candidate title text."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _looks_like_title(text: str) -> bool:
    """Reject nav furniture, sentences and stray words."""
    if not (_MIN_TITLE_LEN <= len(text) <= _MAX_TITLE_LEN):
        return False
    if _NAV_TEXT_RE.match(text):
        return False
    # A job title is a phrase; long prose and multiple sentences are not.
    if text.count(".") > 1 or len(text.split()) > 14:
        return False
    return any(ch.isalpha() for ch in text)


def _title_from_anchor(anchor) -> str:
    """Pull the job title out of an anchor.

    Card-style listings wrap the whole card in one <a>, so the anchor's own
    text is "Title + blurb + tags" — far too long to pass _looks_like_title.
    Prefer, in order: a heading, an element whose class names it a title, or
    the first text-bearing block inside the anchor. Fall back to the full
    anchor text for plain <a>Job Title</a> links.
    """
    heading = anchor.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading:
        text = _clean_title(heading.get_text(" ", strip=True))
        if _looks_like_title(text):
            return text

    titled = anchor.find(
        class_=lambda c: c and "title" in " ".join(
            c if isinstance(c, list) else [c]
        ).lower()
    )
    if titled:
        text = _clean_title(titled.get_text(" ", strip=True))
        if _looks_like_title(text):
            return text

    for child in anchor.find_all(["p", "div", "span", "strong", "b"]):
        text = _clean_title(child.get_text(" ", strip=True))
        if text and _looks_like_title(text):
            return text

    return _clean_title(anchor.get_text(" ", strip=True))


def _absolutise(href: str, base_url: str) -> str | None:
    """Resolve href against base_url; return None for non-HTTP targets."""
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    absolute = urljoin(base_url, href)
    if urlparse(absolute).scheme not in ("http", "https"):
        return None
    return absolute


def _location_from_jsonld(node: dict) -> str | None:
    """Flatten schema.org jobLocation into a display string."""
    loc = node.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return _clean_title(loc) or None
    if not isinstance(loc, dict):
        return None

    address = loc.get("address")
    if isinstance(address, str):
        return _clean_title(address) or None
    if not isinstance(address, dict):
        return None

    flat: list[str] = []
    for key in ("addressLocality", "addressRegion", "addressCountry"):
        part = address.get(key)
        if isinstance(part, dict):
            part = part.get("name")
        if isinstance(part, str) and part.strip():
            flat.append(part.strip())
    return ", ".join(flat) or None


def _iter_jsonld_nodes(soup: BeautifulSoup):
    """Yield every dict inside the page's ld+json blocks, @graph included."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue

        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def _parse_jsonld(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Pass 1 — schema.org/JobPosting blocks."""
    jobs: list[dict] = []
    for node in _iter_jsonld_nodes(soup):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if not any(str(t).lower() == "jobposting" for t in types):
            continue

        title = _clean_title(node.get("title") or node.get("name") or "")
        if not title:
            continue

        url = _absolutise(node.get("url") or node.get("@id") or "", page_url) or page_url
        jobs.append({
            "url": url,
            "title": title,
            "location_raw": _location_from_jsonld(node),
            "summary": _strip_html(node.get("description") or "") or None,
            "salary_range": None,
        })
    return jobs


def _parse_anchors(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """Pass 2 — anchors that look like links to individual job postings."""
    jobs: list[dict] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not (_JOB_HREF_RE.search(href) or _ATS_HREF_RE.search(href)):
            continue

        url = _absolutise(href, page_url)
        if not url or url.rstrip("/") == page_url.rstrip("/"):
            continue

        title = _title_from_anchor(anchor)
        if not _looks_like_title(title):
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        jobs.append({
            "url": url,
            "title": title,
            "location_raw": None,
            "summary": None,
            "salary_range": None,
        })
    return jobs


class CustomHTMLAdapter(BaseAdapter):
    """Adapter for company-owned careers pages with no ATS behind them.

    Reads careers_url from the source row. Returns [] (not an error) when the
    page loads but advertises no roles — the common, correct case for a small
    company between hires.
    """

    platform = "custom_html"

    def fetch(self, source: dict) -> list[dict]:
        """GET careers_url and return normalised job dicts.

        Raises requests.HTTPError on 4xx/5xx so run() records the failure.
        Returns [] for a reachable page with no parseable openings.
        """
        source_name = source.get("company_name") or source.get("id", "unknown")
        careers_url = (source.get("careers_url") or "").strip()

        if not careers_url:
            raise ValueError("custom_html source has no careers_url")

        resp = requests.get(
            careers_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        # Drop scripts/styles so their contents cannot surface as link text,
        # but keep ld+json blocks — that is where pass 1 reads from.
        for tag in soup(["script", "style", "noscript"]):
            if tag.name == "script" and tag.get("type") == "application/ld+json":
                continue
            tag.decompose()

        page_url = str(resp.url)

        jobs = _parse_jsonld(soup, page_url)
        strategy = "json-ld"
        if not jobs:
            jobs = _parse_anchors(soup, page_url)
            strategy = "anchors"

        if not jobs:
            log.info("custom_html: [%s] no openings parsed from %s", source_name, page_url)
            return []

        kept: list[dict] = []
        for job in jobs:
            is_relevant, reason = check_relevance(job["title"], source_id=source.get("id"))
            if not is_relevant:
                log.debug(
                    "custom_html: [%s] dropped '%s' (%s)",
                    source_name, job["title"], reason,
                )
                continue
            kept.append(job)

        if len(kept) > _MAX_JOBS:
            log.warning(
                "custom_html: [%s] %d candidates from %s exceeds cap %d "
                "— page is probably being mis-parsed, truncating",
                source_name, len(kept), page_url, _MAX_JOBS,
            )
            kept = kept[:_MAX_JOBS]

        log.info(
            "custom_html: [%s] %d job(s) via %s from %s",
            source_name, len(kept), strategy, page_url,
        )
        return kept
