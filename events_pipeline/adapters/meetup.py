"""meetup.py — discover event URLs from Meetup group listing pages."""

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseEventAdapter, strip_tracking_params

log = logging.getLogger(__name__)

MEETUP_GROUPS = [
    "ai-in-action-dublin",
    "dublin-ai-developers-group",
    "machine-learning-dublin",
    "Dublin-Data-Science",
    "Dublin-Data-Science-ODSC",
    "python-ireland",
    "pyladies-dublin",
    "stripe-dublin",
]


def _event_url_re(group: str) -> re.Pattern[str]:
    slug = re.escape(group)
    return re.compile(
        rf"https://www\.meetup\.com/{slug}/events/\d+/?",
        re.IGNORECASE,
    )


# Generic Meetup event URL pattern for raw-HTML fallback scan
_ANY_MEETUP_EVENT_RE = re.compile(
    r"https://www\.meetup\.com/([\w\-]+)/events/(\d+)/?",
    re.IGNORECASE,
)


class MeetupAdapter(BaseEventAdapter):
    """Discovers upcoming event URLs from a curated list of Irish tech Meetup groups."""

    source_name = "meetup"

    def discover_event_urls(self) -> list[str]:
        urls: list[str] = []

        for group in MEETUP_GROUPS:
            listing_url = f"https://www.meetup.com/{group}/events/"
            try:
                resp = self._fetch(listing_url)
                if resp.status_code in (403, 404, 410):
                    log.warning(
                        "[meetup] %s → HTTP %d (group inactive/private, skipping)",
                        group, resp.status_code,
                    )
                    continue
                if resp.status_code != 200:
                    log.warning("[meetup] %s → HTTP %d", group, resp.status_code)
                    continue

                found = self._extract_event_urls(resp.text, group)
                log.info("[meetup] %s → %d URLs", group, len(found))
                urls.extend(found)

            except Exception as exc:
                log.warning("[meetup] fetch error for group %r: %s", group, exc)

        return self._cap(urls)

    def _extract_event_urls(self, html: str, group: str) -> list[str]:
        urls: list[str] = []
        group_pattern = _event_url_re(group)

        # Primary: BeautifulSoup <a> tags
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].split("?")[0]
            if group_pattern.match(href):
                urls.append(strip_tracking_params(a["href"]))

        # Fallback: raw HTML regex scan (handles JS-inlined hrefs)
        if not urls:
            for match in _ANY_MEETUP_EVENT_RE.finditer(html):
                if match.group(1).lower() == group.lower():
                    candidate = f"https://www.meetup.com/{match.group(1)}/events/{match.group(2)}/"
                    cleaned = strip_tracking_params(candidate)
                    if cleaned not in urls:
                        urls.append(cleaned)

        return urls
