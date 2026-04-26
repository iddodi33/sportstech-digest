"""base.py — abstract base class and shared utilities for event discovery adapters."""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

log = logging.getLogger(__name__)

_MAX_URLS_PER_ADAPTER = 30
_FETCH_TIMEOUT = 15
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Full browser-like headers used by _fetch(). Sites that return 403 to bare
# User-Agent strings (e.g. aitinkerers.org) typically accept these.
# Accept-Encoding is intentionally omitted — letting requests negotiate it
# automatically avoids brotli-encoded responses that requests can't decompress
# without the optional brotli package.
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IE,en;q=0.9",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Query params that are pure tracking noise — stripped before returning URLs.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS_EXACT = frozenset({
    "_gl", "recId", "recSource", "searchId", "eventOrigin",
    "aff", "fbclid", "gclid",
})


# ── URL utilities ──────────────────────────────────────────────────────────────

def strip_tracking_params(url: str) -> str:
    """Remove tracking query parameters from a URL, preserving canonical form."""
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        params = parse_qs(parsed.query, keep_blank_values=True)
        clean = {
            k: v for k, v in params.items()
            if k not in _TRACKING_PARAMS_EXACT
            and not any(k.startswith(p) for p in _TRACKING_PARAM_PREFIXES)
        }
        return urlunparse(parsed._replace(query=urlencode(clean, doseq=True)))
    except Exception:
        return url


def make_absolute(href: str, base_url: str) -> str:
    """Resolve a potentially relative href against the page's base URL."""
    return urljoin(base_url, href)


def is_valid_http_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


# ── Dataclass / base class ────────────────────────────────────────────────────

@dataclass
class AdapterResult:
    source_name: str
    urls_discovered: list[str] = field(default_factory=list)
    error: str | None = None
    runtime_seconds: float = 0.0


class BaseEventAdapter(ABC):
    """Abstract base for all event source adapters.

    Subclasses implement discover_event_urls() and set source_name.
    run() wraps discovery with timing and error handling.
    _fetch(url) provides a shared HTTP GET with full browser headers.
    """

    DEFAULT_HEADERS: dict[str, str] = _DEFAULT_HEADERS

    def _fetch(self, url: str):
        """GET url with browser-like headers and 15s timeout.

        Returns a requests.Response. Does not raise on HTTP errors — callers
        check resp.status_code or call resp.raise_for_status() themselves.
        Propagates requests.RequestException (connection/timeout failures).
        """
        import requests
        return requests.get(
            url,
            headers=self.DEFAULT_HEADERS,
            timeout=_FETCH_TIMEOUT,
            allow_redirects=True,
        )

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Identifier matching the upsert_event source param."""
        ...

    @abstractmethod
    def discover_event_urls(self) -> list[str]:
        """Fetch listing page(s), extract and return absolute event detail URLs.

        Rules (enforced by implementations):
        - Absolute URLs only (use make_absolute()).
        - Strip tracking params (use strip_tracking_params()).
        - Dedupe within the returned list.
        - Skip obvious non-detail pages (/tag/, /category/, /author/, /page/).
        - Hard cap: call _cap(urls) before returning.
        """
        ...

    def _cap(self, urls: list[str]) -> list[str]:
        """Dedupe, then warn and cap if over _MAX_URLS_PER_ADAPTER."""
        seen: set[str] = set()
        deduped = [u for u in urls if not (u in seen or seen.add(u))]  # type: ignore[func-returns-value]
        if len(deduped) > _MAX_URLS_PER_ADAPTER:
            log.warning(
                "[%s] capping %d discovered URLs to %d",
                self.source_name, len(deduped), _MAX_URLS_PER_ADAPTER,
            )
            return deduped[:_MAX_URLS_PER_ADAPTER]
        return deduped

    def run(self) -> AdapterResult:
        """Discover event URLs with timing and exception handling."""
        import time
        start = time.time()
        result = AdapterResult(source_name=self.source_name)
        try:
            result.urls_discovered = self.discover_event_urls()
            log.info(
                "[%s] discovered %d URLs in %.1fs",
                self.source_name, len(result.urls_discovered), time.time() - start,
            )
        except Exception as exc:
            result.error = str(exc)
            log.error("[%s] adapter failed: %s", self.source_name, exc)
        result.runtime_seconds = time.time() - start
        return result
