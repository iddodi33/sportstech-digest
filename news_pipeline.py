"""
news_pipeline.py
Fetches sportstech news from direct site RSS and Google News RSS queries.
Deduplicates by URL and saves to news_raw_YYYY-MM.json.
"""

import calendar
import json
import os
import logging
import platform
import re
import socket
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import cloudscraper
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- tuneable constants ---------------------------------------------------
CUTOFF_DAYS          = 40   # Google News feeds
SITE_RSS_CUTOFF_DAYS = 35   # Site RSS feeds (stricter — avoids old bulk content)

# Set True to bypass the date filter entirely for diagnosis.
DEBUG_SKIP_DATE_FILTER = False
# -------------------------------------------------------------------------

# bebeez.eu geo-filter: keep only articles with an Ireland/UK signal in the title
_BEBEEZ_GEO_TERMS = {
    "ireland", "irish", "dublin", "cork", "galway",
    "limerick", "belfast", "waterford", "uk", "britain",
}

# --- tiered per-source caps (applied per feed URL) -----------------------
HIGH_QUALITY_SOURCES = {
    "sportforbusiness.com",
    "siliconrepublic.com",
    "thinkbusiness.ie",
    "bebeez.eu",
}
MEDIUM_QUALITY_SOURCES = {
    "businessplus.ie",
    "techcentral.ie",
    "sportsbusinessjournal.com",
    "irishrugby.ie",
    "gaa.ie",
    "irishmirror.ie",
    "businesswire.com",
    "gov.ie",
}
LOW_QUALITY_SOURCES = {
    "irishtechnews.ie",
    "limerickleader.ie",
    "advertiser.ie",
}

# High-volume broadsheets: full feeds require strict keyword filtering
# to prevent non-sportstech content flooding results.
BROADSHEET_SOURCES = {
    "independent.ie",
    "irishtimes.com",
    "irishexaminer.com",
}

_BROADSHEET_KEYWORDS = {
    "sport", "sports", "sportstech", "tech", "startup", "funding",
    "digital", "gaa", "rugby", "football", "soccer", "esports",
    "fitness", "stadium", "arena", "wearable", "data", "analytics",
    "ai", "innovation", "ireland",
}

CAP_HIGH           = 15
CAP_BUSINESSPOST   = 10  # cloudscraper direct scrape
CAP_BROADSHEET     = 5   # strict cap for high-volume broadsheets
CAP_MEDIUM         = 5
CAP_LOW            = 3
CAP_GOOGLE_NEWS    = 10
# -------------------------------------------------------------------------

SITE_RSS_FEEDS = [
    # High quality — sportstech focused
    "https://www.siliconrepublic.com/feed",
    "https://sportforbusiness.com/feed",
    "https://www.thinkbusiness.ie/feed/",
    "https://businessplus.ie/feed/",
    "https://www.techcentral.ie/feed/",
    "http://feeds.feedburner.com/IrishTechNews",  # irishtechnews.ie official Feedburner feed
    "https://bebeez.eu/feed/",
    # RSS attempted first, scraped on failure (see SCRAPE_FALLBACK)
    "https://www.sportireland.ie/rss",
]

# Maps specific feed URLs to their logical source name (used for labelling and cap lookup)
_FEED_URL_SOURCE = {
    "http://feeds.feedburner.com/IrishTechNews": "irishtechnews.ie",
}

GOOGLE_NEWS_FEEDS = [
    # Ireland-specific
    "https://news.google.com/rss/search?q=sportstech+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=sports+technology+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=sports+startup+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=irish+sports+tech+funding&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=sports+analytics+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=digital+sport+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=fan+engagement+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=fitness+startup+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=stadium+technology+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=esports+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=wellness+startup+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    # Named Irish companies
    "https://news.google.com/rss/search?q=kitman+labs&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=output+sports&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=orreco+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=statsports&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=tixserve&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=enterprise+ireland+sports&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=sport+ireland+technology&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=wiistream+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=clubforce+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=trojantrack&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=sports+impact+technologies+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=feenix+group+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=anyscor+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=locker+app+ireland+sports&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=precision+sports+technology+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=headhawk+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=teamfeepay+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=clubspot+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=revelate+fitness+ireland&hl=en-IE&gl=IE&ceid=IE:en",
    # Ecosystem people (broader queries — no forced co-occurrence, no "linkedin" keyword)
    "https://news.google.com/rss/search?q=%22Keith+Brock%22+sportstech&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Keith+Brock%22+%22Enterprise+Ireland%22+sport&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Rob+Hartnett%22+sport&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Aim%C3%A9e+Williams%22+sportstech&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Aimee+Williams%22+sportstech&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Trev+Keane%22+Feenix&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Colin+Deering%22+sport&hl=en-IE&gl=IE&ceid=IE:en",
    # Named entity queries (more specific — surface articles that single-name queries miss)
    "https://news.google.com/rss/search?q=Kitman+Labs+funding&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=STATSports+%22Northern+Ireland%22&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=Orreco+%22sports+science%22&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=Clubforce+Ireland+sport&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=Leinster+Rugby+%22data+analytics%22&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=IRFU+technology&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=FAI+technology&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=GAA+analytics+technology&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=Munster+Rugby+data&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=Connacht+Rugby+analytics&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=Ulster+Rugby+technology&hl=en-IE&gl=IE&ceid=IE:en",
    # Europe sportstech
    "https://news.google.com/rss/search?q=sportstech+europe+startup&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=sports+technology+funding+europe&hl=en-IE&gl=IE&ceid=IE:en",
    # Source-name keyword queries
    "https://news.google.com/rss/search?q=%22Irish+Times%22+sports+technology&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Irish+Independent%22+sportstech&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Irish+Examiner%22+sport+tech&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=%22Business+Post%22+sportstech&hl=en-IE&gl=IE&ceid=IE:en",
    # Business Post site: queries — redundant coverage via Google News index
    "https://news.google.com/rss/search?q=site%3Abusinesspost.ie+sportstech&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=site%3Abusinesspost.ie+sport+technology&hl=en-IE&gl=IE&ceid=IE:en",
    "https://news.google.com/rss/search?q=site%3Abusinesspost.ie+%22sports+tech%22+Ireland&hl=en-IE&gl=IE&ceid=IE:en",
]

# Domains whose RSS is malformed or absent — scraped as fallback when RSS yields 0 entries.
SCRAPE_FALLBACK = {
    "thinkbusiness.ie": "https://www.thinkbusiness.ie",
    "sportireland.ie":  "https://www.sportireland.ie/news",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# Feed fetcher with socket-level timeout (works on Windows SSL)
# ---------------------------------------------------------------------------

_SOCKET_TIMEOUT = 10  # seconds; applied at socket level so it works on Windows


def fetch_feed_fresh(url: str):
    """
    Fetch an RSS feed via feedparser with a socket-level timeout.
    Using socket.setdefaulttimeout() rather than requests.get() correctly
    interrupts hanging SSL connections on Windows where urllib/feedparser
    can stall indefinitely on certain TLS handshakes.
    """
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(_SOCKET_TIMEOUT)
        return feedparser.parse(url)
    except Exception as exc:
        log.warning("fetch_feed_fresh failed for %s (%s)", url[:80], exc)
        return None
    finally:
        socket.setdefaulttimeout(old)


def google_news_responsive() -> bool:
    """Quick pre-flight check: can we get at least one entry from Google News RSS?"""
    test_url = "https://news.google.com/rss/search?q=test&hl=en-IE&gl=IE&ceid=IE:en"
    old = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(8)
        feed = feedparser.parse(test_url)
        return len(feed.entries) > 0
    except Exception:
        return False
    finally:
        socket.setdefaulttimeout(old)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _domain(url: str) -> str:
    match = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return match.group(1) if match else url


def _cap_for(url: str, feed_type: str) -> int:
    """Return the article cap for a given feed URL and type."""
    if feed_type == "google_news":
        return CAP_GOOGLE_NEWS
    domain = _FEED_URL_SOURCE.get(url, _domain(url))
    if domain in BROADSHEET_SOURCES:
        return CAP_BROADSHEET
    if domain in HIGH_QUALITY_SOURCES:
        return CAP_HIGH
    if domain in MEDIUM_QUALITY_SOURCES:
        return CAP_MEDIUM
    if domain in LOW_QUALITY_SOURCES:
        return CAP_LOW
    return CAP_HIGH  # unknown site RSS — treat as high quality


def label_for(url: str, feed_type: str) -> str:
    if url in _FEED_URL_SOURCE:
        return _FEED_URL_SOURCE[url]
    if feed_type == "google_news":
        match = re.search(r"[?&]q=([^&]+)", url)
        return f"GNews: {match.group(1).replace('+', ' ')}" if match else "Google News"
    match = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return match.group(1) if match else url


def _parse_date_entry(entry) -> datetime | None:
    """Return a timezone-aware datetime from a feedparser entry, or None."""
    for field in ("published", "updated"):
        raw = getattr(entry, field, None)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None)
        if parsed:
            try:
                ts = calendar.timegm(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                pass
    return None


def _parse_date_str(raw: str) -> datetime | None:
    """Try to parse an arbitrary date string. Returns None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:len(fmt) + 5], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    return None


def _within_cutoff(pub_dt: datetime | None, feed_type: str = "google_news") -> bool:
    if pub_dt is None:
        return True
    if DEBUG_SKIP_DATE_FILTER:
        return True
    days = SITE_RSS_CUTOFF_DAYS if feed_type == "site_rss" else CUTOFF_DAYS
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    return pub_dt >= cutoff


# ---------------------------------------------------------------------------
# lxml RSS fallback
# ---------------------------------------------------------------------------

def _parse_feed_lxml(url: str) -> list[dict]:
    """
    Attempt to parse an RSS/Atom feed with lxml when feedparser returns nothing.
    Returns a list of raw dicts with keys: title, link, pub, desc.
    """
    try:
        from lxml import etree
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError:
            parser = etree.XMLParser(recover=True)
            root = etree.fromstring(resp.content, parser=parser)

        entries = []

        # RSS 2.0
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link")  or "").strip()
            pub   = (item.findtext("pubDate") or "").strip()
            desc  = (item.findtext("description") or "").strip()
            if title and link:
                entries.append({"title": title, "link": link, "pub": pub, "desc": desc})

        # Atom
        if not entries:
            ns = "http://www.w3.org/2005/Atom"
            for entry in root.findall(f".//{{{ns}}}entry"):
                title   = (entry.findtext(f"{{{ns}}}title") or "").strip()
                link_el = entry.find(f"{{{ns}}}link")
                link    = link_el.get("href", "") if link_el is not None else ""
                pub     = (entry.findtext(f"{{{ns}}}published")
                           or entry.findtext(f"{{{ns}}}updated") or "").strip()
                summary = (entry.findtext(f"{{{ns}}}summary") or "").strip()
                if title and link:
                    entries.append({"title": title, "link": link, "pub": pub, "desc": summary})

        return entries
    except Exception as exc:
        log.debug("lxml parse failed for %s: %s", url, exc)
        return []


# ---------------------------------------------------------------------------
# HTML scrape fallback
# ---------------------------------------------------------------------------

def _scrape_articles(url: str, source_label: str, failed_sources: list) -> tuple[list[dict], dict]:
    """
    Scrape a news listing page for article links when RSS is unavailable.
    Returns (articles, stats).
    """
    empty_stats = {"total_entries": 0, "date_dropped": 0, "unknown_date": 0, "kept": 0}
    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        log.warning("SCRAPE FAILED %s (%s): %s", source_label, url, exc)
        failed_sources.append({"source": source_label, "url": url, "error": f"scrape: {exc}"})
        return [], empty_stats

    containers = (
        soup.find_all("article")
        or soup.select(".post, .entry, .news-item, .article-card, .teaser")
        or soup.select("li.news, li.post")
    )

    articles = []
    date_dropped = 0
    unknown_date = 0

    for tag in containers[:40]:
        anchor = None
        for heading in tag.find_all(["h2", "h3", "h4"]):
            anchor = heading.find("a", href=True)
            if anchor:
                break
        if not anchor:
            anchor = tag.find("a", href=True)
        if not anchor:
            continue

        title = _strip_html(anchor.get_text())
        href  = anchor["href"]
        if not title or not href:
            continue
        if not href.startswith("http"):
            href = urljoin(url, href)

        pub_dt = None
        time_el = tag.find("time")
        if time_el:
            pub_dt = _parse_date_str(time_el.get("datetime") or time_el.get_text())
        if pub_dt is None:
            for cls in ("date", "published", "post-date", "entry-date", "article-date"):
                el = tag.find(class_=re.compile(cls, re.I))
                if el:
                    pub_dt = _parse_date_str(el.get_text())
                    if pub_dt:
                        break

        if pub_dt is None:
            unknown_date += 1
        elif not _within_cutoff(pub_dt):
            date_dropped += 1
            continue

        snippet = _strip_html((tag.find("p") or tag).get_text())[:300]

        articles.append({
            "title":   title,
            "link":    href,
            "pubDate": pub_dt.isoformat() if pub_dt else "Unknown",
            "source":  source_label,
            "snippet": snippet,
        })

    stats = {
        "total_entries": len(containers),
        "date_dropped":  date_dropped,
        "unknown_date":  unknown_date,
        "kept":          len(articles),
    }
    log.info("Scraped %s: %d containers → %d kept", source_label, len(containers), len(articles))
    return articles, stats


# ---------------------------------------------------------------------------
# Enterprise Ireland direct scraper
# ---------------------------------------------------------------------------

def _scrape_enterprise_ireland(failed_sources: list) -> list[dict]:
    """
    Custom scraper for enterprise-ireland.com/en/news.
    Their news listing uses <a href="/en/news/<slug>"> links with <h4> titles,
    date blocks formatted as "16th April 2026", and short description paragraphs.
    """
    base_url = "https://www.enterprise-ireland.com"
    url = f"{base_url}/en/news"
    source_label = "enterprise-ireland.com"

    try:
        resp = requests.get(url, timeout=10, headers=_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        log.warning("SCRAPE FAILED %s (%s): %s", source_label, url, exc)
        failed_sources.append({"source": source_label, "url": url, "error": f"scrape: {exc}"})
        return []

    articles = []
    seen_links: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "javascript:void" in href:
            continue
        if "/en/news/" not in href:
            continue
        if href.startswith("/"):
            href = base_url + href
        if href in seen_links:
            continue
        seen_links.add(href)

        # Walk up to find the containing card
        card = a.find_parent(["div", "li", "article", "section"])

        # Title: prefer h4 inside the card, fall back to the anchor text
        title = ""
        if card:
            h4 = card.find("h4")
            title = h4.get_text(strip=True) if h4 else a.get_text(strip=True)
        else:
            title = a.get_text(strip=True)
        title = title.strip()
        if not title or len(title) < 5:
            continue

        # Date: look for "16th April 2026" style text in the card
        pub_dt = None
        if card:
            for el in card.find_all(string=True):
                text = el.strip()
                m = re.search(r'(\d{1,2})(?:st|nd|rd|th)\s+(\w+)\s+(\d{4})', text, re.I)
                if m:
                    try:
                        pub_dt = datetime.strptime(
                            f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
                        ).replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
                    break

        if pub_dt is not None and not _within_cutoff(pub_dt, "site_rss"):
            continue

        # Snippet: first <p> in the card
        snippet = ""
        if card:
            p = card.find("p")
            if p:
                snippet = p.get_text(strip=True)[:300]

        articles.append({
            "title":   title,
            "link":    href,
            "pubDate": pub_dt.isoformat() if pub_dt else "Unknown",
            "source":  source_label,
            "snippet": snippet,
        })

    log.info("Enterprise Ireland scrape: %d articles from %d unique links", len(articles), len(seen_links))
    return articles


# ---------------------------------------------------------------------------
# Business Post scraper (cloudscraper — bypasses Cloudflare bot protection)
# ---------------------------------------------------------------------------

def _scrape_businesspost(failed_sources: list) -> list[dict]:
    """
    Scrape https://www.businesspost.ie/tech/ using cloudscraper to bypass
    Cloudflare bot protection. Plain requests and feedparser both return 403.
    """
    url          = "https://www.businesspost.ie/tech/"
    source_label = "businesspost.ie"

    try:
        scraper  = cloudscraper.create_scraper()
        resp     = scraper.get(url, timeout=20)
        resp.raise_for_status()
        soup     = BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        log.warning("SCRAPE FAILED %s via cloudscraper (%s): %s", source_label, url, exc)
        failed_sources.append({"source": source_label, "url": url, "error": f"cloudscraper: {exc}"})
        return []

    articles   = []
    seen_links: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href:
            continue
        # Only article paths — /news/ or /tech/ slugs, not nav/section links
        if not re.search(r"/(?:news|tech)/[^/]{5,}", href):
            continue
        if not href.startswith("http"):
            href = "https://www.businesspost.ie" + href
        if href in seen_links:
            continue
        seen_links.add(href)

        card  = a.find_parent(["article", "div", "li", "section"])
        title = ""
        if card:
            for heading in card.find_all(["h2", "h3", "h4"]):
                title = heading.get_text(strip=True)
                if title:
                    break
        if not title:
            title = a.get_text(strip=True)
        title = title.strip()
        if not title or len(title) < 10:
            continue

        # Date
        pub_dt = None
        if card:
            time_el = card.find("time")
            if time_el:
                pub_dt = _parse_date_str(
                    time_el.get("datetime", "") or time_el.get_text(strip=True)
                )

        if pub_dt is not None and not _within_cutoff(pub_dt, "site_rss"):
            continue

        snippet = ""
        if card:
            p = card.find("p")
            if p:
                snippet = p.get_text(strip=True)[:300]

        articles.append({
            "title":   title,
            "link":    href,
            "pubDate": pub_dt.isoformat() if pub_dt else "Unknown",
            "source":  source_label,
            "snippet": snippet,
        })

    log.info("Business Post cloudscraper: %d articles from %s", len(articles), url)
    return articles


# ---------------------------------------------------------------------------
# Primary RSS fetcher
# ---------------------------------------------------------------------------

def _date_range(articles: list[dict]) -> tuple[str, str]:
    """Return (oldest, newest) pubDate strings from an article list, or ('—','—')."""
    dates = [a["pubDate"] for a in articles if a["pubDate"] not in ("Unknown", "")]
    if not dates:
        return "—", "—"
    return min(dates)[:10], max(dates)[:10]


def fetch_feed(
    url: str,
    source_label: str,
    failed_sources: list,
    feed_type: str = "google_news",
) -> tuple[list[dict], dict]:
    """
    Parse one RSS/Atom feed. If feedparser returns 0 entries and the domain
    is in SCRAPE_FALLBACK, tries lxml then HTML scraping.

    Returns (articles, stats).
    stats keys: total_entries, date_dropped, unknown_date, kept, method,
                oldest_date, newest_date
    """
    empty_stats = {
        "total_entries": 0, "date_dropped": 0, "unknown_date": 0,
        "kept": 0, "method": "rss", "oldest_date": "—", "newest_date": "—",
    }
    is_bebeez     = _domain(url) == "bebeez.eu"
    is_broadsheet = _domain(url) in BROADSHEET_SOURCES

    try:
        # Both paths use socket-level timeout; fetch_feed_fresh wraps the same logic
        if feed_type == "google_news":
            feed = fetch_feed_fresh(url)
            if feed is None:
                raise ValueError("fetch_feed_fresh returned None")
        else:
            _old = socket.getdefaulttimeout()
            try:
                socket.setdefaulttimeout(_SOCKET_TIMEOUT)
                feed = feedparser.parse(url)
            finally:
                socket.setdefaulttimeout(_old)
        if feed.bozo and not feed.entries:
            raise ValueError(f"Feed bozo with no entries: {feed.bozo_exception}")
    except Exception as exc:
        log.warning("FAILED feedparser %s (%s): %s", source_label, url, exc)
        failed_sources.append({"source": source_label, "url": url, "error": str(exc)})
        feed = type("_F", (), {"entries": [], "bozo": True})()

    if feed.entries:
        articles, date_dropped, unknown_date = [], 0, 0
        for entry in feed.entries:
            pub_dt = _parse_date_entry(entry)

            if pub_dt is None:
                unknown_date += 1
            elif not _within_cutoff(pub_dt, feed_type):
                date_dropped += 1
                continue

            link = entry.get("link", "")
            if not link:
                continue

            title = _strip_html(entry.get("title", ""))

            # bebeez.eu geo-filter: only keep Ireland/UK content
            if is_bebeez and not any(t in title.lower() for t in _BEBEEZ_GEO_TERMS):
                date_dropped += 1
                continue

            # Broadsheet keyword filter: high-volume Irish nationals must have
            # at least one sportstech signal in the title to be kept
            if is_broadsheet:
                title_lower = title.lower()
                if not any(kw in title_lower for kw in _BROADSHEET_KEYWORDS):
                    date_dropped += 1
                    continue

            snippet = _strip_html(
                entry.get("summary", "") or entry.get("description", "")
            )[:300]

            articles.append({
                "title":   title,
                "link":    link,
                "pubDate": pub_dt.isoformat() if pub_dt else "Unknown",
                "source":  source_label,
                "snippet": snippet,
            })

        oldest, newest = _date_range(articles)
        stats = {
            "total_entries": len(feed.entries),
            "date_dropped":  date_dropped,
            "unknown_date":  unknown_date,
            "kept":          len(articles),
            "method":        "rss",
            "oldest_date":   oldest,
            "newest_date":   newest,
        }
        return articles, stats

    # feedparser returned 0 entries: try fallbacks for known sites
    domain = _domain(url)
    if domain not in SCRAPE_FALLBACK:
        if not any(f["url"] == url for f in failed_sources):
            failed_sources.append({"source": source_label, "url": url, "error": "0 entries from feedparser"})
        return [], {**empty_stats}

    log.info("feedparser returned 0 for %s — trying lxml fallback", source_label)

    lxml_entries = _parse_feed_lxml(url)
    if lxml_entries:
        articles, date_dropped, unknown_date = [], 0, 0
        for e in lxml_entries:
            pub_dt = _parse_date_str(e.get("pub", ""))
            title = _strip_html(e["title"])
            if pub_dt is None:
                unknown_date += 1
            elif not _within_cutoff(pub_dt, feed_type):
                date_dropped += 1
                continue
            if is_bebeez and not any(t in title.lower() for t in _BEBEEZ_GEO_TERMS):
                date_dropped += 1
                continue
            articles.append({
                "title":   title,
                "link":    e["link"],
                "pubDate": pub_dt.isoformat() if pub_dt else "Unknown",
                "source":  source_label,
                "snippet": _strip_html(e.get("desc", ""))[:300],
            })
        if articles:
            oldest, newest = _date_range(articles)
            stats = {
                "total_entries": len(lxml_entries),
                "date_dropped":  date_dropped,
                "unknown_date":  unknown_date,
                "kept":          len(articles),
                "method":        "lxml",
                "oldest_date":   oldest,
                "newest_date":   newest,
            }
            log.info("lxml fallback succeeded for %s: %d articles", source_label, len(articles))
            return articles, stats

    log.info("lxml returned 0 for %s — falling back to HTML scrape", source_label)
    scrape_url = SCRAPE_FALLBACK[domain]
    articles, stats = _scrape_articles(scrape_url, source_label, failed_sources)
    oldest, newest = _date_range(articles)
    stats["method"]       = "scrape"
    stats["oldest_date"]  = oldest
    stats["newest_date"]  = newest
    return articles, stats


# ---------------------------------------------------------------------------
# Supabase company feeds
# ---------------------------------------------------------------------------

CAP_SUPABASE = 3  # max articles per company query


def get_supabase_company_feeds() -> list[tuple[str, str]]:
    """
    Fetch Irish-founded company names from Supabase REST API directly.
    Uses requests + Supabase PostgREST API — no supabase package needed.
    """
    try:
        supabase_url = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
        anon_key     = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

        if not supabase_url or not anon_key:
            log.warning("Supabase credentials not found — skipping company feeds")
            return []

        endpoint = f"{supabase_url}/rest/v1/companies"
        headers  = {
            "apikey":        anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type":  "application/json",
        }
        params = {
            "select":           "name,website,total_funding,employees",
            "is_irish_founded": "eq.true",
            "is_fdi":           "eq.false",
            "order":            "total_funding.desc.nullslast",
            "limit":            "30",
        }

        response = requests.get(endpoint, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        companies = response.json()

        log.info("Fetched %d Irish companies from Supabase", len(companies))

        feeds = []
        for company in companies:
            name = company.get("name", "").strip()
            if not name or len(name) < 3:
                continue
            encoded   = name.replace(" ", "+")
            query_url = (
                f"https://news.google.com/rss/search?q=%22{encoded}%22+ireland"
                f"&hl=en-IE&gl=IE&ceid=IE:en"
            )
            feeds.append((query_url, f"Supabase: {name}"))

        log.info("Generated %d company Google News queries", len(feeds))
        return feeds

    except Exception as exc:
        log.warning("Supabase company fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run() -> list[dict]:
    failed_sources: list[dict] = []

    if DEBUG_SKIP_DATE_FILTER:
        print("  ⚠️  DEBUG_SKIP_DATE_FILTER=True — date filter is OFF")

    on_windows = platform.system() == "Windows"
    if on_windows:
        log.info("Skipping Supabase company queries on Windows (SSL timeout risk)")
        supabase_feeds = []
    else:
        supabase_feeds = get_supabase_company_feeds()

    feed_stats: dict[str, dict] = {}
    type_counts = {"site_rss": 0, "google_news": 0}
    grand_total_entries = 0
    grand_date_dropped  = 0
    grand_unknown_date  = 0
    all_articles: list[dict] = []

    TOTAL_TIMEOUT_SECS = 300  # 5 minutes hard cap across all feeds
    run_start = time.time()

    supabase_stats: list[dict] = []  # separate tracking for Supabase company feeds

    for feed_type, feeds in [
        ("site_rss",    SITE_RSS_FEEDS),
        ("google_news", GOOGLE_NEWS_FEEDS),
    ]:
        for url in feeds:
            if time.time() - run_start > TOTAL_TIMEOUT_SECS:
                log.warning("Total fetch time exceeded %ds — stopping early with %d articles so far", TOTAL_TIMEOUT_SECS, len(all_articles))
                break

            label = label_for(url, feed_type)
            cap   = _cap_for(url, feed_type)

            items, fstats = fetch_feed(url, label, failed_sources, feed_type=feed_type)

            grand_total_entries += fstats["total_entries"]
            grand_date_dropped  += fstats["date_dropped"]
            grand_unknown_date  += fstats["unknown_date"]

            kept        = items[:cap]
            cap_dropped = len(items) - len(kept)

            feed_stats[url] = {
                "label":        label,
                "feed_type":    feed_type,
                "cap":          cap,
                "method":       fstats.get("method", "rss"),
                "fetched":      fstats["kept"],
                "cap_dropped":  cap_dropped,
                "kept":         len(kept),
                "oldest_date":  fstats.get("oldest_date", "—"),
                "newest_date":  fstats.get("newest_date", "—"),
            }

            type_counts[feed_type] += len(kept)
            all_articles.extend(kept)

    # Enterprise Ireland direct scrape (no RSS — custom parser for /en/news listing)
    ei_articles = _scrape_enterprise_ireland(failed_sources)
    ei_kept = ei_articles[:CAP_HIGH]
    ei_url = "https://www.enterprise-ireland.com/en/news"
    feed_stats[ei_url] = {
        "label":       "enterprise-ireland.com",
        "feed_type":   "site_rss",
        "cap":         CAP_HIGH,
        "method":      "scrape",
        "fetched":     len(ei_articles),
        "cap_dropped": len(ei_articles) - len(ei_kept),
        "kept":        len(ei_kept),
        "oldest_date": _date_range(ei_kept)[0],
        "newest_date": _date_range(ei_kept)[1],
    }
    type_counts["site_rss"] += len(ei_kept)
    all_articles.extend(ei_kept)

    # Business Post cloudscraper (bypasses Cloudflare; feedparser + plain requests = 403)
    bp_articles = _scrape_businesspost(failed_sources)
    bp_kept = bp_articles[:CAP_BUSINESSPOST]
    bp_url = "https://www.businesspost.ie/tech/"
    feed_stats[bp_url] = {
        "label":       "businesspost.ie",
        "feed_type":   "site_rss",
        "cap":         CAP_BUSINESSPOST,
        "method":      "cloudscraper",
        "fetched":     len(bp_articles),
        "cap_dropped": len(bp_articles) - len(bp_kept),
        "kept":        len(bp_kept),
        "oldest_date": _date_range(bp_kept)[0],
        "newest_date": _date_range(bp_kept)[1],
    }
    type_counts["site_rss"] += len(bp_kept)
    all_articles.extend(bp_kept)

    # Supabase company feeds — treated as google_news, capped at CAP_SUPABASE
    SUPABASE_TIMEOUT_SECS = 60
    supabase_start = time.time()
    for url, label in supabase_feeds:
        if time.time() - supabase_start > SUPABASE_TIMEOUT_SECS:
            log.warning("Supabase feed section exceeded %ds — stopping early", SUPABASE_TIMEOUT_SECS)
            break
        if time.time() - run_start > TOTAL_TIMEOUT_SECS:
            log.warning("Total fetch time exceeded %ds — stopping Supabase feeds early", TOTAL_TIMEOUT_SECS)
            break

        items, fstats = fetch_feed(url, label, failed_sources, feed_type="google_news")

        grand_total_entries += fstats["total_entries"]
        grand_date_dropped  += fstats["date_dropped"]
        grand_unknown_date  += fstats["unknown_date"]

        kept        = items[:CAP_SUPABASE]
        cap_dropped = len(items) - len(kept)

        type_counts["google_news"] += len(kept)
        all_articles.extend(kept)

        if fstats["kept"] > 0:  # only track companies that returned results
            supabase_stats.append({
                "label":   label.replace("Supabase: ", ""),
                "fetched": fstats["kept"],
                "kept":    len(kept),
            })

    # Deduplicate by URL
    seen: set[str] = set()
    deduped: list[dict] = []
    for art in all_articles:
        if art["link"] not in seen:
            seen.add(art["link"])
            deduped.append(art)

    total_after_cap = sum(type_counts.values())
    dedup_dropped   = total_after_cap - len(deduped)

    # Save
    month = datetime.now().strftime("%Y-%m")
    output_file = f"news_raw_{month}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "articles":       deduped,
                "failed_sources": failed_sources,
                "run_date":       datetime.now().isoformat(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # --- Diagnostic output ------------------------------------------------
    print(f"\n=== News Pipeline Diagnostic ===")
    print(f"  Date filter        : {'OFF (DEBUG)' if DEBUG_SKIP_DATE_FILTER else f'ON ({CUTOFF_DAYS}-day cutoff)'}")
    print()
    print(f"  Total feed entries : {grand_total_entries}")
    print(f"  Dropped by date    : {grand_date_dropped}  ({grand_unknown_date} unparseable → kept)")
    print(f"  After date filter  : {grand_total_entries - grand_date_dropped}")
    print(f"  Dropped by cap     : {sum(s['cap_dropped'] for s in feed_stats.values())}")
    print(f"  After cap          : {total_after_cap}")
    print(f"  Dropped by dedup   : {dedup_dropped}")
    print(f"  Final pool         : {len(deduped)}")
    print(f"  Failed feeds       : {len(failed_sources)}")
    print()
    print(f"  By type:")
    print(f"    Site RSS ({SITE_RSS_CUTOFF_DAYS}d cutoff) : {type_counts['site_rss']}")
    print(f"    Google News RSS ({CUTOFF_DAYS}d cutoff) : {type_counts['google_news']}")

    # Site RSS breakdown — includes date range to diagnose stale content
    print()
    print(f"  --- Site RSS (cutoff: {SITE_RSS_CUTOFF_DAYS} days) ---")
    print(f"  {'Source':<28} {'method':>6} {'fetched':>7} {'kept':>5}  {'oldest':>10}  {'newest':>10}")
    print(f"  {'-'*28} {'-'*6} {'-'*7} {'-'*5}  {'-'*10}  {'-'*10}")
    for url, s in feed_stats.items():
        if s["feed_type"] != "site_rss":
            continue
        lbl = s["label"][:26]
        print(
            f"  {lbl:<28} {s['method']:>6} {s['fetched']:>7} {s['kept']:>5}"
            f"  {s['oldest_date']:>10}  {s['newest_date']:>10}"
        )

    # Google News breakdown — one row per query so empty ones are obvious
    print()
    print(f"  --- Google News queries ---")
    print(f"  {'Query':<46} {'fetched':>7} {'capped':>6} {'kept':>5}")
    print(f"  {'-'*46} {'-'*7} {'-'*6} {'-'*5}")
    for url, s in feed_stats.items():
        if s["feed_type"] != "google_news":
            continue
        # strip "GNews: " prefix for compactness
        lbl = s["label"].replace("GNews: ", "")[:44]
        marker = "  ← empty" if s["kept"] == 0 else ""
        print(
            f"  {lbl:<46} {s['fetched']:>7} {s['cap_dropped']:>6} {s['kept']:>5}{marker}"
        )

    if on_windows:
        print(f"\n  Supabase queries: skipped (Windows)")
    elif supabase_stats:
        print(f"\n  --- Supabase company queries ({len(supabase_feeds)} companies, {len(supabase_stats)} with results) ---")
        print(f"  {'Company':<35} {'fetched':>7} {'kept':>5}")
        print(f"  {'-'*35} {'-'*7} {'-'*5}")
        for s in sorted(supabase_stats, key=lambda x: -x["kept"]):
            print(f"  {s['label'][:35]:<35} {s['fetched']:>7} {s['kept']:>5}")
    elif supabase_feeds:
        print(f"\n  Supabase: {len(supabase_feeds)} companies queried, 0 returned results")

    if failed_sources:
        print(f"\n  Failed feeds:")
        for fs in failed_sources:
            print(f"    - {fs['source']}: {fs['error']}")

    print(f"\n  Saved to: {output_file}")
    return deduped


if __name__ == "__main__":
    run()
