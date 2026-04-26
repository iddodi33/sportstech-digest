"""eventbrite_ireland.py — discover event URLs from Eventbrite Ireland searches."""

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseEventAdapter, strip_tracking_params

log = logging.getLogger(__name__)

_SEARCH_URLS = [
    "https://www.eventbrite.ie/d/ireland/sport-events/",
    "https://www.eventbrite.ie/d/ireland/tech-events/",
    "https://www.eventbrite.ie/d/ireland/ai-events/",
    "https://www.eventbrite.ie/d/ireland/startups-events/",
]

# Matches both .ie and .com Eventbrite event detail URLs
_EVENT_URL_RE = re.compile(
    r"https://www\.eventbrite\.(?:ie|com)/e/[a-z0-9\-]+-tickets-\d+",
    re.IGNORECASE,
)


class EventbriteIrelandAdapter(BaseEventAdapter):
    """Discovers event URLs from 4 Eventbrite Ireland search categories."""

    source_name = "eventbrite_ireland"

    def discover_event_urls(self) -> list[str]:
        urls: list[str] = []

        for search_url in _SEARCH_URLS:
            try:
                resp = self._fetch(search_url)
                if resp.status_code != 200:
                    log.warning(
                        "[eventbrite_ireland] %s → HTTP %d", search_url, resp.status_code
                    )
                    continue

                found = self._extract_urls(resp.text)
                log.info(
                    "[eventbrite_ireland] %s → %d URLs", search_url, len(found)
                )
                urls.extend(found)

            except Exception as exc:
                log.warning("[eventbrite_ireland] fetch error for %s: %s", search_url, exc)

        return self._cap(urls)

    def _extract_urls(self, html: str) -> list[str]:
        urls: list[str] = []

        # Primary: parse <a> tags with BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if _EVENT_URL_RE.match(href.split("?")[0]):
                urls.append(strip_tracking_params(href))

        # Fallback: regex scan raw HTML for event URLs not visible in parsed links
        # (handles cases where Eventbrite renders href in data- attributes)
        for match in _EVENT_URL_RE.finditer(html):
            candidate = strip_tracking_params(match.group(0))
            if candidate not in urls:
                urls.append(candidate)

        # Normalise all to .ie domain for consistency
        return [u.replace("eventbrite.com/e/", "eventbrite.ie/e/") for u in urls]
