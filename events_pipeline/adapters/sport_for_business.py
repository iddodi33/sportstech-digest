"""sport_for_business.py — discover event URLs from Sport for Business."""

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseEventAdapter, make_absolute, strip_tracking_params

log = logging.getLogger(__name__)

_LISTING_URLS = [
    "https://sportforbusiness.com/category/events/",
    "https://sportforbusiness.com/events/",
]

# Explicit blocklist of known nav/structural path slugs.
# Checked against the first path segment (after stripping leading slash).
_BLOCKLIST_RE = re.compile(
    r"^/(?:"
    r"about|member[^/]*|membership[^/]*|sponsor[^/]*|"
    r"featured-sports|featured-sectors|partner[^/]*|"
    r"contact|privacy[^/]*|terms[^/]*|login|signup|search|"
    r"the-bigger-picture|sport-for-business-podcast[^/]*|podcasts?|"
    r"event-listing[^/]*|"
    r"tag|category|author|page|wp-content|wp-admin|wp-json|feed"
    r")(?:/|$)",
    re.IGNORECASE,
)


class SportForBusinessAdapter(BaseEventAdapter):
    """Discovers event detail URLs from the Sport for Business events category.

    Primary strategy: single-segment slug URLs not on the blocklist.
    Fallback: any URL whose path contains /event or /events and isn't blocked.
    Quality over quantity — 0-10 URLs per run is expected.
    """

    source_name = "sport_for_business"

    def discover_event_urls(self) -> list[str]:
        html = self._fetch_listing()
        if not html:
            return []
        return self._parse_urls(html)

    def _fetch_listing(self) -> str | None:
        for url in _LISTING_URLS:
            try:
                resp = self._fetch(url)
                if resp.status_code == 200:
                    log.info("[sport_for_business] fetched listing: %s", url)
                    return resp.text
                log.debug("[sport_for_business] %s → HTTP %d", url, resp.status_code)
            except Exception as exc:
                log.warning("[sport_for_business] fetch error for %s: %s", url, exc)
        return None

    def _parse_urls(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        base = "https://sportforbusiness.com"

        primary: list[str] = []
        fallback: list[str] = []

        for a in soup.find_all("a", href=True):
            href: str = a["href"].strip()
            abs_url = make_absolute(href, base)

            if "sportforbusiness.com" not in abs_url:
                continue

            path = abs_url.split("sportforbusiness.com", 1)[-1]

            # Always skip blocklisted paths
            if _BLOCKLIST_RE.match(path):
                continue

            # Primary: single-depth slugs (/some-event-slug/)
            clean_path = path.strip("/")
            if clean_path and "/" not in clean_path and len(clean_path) >= 4:
                primary.append(strip_tracking_params(abs_url))

            # Fallback signal: any path containing /event or /events
            elif re.search(r"/events?(?:/|$)", path, re.IGNORECASE):
                fallback.append(strip_tracking_params(abs_url))

        if primary:
            log.info("[sport_for_business] %d primary slug URLs found", len(primary))
            return self._cap(primary)

        log.info(
            "[sport_for_business] 0 primary URLs; falling back to %d event-path URLs",
            len(fallback),
        )
        return self._cap(fallback)
