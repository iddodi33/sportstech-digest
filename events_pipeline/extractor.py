"""extractor.py — HTML-to-structured-event extractor using Claude Sonnet 4.5.

Core function:
  extract_event(url) -> dict
    Fetches the URL, cleans the HTML, calls Claude, returns a structured dict.
    Raises ExtractorError on hard failures (HTTP, JSON parse, Anthropic API).
    Returning relevance_category='not_relevant' is NOT an error.
"""

import json
import logging
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-5-20250929"
_MAX_TOKENS = 1500
_HTML_CHAR_LIMIT = 30_000
_FETCH_TIMEOUT = 15

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SYSTEM_PROMPT = """\
You are extracting structured event information from HTML for an Irish SportsTech intelligence platform called Sports D3c0d3d.
The platform curates events relevant to three categories:

sportstech: events covering sports technology, sports analytics, AI in sport, sports innovation, performance science, sports data, athlete tracking, fan engagement tech, sports media tech, esports tech, wearables, or sports business with a clear technology focus
ai_tech_ireland: general Irish AI, data, software, or technology events that aren't sport-specific but build the broader Irish tech ecosystem (e.g. Dublin Tech Summit, Engineers Ireland AI events, IEEE Ireland, machine learning meetups, AI ethics conferences)
startup_opportunity: accelerators, pitch competitions, founder programmes, Enterprise Ireland competitions, NDRC, Dogpatch Labs events, Local Enterprise Office programmes, founder networking with structured programmes

Events that are NOT relevant:

General sport events (matches, games, tournaments, league fixtures, championship finals)
Sponsorship breakfasts or commercial entertainment without speaker programmes
Press conferences, product launches, weekly match previews
Network events with no speaker programme or learning component
General business events without a tech, AI, or startup angle
Sports awards (unless explicitly tech-focused like SportsTech Ireland Awards)
Children's sport, mass participation events, recreational sport unless tech is the focus

Your output must be valid JSON only. No prose, no markdown fences. Just the JSON object.
Required schema:
{
  "name": string (event title),
  "date": string ISO YYYY-MM-DD or null (start date),
  "end_date": string ISO YYYY-MM-DD or null (set only if event spans multiple days),
  "start_time": string like "09:30" or null,
  "location": string (raw location text from page) or null,
  "area": one of ["Dublin", "Cork", "Galway", "Other Ireland", "Online", "Hybrid"] or null,
  "format": one of ["online", "in_person", "hybrid"] or null,
  "organiser": string or null,
  "description": string (2-4 sentences max, your own concise summary, not raw paste) or null,
  "image_url": string URL or null (use the OG image URL provided if no better image found in HTML),
  "recurrence": string like "monthly", "weekly" or null (only set if event explicitly recurs),
  "relevance_category": one of ["sportstech", "ai_tech_ireland", "startup_opportunity", "not_relevant"],
  "relevance_reason": string (one sentence explaining the category choice),
  "extraction_confidence": one of ["high", "medium", "low"]
}

Area mapping rules:
Dublin (city, county, suburbs like Dún Laoghaire, Tallaght) → "Dublin"
Cork (city, county) → "Cork"
Galway (city, county) → "Galway"
Anywhere else in Ireland (Belfast, Limerick, Kilkenny, etc.) → "Other Ireland"
Online-only events → "Online"
Hybrid events with both online and in-person components → "Hybrid"
Foreign events (London, Amsterdam, etc.) → null

Format inference:
"online", "virtual", "Zoom", "Teams", "webinar" → "online"
Physical venue named, no online option mentioned → "in_person"
Both physical and online options mentioned → "hybrid"

Date parsing:
Convert all dates to YYYY-MM-DD ISO format
"Tuesday 14 May 2026" → "2026-05-14"
"14-16 May 2026" → date: "2026-05-14", end_date: "2026-05-16"
"Q2 2026" → null (too vague)
If year is missing but context suggests current year, use current year
Today's date is provided in the user message for context

Confidence rubric:
"high": clear event detail page with explicit date, venue, programme
"medium": some ambiguity in date/venue but core event identifiable
"low": unclear if this is even an event detail page, dates inferred, or page is mostly a listicle mentioning the event in passing

If relevance_category is "not_relevant", still fill in the other fields as best you can. The platform will skip the event but the extraction is logged for audit.
If the page is clearly NOT an event page (e.g. it's an article, a general listing page, a homepage, a 404), return all fields as null and set relevance_category to "not_relevant" with relevance_reason explaining what the page actually is.\
"""


class ExtractorError(Exception):
    """Raised on hard failures: HTTP error, JSON parse failure, Anthropic API error."""


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_html(url: str) -> tuple[str, str | None]:
    """GET the URL and extract (raw_html, og_image_url).

    Raises ExtractorError on HTTP errors or connection failures.
    OG image is extracted before returning so clean_html can strip meta tags.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ExtractorError(f"HTTP fetch failed for {url!r}: {exc}") from exc

    html = resp.text

    # Extract OG / Twitter image before parsing further
    og_image: str | None = None
    try:
        soup = BeautifulSoup(html, "html.parser")
        for attr_name, attr_value in [
            ("property", "og:image"),
            ("name",     "twitter:image"),
            ("name",     "twitter:image:src"),
        ]:
            tag = soup.find("meta", attrs={attr_name: attr_value})
            if tag and tag.get("content"):
                og_image = tag["content"].strip() or None
                break
    except Exception:
        pass  # OG extraction is best-effort

    return html, og_image


# ── Clean ──────────────────────────────────────────────────────────────────────

def clean_html(html: str) -> str:
    """Parse HTML, strip noise, return the main content as an HTML string.

    Strips: <script>, <style>, <nav>, <footer>, <aside>, <header>.
    Prefers <main>, <article>, or [role=main]; falls back to <body>.
    Output truncated to _HTML_CHAR_LIMIT characters.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "nav", "footer", "aside", "header"]):
        tag.decompose()

    content = (
        soup.find("main")
        or soup.find("article")
        or soup.find(attrs={"role": "main"})
        or soup.find("body")
        or soup
    )

    result = str(content)
    if len(result) > _HTML_CHAR_LIMIT:
        result = result[:_HTML_CHAR_LIMIT]

    return result


# ── Extract ────────────────────────────────────────────────────────────────────

def extract_with_claude(
    cleaned_html: str,
    url: str,
    og_image_url: str | None,
) -> dict:
    """Send cleaned HTML to Claude Sonnet and parse the JSON response.

    Raises ExtractorError if the Anthropic call fails or the response is
    not valid JSON.
    """
    import anthropic
    import os

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    today = date.today().isoformat()
    og_str = og_image_url if og_image_url else "none"

    user_message = (
        f"Today's date: {today}\n"
        f"Source URL: {url}\n"
        f"OG image URL (if found): {og_str}\n\n"
        f"Cleaned HTML content:\n{cleaned_html}"
    )

    try:
        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:
        raise ExtractorError(f"Anthropic API call failed: {exc}") from exc

    raw = response.content[0].text.strip()

    # Strip markdown code fences if Claude added them despite instructions
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExtractorError(
            f"Claude response was not valid JSON: {exc}\nRaw response:\n{raw[:500]}"
        ) from exc


# ── Public entry point ─────────────────────────────────────────────────────────

def extract_event(url: str) -> dict:
    """Fetch, clean, and extract structured event data from a URL.

    Returns the Claude JSON dict with `url` injected at the top level.
    Raises ExtractorError on HTTP, API, or JSON parse failures.
    Returning relevance_category='not_relevant' is NOT an error.
    """
    log.info("Fetching %s", url)
    html, og_image_url = fetch_html(url)
    log.info("Cleaning HTML (%d chars raw)", len(html))
    cleaned = clean_html(html)
    log.info("Sending %d chars to Claude (%s)", len(cleaned), _MODEL)
    extraction = extract_with_claude(cleaned, url, og_image_url)
    extraction["url"] = url  # inject for downstream use and audit
    log.info(
        "Extracted: category=%s confidence=%s name=%r",
        extraction.get("relevance_category"),
        extraction.get("extraction_confidence"),
        extraction.get("name"),
    )
    return extraction
