"""ai_tinkerers_dublin.py — discover event URLs from the Dublin AI Tinkerers site."""

import logging
import re

from bs4 import BeautifulSoup

from .base import (
    BaseEventAdapter,
    is_valid_http_url,
    make_absolute,
    strip_tracking_params,
)

log = logging.getLogger(__name__)

_BASE_URL = "https://dublin.aitinkerers.org"
_LISTING_URL = "https://dublin.aitinkerers.org/"

# Post/event detail paths on this site typically look like /p/[slug]
_EVENT_PATH_RE = re.compile(r"^/p/[a-z0-9\-]+/?$", re.IGNORECASE)
_OWN_DOMAIN = "dublin.aitinkerers.org"


class AiTinkerersDublinAdapter(BaseEventAdapter):
    """Discovers event URLs from the Dublin AI Tinkerers homepage / event listings.

    The site uses Substack or similar, with event posts at /p/[slug].
    Expected volume: 1-3 events per run.
    """

    source_name = "ai_tinkerers_dublin"

    def discover_event_urls(self) -> list[str]:
        try:
            resp = self._fetch(_LISTING_URL)
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch {_LISTING_URL}: {exc}") from exc

        if resp.status_code == 403:
            # dublin.aitinkerers.org uses Cloudflare bot protection that blocks
            # all programmatic HTTP clients regardless of headers. Return empty
            # rather than failing the run — this adapter shows as a silent source.
            log.warning(
                "[ai_tinkerers_dublin] 403 Cloudflare block on %s — "
                "returning 0 URLs (headless browser required to bypass)",
                _LISTING_URL,
            )
            return []

        if resp.status_code != 200:
            raise RuntimeError(
                f"Unexpected HTTP {resp.status_code} from {_LISTING_URL}"
            )

        return self._parse_urls(resp.text)

    def _parse_urls(self, html: str) -> list[str]:
        from urllib.parse import urlparse
        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []

        for a in soup.find_all("a", href=True):
            href: str = a["href"].strip()
            if not href or href.startswith("#") or href.startswith("mailto:"):
                continue

            abs_url = make_absolute(href, _BASE_URL)
            if not is_valid_http_url(abs_url):
                continue

            parsed = urlparse(abs_url)

            # Only include URLs from our own domain
            if parsed.netloc.lstrip("www.") != _OWN_DOMAIN.lstrip("www."):
                continue

            # Must match the /p/[slug] event detail pattern
            if not _EVENT_PATH_RE.match(parsed.path):
                continue

            cleaned = strip_tracking_params(abs_url)
            urls.append(cleaned)

        # Also scan raw HTML for /p/ paths in case they're in data attributes or JS
        for match in re.finditer(r'href="(/p/[a-z0-9\-]+/?)(?:["\?])', html, re.IGNORECASE):
            abs_url = _BASE_URL + match.group(1)
            cleaned = strip_tracking_params(abs_url)
            if cleaned not in urls:
                urls.append(cleaned)

        log.info("[ai_tinkerers_dublin] %d event URLs found", len(urls))
        return self._cap(urls)
