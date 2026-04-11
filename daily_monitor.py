"""
daily_monitor.py
Fetches Google News RSS feeds, scores with Claude, writes LinkedIn post drafts,
and sends email alerts via SendGrid for articles scoring 4 or 5.
Run daily via cron / GitHub Actions.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import urllib.parse

import anthropic
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

from news_pipeline import GOOGLE_NEWS_FEEDS

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL        = "claude-sonnet-4-20250514"
LOOKBACK_HOURS = 25
MIN_SCORE    = 4
BATCH_SIZE   = 15
SEEN_FILE    = "daily_monitor_seen.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ---------------------------------------------------------------------------
# URL extraction (Google News RSS → real article URLs)
# ---------------------------------------------------------------------------

def _is_google_url(url: str) -> bool:
    return "google.com" in url or "news.google.com" in url


def _extract_real_url(entry, title: str) -> tuple[str, bool]:
    """
    Extract the real article URL from a feedparser entry without HTTP requests.
    Returns (url, is_fallback) where is_fallback=True means a search URL was used.

    Try order:
      1. entry.source.href if non-Google
      2. Non-Google href in entry.links
      3. Non-Google <a href> in entry.summary HTML
      4. Google search URL as fallback
    """
    # 1. source href
    source_href = ""
    if hasattr(entry, "source") and isinstance(entry.source, dict):
        source_href = entry.source.get("href", "")
    if source_href and not _is_google_url(source_href):
        return source_href, False

    # 2. links list
    for lnk in getattr(entry, "links", []):
        href = lnk.get("href", "")
        if href and not _is_google_url(href):
            return href, False

    # 3. summary HTML
    summary_html = getattr(entry, "summary", "") or ""
    if summary_html:
        try:
            soup = BeautifulSoup(summary_html, "html.parser")
            for tag in soup.find_all("a", href=True):
                href = tag["href"]
                if href and not _is_google_url(href):
                    return href, False
        except Exception:
            pass

    # 4. Fallback: Google search link for the title
    fallback = f"https://www.google.com/search?q={urllib.parse.quote(title)}"
    return fallback, True


# ---------------------------------------------------------------------------
# Seen-URL deduplication
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if Path(SEEN_FILE).exists():
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("seen_urls", []))
        except Exception:
            pass
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen_urls": sorted(seen)}, f, indent=2)


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------

_NO_CACHE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def fetch_feed_fresh(url: str):
    """
    Fetch a Google News RSS feed via requests with cache-busting headers
    and a timestamp query parameter, then parse with feedparser.
    Falls back to plain feedparser on any requests error.
    """
    separator = "&" if "?" in url else "?"
    bust_url  = f"{url}{separator}ts={int(time.time())}"
    try:
        resp = requests.get(bust_url, headers=_NO_CACHE_HEADERS, timeout=15)
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as exc:
        log.warning("fetch_feed_fresh failed for %s (%s) — falling back to feedparser", url[:80], exc)
        try:
            return feedparser.parse(url)
        except Exception:
            return None

_DATE_FORMATS = [
    '%a, %d %b %Y %H:%M:%S %z',
    '%a, %d %b %Y %H:%M:%S GMT',
    '%Y-%m-%dT%H:%M:%S%z',
    '%Y-%m-%dT%H:%M:%SZ',
    '%Y-%m-%d %H:%M:%S',
]


def parse_date_robust(date_str: str) -> datetime | None:
    """Parse a date string to a UTC-aware datetime, trying multiple formats."""
    if not date_str:
        return None
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None


def _entry_pub_dt(entry) -> datetime | None:
    """
    Extract a UTC-aware datetime from a feedparser entry.
    Tries published_parsed (time tuple) first — most reliable cross-platform.
    Falls back to string parsing of published/updated fields.
    """
    # published_parsed is a UTC time.struct_time — most reliable
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass
    # String fallback
    date_str = getattr(entry, "published", "") or getattr(entry, "updated", "")
    return parse_date_robust(date_str)


def is_within_hours(entry, hours: int = 25) -> tuple[bool, datetime | None]:
    """
    Return (within_cutoff, pub_dt).
    If the date cannot be parsed at all, returns (True, None) — include
    rather than silently drop articles with unparseable dates.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    pub_dt = _entry_pub_dt(entry)
    if pub_dt is None:
        return True, None  # can't parse → don't discard
    return pub_dt >= cutoff, pub_dt


def fetch_recent_articles(hours: int = LOOKBACK_HOURS) -> tuple[list[dict], int]:
    """Returns (articles_within_cutoff, total_fetched_before_filter)."""
    all_articles = []
    recent_articles = []
    seen_links: set[str] = set()
    date_check_count = 0
    newest_pub_dt: datetime | None = None

    for url in GOOGLE_NEWS_FEEDS:
        try:
            feed = fetch_feed_fresh(url)
            if feed is None:
                continue
            for entry in feed.entries:
                raw_link = getattr(entry, "link", "")
                if not raw_link:
                    continue

                within, pub_dt = is_within_hours(entry, hours)
                title = getattr(entry, "title", "").strip()

                # Diagnostic: log date info for the first 3 articles seen
                if date_check_count < 3:
                    log.info(
                        "[DATE CHECK] Article: %s | parsed_date: %s | within_%dh: %s",
                        title[:50], pub_dt, hours, within,
                    )
                    date_check_count += 1

                real_link, is_fallback = _extract_real_url(entry, title)
                if real_link in seen_links:
                    continue
                seen_links.add(real_link)

                pub_iso = pub_dt.isoformat() if pub_dt else ""
                article = {
                    "title":            title,
                    "source":           getattr(feed.feed, "title", url),
                    "pubDate":          pub_iso,
                    "link":             real_link,
                    "link_is_fallback": is_fallback,
                    "snippet":          re.sub(r"<[^>]+>", "", getattr(entry, "summary", "") or "")[:300].strip(),
                }
                all_articles.append(article)
                if within:
                    recent_articles.append(article)
                if pub_dt and (newest_pub_dt is None or pub_dt > newest_pub_dt):
                    newest_pub_dt = pub_dt
                if is_fallback:
                    log.debug("URL fallback (search link) for: %s", title[:80])

        except Exception as exc:
            log.warning("Feed fetch failed (%s): %s", url[:80], exc)

    # Freshness diagnostic
    if newest_pub_dt:
        hours_ago = (datetime.now(timezone.utc) - newest_pub_dt).total_seconds() / 3600
        log.info(
            "[FRESHNESS] Newest article found: %s (%.1fh ago)",
            newest_pub_dt.strftime("%Y-%m-%d %H:%M UTC"), hours_ago,
        )
    else:
        log.warning("[FRESHNESS] Could not determine newest article date.")

    log.info("Fetched %d articles total, %d within last %dh", len(all_articles), len(recent_articles), hours)
    return recent_articles, len(all_articles)


# ---------------------------------------------------------------------------
# Scoring (batched, same logic as digest.py)
# ---------------------------------------------------------------------------

def score_articles(articles: list[dict]) -> list[dict]:
    client  = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    batches = [articles[i:i + BATCH_SIZE] for i in range(0, len(articles), BATCH_SIZE)]
    log.info("Scoring %d articles in %d batches…", len(articles), len(batches))
    all_scored = []

    for batch_num, batch in enumerate(batches):
        articles_text = ""
        for i, a in enumerate(batch):
            articles_text += f"{i}. TITLE: {a.get('title', '')[:120]}\n"
            articles_text += f"   SOURCE: {a.get('source', '')}\n"
            articles_text += f"   DATE: {a.get('pubDate', '')}\n"
            articles_text += f"   SNIPPET: {a.get('snippet', '')[:150]}\n\n"

        prompt = f"""Score these {len(batch)} articles for an Irish sportstech newsletter.

[SCORING CRITERIA]
5 = Irish sportstech company news, funding, award, product launch, international expansion
  Examples: "Cavan Start-up ClubSpot Scales Grassroots Glory into a Global Tech Empire"
            "Torpey Glove Shortlisted for Prestigious Global Sports Tech Award"
            "Feenix Group expands to US base" / "Anyscor secures Enterprise Ireland HPSU funding"
4 = Irish sports org adopting tech, Irish sportstech person featured, Irish adjacent
  Examples: "How Leinster Rugby is using data to boost fan experiences"
            "TrojanTrack grabs One to Watch prize at UCD AI accelerator"
            "Keith Brock Enterprise Ireland sportstech investment"
3 = European sportstech news relevant to Irish audience, Irish sports ecosystem news
2 = Irish sports news without tech angle, tangential sports connection
1 = No sports angle, pure politics/property/crime/lifestyle, exact duplicate

Return ONLY a JSON array, no other text, no markdown, no explanation.
Each item: {{"idx": <number>, "score": <1-5>, "category": "<type>", "reason": "<5-8 words>", "summary": "<one sentence max 120 chars>"}}
Category must be one of: Funding | Product Launch | Company News | Industry Report | Partnership | Event | Other

ARTICLES:
{articles_text}
JSON array:"""

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            try:
                batch_scored = json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r'\[.*\]', raw, re.DOTALL)
                if match:
                    try:
                        batch_scored = json.loads(match.group())
                    except json.JSONDecodeError:
                        log.error("Batch %d JSON parse failed — saving debug file.", batch_num + 1)
                        with open(f"claude_debug_daily_batch_{batch_num + 1}.txt", "w") as f:
                            f.write(raw)
                        continue
                else:
                    log.error("Batch %d: no JSON array found in response.", batch_num + 1)
                    continue

            for item in batch_scored:
                idx = item.get("idx", -1)
                if 0 <= idx < len(batch):
                    article = batch[idx].copy()
                    article["score"]    = item.get("score",    1)
                    article["category"] = item.get("category", "Other")
                    article["reason"]   = item.get("reason",   "")
                    article["summary"]  = item.get("summary",  "")
                    all_scored.append(article)

        except Exception as exc:
            log.error("Batch %d API call failed: %s", batch_num + 1, exc)
            continue

    return all_scored


# ---------------------------------------------------------------------------
# LinkedIn post generation
# ---------------------------------------------------------------------------

LINKEDIN_SYSTEM = """You write LinkedIn posts for Iddo, an Irish sportstech newsletter editor \
and ecosystem builder based in Dublin.

His writing style:
- Opens with a short punchy statement or observation — never a question, \
never starts with "I", never uses "Exciting news" or "Delighted to share"
- Short sentences, lots of white space, very scannable
- Uses → arrow lists when listing multiple things
- Always adds his own perspective or take — not just summarising, \
but adding an insight about what this means for the Irish sportstech ecosystem
- References Irish sportstech context where relevant
- Tags relevant companies or people with [Company Name] or [Person Name] \
as placeholders (we don't have LinkedIn URLs)
- 3-5 hashtags at the end, always includes #SportsTech, plus relevant ones \
from: #IrishSportsTech #SportsInnovation #Innovation #Entrepreneurship \
#FanEngagement #SportsData #Wearables #Esports
- Ends with a forward-looking or thought-provoking closer
- Medium length: 150-250 words
- Tone: insider, ecosystem builder, genuinely engaged, not corporate

Example of his style:
"Ireland just proved it's Europe's #1 SportsTech hub per capita.

Sony acquired STATSports. Irish companies raised €25M+. AI went from \
lab to stadium.

This isn't luck. It's a decade of quiet, determined building.

→ Concussion detection wearables
→ Haptic devices for blind fans
→ Performance analytics in the Premier League, NBA, AFL

Irish innovation is powering elite sport worldwide.

The question isn't whether Ireland belongs at the top table.
It's how we make sure the next generation knows it's possible.

Link in comments 👇

#IrishSportsTech #SportsTech #SportsInnovation"
"""


def generate_linkedin_post(article: dict) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    user = (
        f"Write a LinkedIn post for Iddo about this article.\n"
        f"Add his perspective on what it means for Irish sportstech.\n\n"
        f"Article title: {article.get('title', '')}\n"
        f"Source: {article.get('source', '')}\n"
        f"Summary: {article.get('summary', '')}\n"
        f"Score: {article.get('score', '')}/5\n"
        f"Category: {article.get('category', '')}\n"
        f"URL: {article.get('link', '')}\n\n"
        f"End the post with \"Link in comments 👇\" on its own line,\n"
        f"then the URL on the next line,\n"
        f"then the hashtags."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=600,
            system=LINKEDIN_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        log.error("LinkedIn post generation failed for '%s': %s", article.get("title", ""), exc)
        return f"[LinkedIn post generation failed: {exc}]"


# ---------------------------------------------------------------------------
# Email sending via SendGrid
# ---------------------------------------------------------------------------

def send_email(article: dict, linkedin_post: str) -> bool:
    sg_key     = os.getenv("SENDGRID_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to   = os.getenv("ALERT_TO")

    if not sg_key or not alert_from or not alert_to:
        log.error("SENDGRID_API_KEY, ALERT_FROM, or ALERT_TO not set in .env")
        return False

    score       = article.get("score",            "?")
    title       = article.get("title",            "")
    category    = article.get("category",         "")
    source      = article.get("source",           "")
    pub_date    = article.get("pubDate",          "")[:10]
    reason      = article.get("reason",           "")
    url         = article.get("link",             "")
    is_fallback = article.get("link_is_fallback", False)

    subject = f"⚡ [Score {score}/5] {title}"

    # Escape for HTML
    def _h(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    lp_html = _h(linkedin_post).replace("\n", "<br>")

    link_html = (
        f'<p style="color:#e65c00;">⚠️ Direct link unavailable — search link provided instead</p>'
        f'<p><a href="{url}">Search Google for this article →</a></p>'
        if is_fallback else
        f'<p><a href="{url}">Read the full article →</a></p>'
    )

    html_body = f"""<h2>New Irish SportsTech Alert</h2>

<p><strong>Score:</strong> {score}/5<br>
<strong>Category:</strong> {_h(category)}<br>
<strong>Source:</strong> {_h(source)}<br>
<strong>Published:</strong> {pub_date}<br>
<strong>Reason:</strong> {_h(reason)}</p>

{link_html}

<hr>

<h3>LinkedIn Post Draft</h3>
<p><em>Copy, edit as needed, and post:</em></p>
<pre style="background:#f5f5f5;padding:15px;border-radius:5px;white-space:pre-wrap;">{lp_html}</pre>

<hr>
<p style="color:#888;font-size:12px;">
Sent by Sports D3c0d3d daily monitor.
Article scored {score}/5 for Irish sportstech relevance.
</p>"""

    message = Mail(
        from_email=alert_from,
        to_emails=alert_to,
        subject=subject,
        html_content=html_body,
    )

    try:
        sg = SendGridAPIClient(sg_key)
        sg.send(message)
        return True
    except Exception as exc:
        log.error("SendGrid send failed for '%s': %s", title, exc)
        return False


# ---------------------------------------------------------------------------
# Story-level deduplication
# ---------------------------------------------------------------------------

def deduplicate_by_story(articles: list[dict]) -> list[dict]:
    """
    Group articles that are likely the same story using Claude.
    Keeps the highest-scored article per group; on equal scores,
    prefers direct site RSS over Google News.
    """
    if len(articles) <= 1:
        return articles

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    titles_text = "\n".join(
        f"{i}. {a['title']} (source: {a['source']})"
        for i, a in enumerate(articles)
    )

    prompt = f"""These news articles may contain duplicate stories from different sources. \
Group any articles that are about the same event or announcement.

Return ONLY a JSON array of groups. Each group is an array of indices. \
Articles that are unique get their own single-element group.

Example: [[0,2],[1],[3,4]] means articles 0 and 2 are the same story, \
article 1 is unique, and 3 and 4 are duplicates of each other.

Articles:
{titles_text}

JSON array of groups:"""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not match:
            log.warning("Story deduplication: no JSON array in response — skipping.")
            return articles

        groups = json.loads(match.group())

        def _sort_key(a):
            is_gnews = 1 if (
                "news.google.com" in a.get("link", "") or
                "Google News" in a.get("source", "")
            ) else 0
            return (-int(a.get("score", 0)), is_gnews)

        deduped = []
        for group in groups:
            group_articles = [articles[i] for i in group if i < len(articles)]
            if not group_articles:
                continue
            group_articles.sort(key=_sort_key)
            deduped.append(group_articles[0])
            if len(group_articles) > 1:
                dropped_titles = [a["title"][:50] for a in group_articles[1:]]
                log.info(
                    "Deduped: kept '%s', dropped %d duplicate(s): %s",
                    group_articles[0]["title"][:50],
                    len(dropped_titles),
                    dropped_titles,
                )

        return deduped

    except Exception as exc:
        log.warning("Story deduplication failed: %s — using original list.", exc)
        return articles


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    seen   = load_seen()
    unsent = []

    cutoff_display = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M")
    log.info("Daily monitor — cutoff: %s UTC (last %dh)", cutoff_display, LOOKBACK_HOURS)

    # 1. Fetch
    recent_articles, total_fetched = fetch_recent_articles(LOOKBACK_HOURS)
    if not recent_articles:
        log.info("No articles within last %dh — exiting.", LOOKBACK_HOURS)
        print("=== Daily Monitor Complete ===")
        print(f"Articles fetched: {total_fetched}")
        print(f"After {LOOKBACK_HOURS}hr filter: 0")
        print("No new high-scoring articles found today.")
        return

    # 2. Score
    scored = score_articles(recent_articles)
    high   = [a for a in scored if int(a.get("score", 0)) >= MIN_SCORE]

    # 3. Deduplicate against seen
    new_articles   = [a for a in high if a.get("link", "") not in seen]
    already_seen_n = len(high) - len(new_articles)

    if not new_articles:
        log.info("No new high-scoring articles found today.")
        print("=== Daily Monitor Complete ===")
        print(f"Articles fetched: {total_fetched}")
        print(f"After {LOOKBACK_HOURS}hr filter: {len(recent_articles)}")
        print(f"Scored {MIN_SCORE}+: {len(high)}")
        print(f"Already seen (skipped): {already_seen_n}")
        print("Emails sent: 0")
        print("No new high-scoring articles found today.")
        return

    # 3b. Story-level deduplication
    before_dedup  = len(new_articles)
    new_articles  = deduplicate_by_story(new_articles)
    dedup_removed = before_dedup - len(new_articles)
    log.info("After story dedup: %d articles (%d duplicate(s) removed)", len(new_articles), dedup_removed)

    # 4. Generate posts + send emails
    sent_count = 0
    for article in new_articles:
        title = article.get("title", "")
        log.info("Generating LinkedIn post for: %s", title[:80])
        linkedin_post = generate_linkedin_post(article)

        if send_email(article, linkedin_post):
            seen.add(article["link"])
            sent_count += 1
            log.info("Email sent: [Score %s] %s", article.get("score"), title[:80])
        else:
            unsent.append({**article, "linkedin_post": linkedin_post})
            log.warning("Email failed — queued for unsent log: %s", title[:80])

    # 5. Persist seen list
    save_seen(seen)

    # 6. Save any unsent
    if unsent:
        unsent_path = f"daily_alerts_unsent_{datetime.now().strftime('%Y-%m-%d')}.json"
        with open(unsent_path, "w", encoding="utf-8") as f:
            json.dump(unsent, f, ensure_ascii=False, indent=2)
        log.warning("Saved %d unsent alerts to %s", len(unsent), unsent_path)

    print("=== Daily Monitor Complete ===")
    print(f"Articles fetched: {total_fetched}")
    print(f"After {LOOKBACK_HOURS}hr filter: {len(recent_articles)}")
    print(f"Scored {MIN_SCORE}+: {len(high)}")
    print(f"Already seen (skipped): {already_seen_n}")
    print(f"After story dedup: {len(new_articles)} (removed {dedup_removed} duplicate(s))")
    print(f"Emails sent: {sent_count}")
    if unsent:
        print(f"Failed (saved to unsent file): {len(unsent)}")
    if sent_count == 0:
        print("No new high-scoring articles found today.")


if __name__ == "__main__":
    run()
