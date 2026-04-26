"""irish_diversity_in_tech.py — discover event URLs from the Irish Diversity in Tech aggregator."""

import logging

from bs4 import BeautifulSoup

from .base import (
    BaseEventAdapter,
    is_valid_http_url,
    make_absolute,
    strip_tracking_params,
)

log = logging.getLogger(__name__)

_LISTING_URL = "https://irish-diversity-in-tech.netlify.app/events/"

# Only return URLs hosted on known event platforms.
# This eliminates github.com, linkedin.com/company, etc. that the aggregator
# also links to but which aren't event detail pages.
_ALLOWED_DOMAINS = frozenset({
    "meetup.com",
    "www.meetup.com",
    "eventbrite.com",
    "eventbrite.ie",
    "www.eventbrite.com",
    "www.eventbrite.ie",
    "lu.ma",
    "hopin.com",
    "airmeet.com",
})


class IrishDiversityInTechAdapter(BaseEventAdapter):
    """Discovers external event URLs from the Irish Diversity in Tech events aggregator.

    Only returns URLs on known event-hosting platforms (Meetup, Eventbrite, lu.ma, etc.)
    to eliminate noise from github.com, linkedin.com/company, and similar links.
    May overlap with the meetup and eventbrite_ireland adapters; global dedup handles that.
    """

    source_name = "irish_diversity_in_tech"

    def discover_event_urls(self) -> list[str]:
        try:
            resp = self._fetch(_LISTING_URL)
            resp.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch {_LISTING_URL}: {exc}") from exc

        return self._parse_urls(resp.text)

    def _parse_urls(self, html: str) -> list[str]:
        from urllib.parse import urlparse
        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []

        for a in soup.find_all("a", href=True):
            href: str = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue

            abs_url = make_absolute(href, _LISTING_URL)
            if not is_valid_http_url(abs_url):
                continue

            domain = urlparse(abs_url).netloc
            if domain not in _ALLOWED_DOMAINS:
                continue

            cleaned = strip_tracking_params(abs_url)
            urls.append(cleaned)

        log.info("[irish_diversity_in_tech] %d event platform URLs found", len(urls))
        return self._cap(urls)
