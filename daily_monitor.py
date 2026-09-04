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
import socket
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

from email_client import send_email as _send_email
from news_pipeline import (
    GOOGLE_NEWS_FEEDS,
    REGIONAL_RSS_FEEDS,
    _SOCKET_TIMEOUT,
    _cap_for,
    _decode_google_news_url,
)
import run_telemetry
from claude_budget import RunCost, call_claude_with_retry, within_budget
from supabase_client import build_news_item, upsert_news_item

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL        = "claude-sonnet-4-5-20250929"
LOOKBACK_HOURS = 72
MIN_SCORE    = 3
BATCH_SIZE   = 15
SEEN_FILE    = "daily_monitor_seen.json"

# Per-run cost ceiling for THIS pipeline, enforced against actual response.usage —
# an abort, never a prompt, because this runs unattended at 09:00 with nobody to
# confirm. The machinery is shared (claude_budget.py); the value is per-pipeline,
# since digest.py scores a 35-40 day corpus against this job's 72h window.
#
# Basis: a run at the measured post-change volume (83 in-window articles, 6 batches)
# costs ~$0.22 — $0.213 for score_articles plus ~$0.009 for deduplicate_by_story.
# That figure is derived from the real billed usage of the 2026-09-04 audit run
# (1285 articles, 169,239 in / 183,376 out, $3.2584 actual) on the same MODEL,
# rubric and BATCH_SIZE, scaled to daily volume — per-batch input is near-fixed
# because the rubric dominates it, and output ran 142.7 tok/article.
#
# Ceiling set at ~10x that baseline: high enough that normal volume swings and a
# full regional cap (6 feeds x 12) never trip it, low enough to stop a runaway.
# PROVISIONAL — retune against scripts/data/daily_monitor_usage.jsonl once a week
# of real runs has accumulated. No observed daily run existed when this was set.
RUN_COST_CEILING_USD = 2.25

# Headroom above the ceiling reserved for the completion path (deduplicate_by_story).
# The ceiling stops the run expanding its spend — no further scoring batches — but it
# does not abandon work already paid for. Dedup costs ~$0.01 at normal volume and its
# output cannot exceed _DEDUP_MAX_TOKENS, so $0.25 is ample headroom while still
# bounding the exception: past RUN_COST_CEILING_USD + this, dedup is skipped too.
DEDUP_COMPLETION_ALLOWANCE_USD = 0.25
_DEDUP_MAX_TOKENS = 500  # must match the max_tokens passed to the dedup call


RUN_COST = RunCost(RUN_COST_CEILING_USD, label="daily_monitor")


def _dedup_within_hard_stop(client, prompt: str, max_tokens: int) -> bool:
    """Dedup is completion, not expansion — see the call site. Bounded by a hard
    stop slightly above the ceiling so the exception stays measured."""
    return within_budget(
        client, RUN_COST, MODEL, prompt, max_tokens,
        RUN_COST_CEILING_USD + DEDUP_COMPLETION_ALLOWANCE_USD,
        what="deduplication",
    )


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


def _extract_real_url(entry, title: str, decode: bool = False) -> tuple[str, bool]:
    """
    Extract the real article URL from a feedparser entry.
    Returns (url, is_fallback) where is_fallback=True means a search URL was used.

    Try order:
      0. Decode Google News CBMi... redirect (only when decode=True — HTTP request)
      1. Non-Google href in entry.links
      2. Non-Google <a href> in entry.summary HTML
      3. Google search URL as fallback

    decode=True only for within-window articles to avoid making hundreds of
    HTTP requests for entries we won't score.

    Note: entry.source.href is intentionally NOT used — for Google News RSS it
    is the publisher's homepage label, not the article URL.
    """
    # 0. Decode Google News CBMi... redirect to the real article URL
    if decode:
        raw_link = getattr(entry, "link", "") or ""
        if raw_link and _is_google_url(raw_link):
            decoded = _decode_google_news_url(raw_link)
            if not _is_google_url(decoded):
                return decoded, False

    # 1. links list (catches non-GNews sources with a real <link> element)
    for lnk in getattr(entry, "links", []):
        href = lnk.get("href", "")
        if href and not _is_google_url(href):
            return href, False

    # 2. summary HTML
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

    # 3. Fallback: Google search link for the title
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

                # Only decode Google redirects for within-window articles;
                # out-of-window entries are counted but never scored.
                real_link, is_fallback = _extract_real_url(entry, title, decode=within)
                dedup_key = real_link
                if dedup_key in seen_links:
                    continue
                seen_links.add(dedup_key)

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

    # --- Irish regional / niche site RSS (added 2026-09-04) ------------------
    # Direct feeds for titles Google News ranks too deep to surface. No keyword
    # filtering by design — see REGIONAL_SOURCES in news_pipeline.py.
    # The cap is applied AFTER the date filter, taking items in feed order:
    # these feeds are reverse-chronological, so capping the raw entry list first
    # would throw away in-window stories whenever the cap is smaller than the feed.
    regional_run_ts = datetime.now(timezone.utc).isoformat()
    for url in REGIONAL_RSS_FEEDS:
        feed_title = url
        try:
            # feedparser.parse has no timeout of its own — without this a stalled
            # feed would hang the daily run. Shares news_pipeline's _SOCKET_TIMEOUT:
            # one value for one concern (feedparser stalling on TLS handshakes).
            # If it proves too tight for these feeds, raise the shared constant.
            _old_timeout = socket.getdefaulttimeout()
            try:
                socket.setdefaulttimeout(_SOCKET_TIMEOUT)
                feed = feedparser.parse(url)
            finally:
                socket.setdefaulttimeout(_old_timeout)

            entries = getattr(feed, "entries", None)
            feed_title = getattr(getattr(feed, "feed", None), "title", url) or url
            if not entries:
                # news_pipeline's lxml and HTML-scrape fallbacks are both gated on
                # SCRAPE_FALLBACK membership, which holds only thinkbusiness.ie and
                # sportireland.ie — no regional feed has a fallback. Zero entries here
                # is therefore terminal for this feed on this run.
                log.warning("Regional RSS returned no entries: %s", url[:80])
                run_telemetry.record_feed_stats(
                    url, feed_title, "zero_entries",
                    cap=_cap_for(url, "site_rss"), run_ts=regional_run_ts,
                )
                continue

            within_window = []

            for entry in entries:
                link = getattr(entry, "link", "")
                if not link or link in seen_links:
                    continue
                seen_links.add(link)

                within, pub_dt = is_within_hours(entry, hours)
                title = getattr(entry, "title", "").strip()
                raw_summary = (
                    getattr(entry, "summary", "")
                    or getattr(entry, "description", "")
                    or ""
                )

                article = {
                    "title":            title,
                    "source":           feed_title,
                    "pubDate":          pub_dt.isoformat() if pub_dt else "",
                    "link":             link,
                    "link_is_fallback": False,
                    "snippet":          re.sub(r"<[^>]+>", "", raw_summary)[:300].strip(),
                }
                all_articles.append(article)
                if within:
                    within_window.append(article)
                if pub_dt and (newest_pub_dt is None or pub_dt > newest_pub_dt):
                    newest_pub_dt = pub_dt

            cap     = _cap_for(url, "site_rss")
            kept    = within_window[:cap]
            dropped = within_window[cap:]
            recent_articles.extend(kept)

            # The cap truncates by recency, not relevance, so log what it discards:
            # a week of real drops is what tells us whether CAP_REGIONAL is too tight.
            run_telemetry.record_cap_drops(url, feed_title, dropped, cap, regional_run_ts)
            run_telemetry.record_feed_stats(
                url, feed_title, "ok",
                entries_fetched=len(entries),
                entries_in_window=len(within_window),
                kept_after_cap=len(kept),
                cap=cap,
                run_ts=regional_run_ts,
            )

            log.info(
                "[REGIONAL] %s — %d entries, %d within %dh, %d kept, %d dropped by cap %d",
                feed_title[:30], len(entries), len(within_window), hours,
                len(kept), len(dropped), cap,
            )

        except Exception as exc:
            # A timeout or transport error must be distinguishable in telemetry from a
            # feed that simply published nothing — otherwise both look like silence.
            log.warning("Regional RSS fetch failed (%s): %s", url[:80], exc)
            run_telemetry.record_feed_stats(
                url, feed_title, "error",
                cap=_cap_for(url, "site_rss"),
                error=f"{type(exc).__name__}: {exc}",
                run_ts=regional_run_ts,
            )

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
    # Real billed usage, read off each response — not an estimate.
    per_batch_usage: list[dict] = []
    run_in = run_out = 0

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
  Also score 4: Irish legal/regulatory developments with a direct impact on Irish sportstech
  companies (e.g. new DPC guidance on athlete biometric data, AI Act enforcement actions,
  athlete data rights rulings, NGB tech-related regulation, Project Red Card developments).
3 = European sportstech news relevant to Irish audience, Irish sports ecosystem news
  Also score 3 minimum: Irish legal, regulatory or governance commentary on sport
  (e.g. DPC guidance, EU AI Act implications for sport, athlete data rights, Project Red Card,
  Law Society Gazette on sports compliance) directly relevant to Irish sportstech.
2 = Irish sports news without tech angle, tangential sports connection
1 = No sports angle, pure politics/property/crime/lifestyle, exact duplicate

Return ONLY a JSON array, no other text, no markdown, no explanation.
Each item must have ALL of these keys:
  "idx": <number>
  "score": <1-5>
  "category": one of: Funding | Product Launch | Company News | Industry Report | Partnership | Event | Other
  "score_reason": <5-8 words explaining the score>
  "summary": <Exactly 2 sentences, 40-60 words total. Sentence 1: what happened, who did it, where (include Irish angle if present). Sentence 2: why it matters, what it enables, or what context helps the reader understand significance. Never just restate the headline. Factual, Irish-ecosystem-builder voice, no hype, never starts with "Exciting news" or "Delighted".
  BAD (too short, just restates headline): "Output Sports launches HYROX365 Athlete Readiness Test."
  GOOD (gives context and why-it-matters): "Dublin-based Output Sports has partnered with HYROX365 to launch a standardised Athlete Readiness Test using its sensor platform to measure strength, endurance, and recovery benchmarks. The partnership extends Output's reach into mass-participation fitness testing across the global HYROX network.">
  "relevance": <For score 3 and 4 only: one sentence, max 25 words, explaining why this specifically matters for the Irish sportstech ecosystem. Derive from the article content — do not invent context. For score 5, set to null (Irish angle is self-evident from the article). For score 1 and 2, set to null.
  BAD (too generic): "This matters because it affects Irish sportstech companies."
  GOOD (specific tie): "Enterprise Ireland's expanded HPSU programme directly accelerates capital access for early-stage Irish sportstech companies trying to avoid premature exits.">
  "tags": <list of 3-5 keyword strings: company names, themes, event types>
  "verticals": <list of 1-2 from: Performance Analytics | Wearables & Hardware | Fan Engagement | Media & Broadcasting | Health, Fitness and Wellbeing | Scouting & Recruitment | Esports & Gaming | Betting & Fantasy | Stadium & Event Tech | Club Management Software | Sports Education & Coaching | Other / Emerging>
  "mentioned_companies": <list of company names actually mentioned in the article>

ARTICLES:
{articles_text}
JSON array:"""

        try:
            response = call_claude_with_retry(
                client, RUN_COST,
                model=MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()

            b_in, b_out = run_telemetry.usage_from_response(response)
            run_in  += b_in
            run_out += b_out
            per_batch_usage.append({
                "batch":         batch_num + 1,
                "articles":      len(batch),
                "input_tokens":  b_in,
                "output_tokens": b_out,
            })

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
                    article["score"]               = item.get("score",    1)
                    article["category"]            = item.get("category", "Other")
                    article["reason"]              = item.get("score_reason", item.get("reason", ""))
                    article["summary"]             = item.get("summary",  "")
                    article["relevance"]           = item.get("relevance")
                    article["tags"]                = item.get("tags",     [])
                    article["verticals"]           = item.get("verticals", [])
                    article["mentioned_companies"] = item.get("mentioned_companies", [])
                    all_scored.append(article)

        except Exception as exc:
            log.error("Batch %d API call failed: %s", batch_num + 1, exc)
            continue

        # Circuit breaker: checked against actual accumulated usage, after each
        # batch. Everything already scored is kept and still processed downstream —
        # that spend is incurred either way, and discarding it would waste it.
        if RUN_COST.over_ceiling():
            RUN_COST.trip(
                batches_completed=batch_num + 1,
                batches_planned=len(batches),
                articles_scored=len(all_scored),
                articles_in_window=len(articles),
            )
            log.error(
                "COST CEILING HIT — $%.4f exceeds $%.2f after batch %d/%d. "
                "Stopping scoring; keeping %d already-scored article(s).",
                RUN_COST.cost, RUN_COST_CEILING_USD,
                batch_num + 1, len(batches), len(all_scored),
            )
            run_telemetry.record_call(
                "cost_ceiling_abort", MODEL, RUN_COST.input_tokens, RUN_COST.output_tokens,
                articles=len(articles),
                batches=batch_num + 1,
                extra={
                    "aborted":           True,
                    "ceiling_usd":       RUN_COST_CEILING_USD,
                    "batches_planned":   len(batches),
                    "batches_completed": batch_num + 1,
                    "articles_scored":   len(all_scored),
                    "requests":          RUN_COST.requests,
                },
            )
            break

    rec = run_telemetry.record_call(
        "score_articles", MODEL, run_in, run_out,
        articles=len(articles),
        batches=len(batches),
        per_batch=per_batch_usage,
        extra={"scored_returned": len(all_scored), "aborted": RUN_COST.tripped},
    )
    log.info(
        "[USAGE] score_articles — %d in / %d out tokens, $%.4f",
        run_in, run_out, rec["cost_usd"],
    )

    return all_scored


# ---------------------------------------------------------------------------
# Email sending via SendGrid
# ---------------------------------------------------------------------------

def send_cost_abort_alert() -> bool:
    """Email once per aborted run that the cost ceiling tripped.

    A non-zero exit turns the Actions run red, but a red scheduled job can sit
    unnoticed for days — and this failure mode repeats: whatever tripped the
    ceiling (a retry storm, a volume spike) trips it again at the same point on
    the next run, the backlog never clears, and the same articles are re-scored
    daily. The red build is the record; this is the notification.
    """
    d = RUN_COST.abort_details
    subject = (
        f"🚨 Daily monitor ABORTED — cost ceiling ${RUN_COST_CEILING_USD:.2f} "
        f"exceeded (${RUN_COST.cost:.2f})"
    )
    html_body = f"""<h2 style="color:#c00;">Daily monitor hit its cost ceiling</h2>

<p>Scoring stopped early. Articles already scored were still deduplicated,
upserted to Supabase and emailed — that spend was already incurred.</p>

<table cellpadding="6" style="border-collapse:collapse;">
  <tr><td><strong>Accumulated cost</strong></td><td>${RUN_COST.cost:.4f}</td></tr>
  <tr><td><strong>Ceiling</strong></td><td>${RUN_COST_CEILING_USD:.2f}</td></tr>
  <tr><td><strong>Batches completed</strong></td>
      <td>{d.get('batches_completed', '?')} of {d.get('batches_planned', '?')}</td></tr>
  <tr><td><strong>Articles scored</strong></td>
      <td>{d.get('articles_scored', '?')} of {d.get('articles_in_window', '?')} in window</td></tr>
  <tr><td><strong>Billed requests</strong></td><td>{RUN_COST.requests}</td></tr>
  <tr><td><strong>Tokens</strong></td>
      <td>{RUN_COST.input_tokens:,} in / {RUN_COST.output_tokens:,} out</td></tr>
  <tr><td><strong>Model</strong></td><td>{MODEL}</td></tr>
</table>

<p><strong>This will repeat.</strong> Whatever caused it will trip the ceiling again
at the same point on tomorrow's run, so the backlog will not clear on its own.</p>

<p>Check <code>scripts/data/daily_monitor_usage.jsonl</code> for the
<code>cost_ceiling_abort</code> record and the per-batch token counts. A high request
count relative to batches points at retries in <code>claude_budget.call_claude_with_retry</code>;
a high article count points at a discovery volume spike.</p>

<hr>
<p style="color:#888;font-size:12px;">Sent by the Sports D3c0d3d daily monitor cost breaker.</p>"""

    try:
        _send_email(subject, html_body)
        log.info("Cost abort alert email sent.")
        return True
    except Exception as exc:
        # Never let the alert failing mask the abort itself — the non-zero exit stands.
        log.error("Failed to send cost abort alert email: %s", exc)
        return False


def send_email(article: dict) -> bool:
    score       = article.get("score",            "?")
    title       = article.get("title",            "")
    category    = article.get("category",         "")
    source      = article.get("source",           "")
    pub_date    = article.get("pubDate",          "")[:10]
    reason      = article.get("reason",           "")
    summary     = article.get("summary",          "")
    relevance   = article.get("relevance")
    url         = article.get("link",             "")
    is_fallback = article.get("link_is_fallback", False)

    subject = f"⚡ [Score {score}/5] {title}"

    def _h(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    title_html = (
        f'<h2 style="color:#e65c00;">⚠️ {_h(title)}</h2>'
        f'<p><em>Direct link unavailable — search link provided instead</em></p>'
        if is_fallback else
        f'<h2><a href="{url}" style="color:#1a0dab;">{_h(title)}</a></h2>'
    )

    link_html = (
        f'<p><a href="{url}">Search Google for this article →</a></p>'
        if is_fallback else
        f'<p><a href="{url}">Read the full article →</a></p>'
    )

    relevance_html = (
        f'<p><strong>Relevance:</strong> {_h(relevance)}</p>'
        if relevance else ""
    )

    html_body = f"""{title_html}

<p><strong>Score:</strong> {score}/5<br>
<strong>Category:</strong> {_h(category)}<br>
<strong>Source:</strong> {_h(source)}<br>
<strong>Published:</strong> {pub_date}<br>
<strong>Reason:</strong> {_h(reason)}</p>

<p>{_h(summary)}</p>

{relevance_html}
{link_html}

<hr>
<p style="color:#888;font-size:12px;">Sent by Sports D3c0d3d daily monitor. Article scored {score}/5 for Irish sportstech relevance.</p>"""

    _send_email(subject, html_body, cc=os.getenv("ALERT_CC"))
    return True


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

    # Dedup is completion, not expansion: the ceiling stops the run from taking on
    # NEW spend (further scoring batches), but it does not abandon the completion
    # path for work already paid for. Skipping it would let duplicates reach both
    # the emails and the Supabase upserts, and those rows persist long after the
    # run ends — a worse outcome than one more small call.
    #
    # The exception is bounded and measured rather than asserted: price the actual
    # prompt with the free token counter, assume the worst-case output (max_tokens,
    # which the API cannot exceed), and only proceed if accumulated + projected
    # stays under a hard stop slightly above the ceiling.
    if not _dedup_within_hard_stop(client, prompt, _DEDUP_MAX_TOKENS):
        return articles

    try:
        response = call_claude_with_retry(
            client, RUN_COST,
            model=MODEL,
            max_tokens=_DEDUP_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Second billed call per run. Its prompt grows with the number of surviving
        # articles, so widening discovery grows this too — hence measured separately.
        d_in, d_out = run_telemetry.usage_from_response(response)
        rec = run_telemetry.record_call(
            "deduplicate_by_story", MODEL, d_in, d_out,
            articles=len(articles),
            batches=1,
        )
        log.info(
            "[USAGE] deduplicate_by_story — %d articles, %d in / %d out tokens, $%.4f",
            len(articles), d_in, d_out, rec["cost_usd"],
        )

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

def run() -> bool:
    """Returns True on a normal run, False if the cost ceiling aborted scoring."""
    RUN_COST.reset()
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
        return True

    # 2. Score
    scored = score_articles(recent_articles)

    # Alert immediately after scoring, so exactly one alert is sent per aborted run
    # regardless of which of run()'s return paths is taken below.
    if RUN_COST.tripped:
        send_cost_abort_alert()

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
        return not RUN_COST.tripped

    # 3b. Story-level deduplication
    before_dedup  = len(new_articles)
    new_articles  = deduplicate_by_story(new_articles)
    dedup_removed = before_dedup - len(new_articles)
    log.info("After story dedup: %d articles (%d duplicate(s) removed)", len(new_articles), dedup_removed)

    # 3c. Upsert score 3+ articles to Supabase hub
    hub_upsert_count = 0
    for article in new_articles:
        item = build_news_item(article)
        if upsert_news_item(item) is not None:
            hub_upsert_count += 1
            log.info("Supabase upsert OK: [Score %s] %s", article.get("score"), article.get("title", "")[:80])
        else:
            log.warning("Supabase upsert failed: %s", article.get("title", "")[:80])
    log.info("Supabase: upserted %d/%d items to hub", hub_upsert_count, len(new_articles))

    # 4. Send emails
    sent_count = 0
    for article in new_articles:
        title = article.get("title", "")
        if send_email(article):
            seen.add(article["link"])
            sent_count += 1
            log.info("Email sent: [Score %s] %s", article.get("score"), title[:80])
        else:
            unsent.append(article)
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
    print(f"Supabase upserted: {hub_upsert_count}/{len(new_articles)}")
    print(f"Emails sent: {sent_count}")
    if unsent:
        print(f"Failed (saved to unsent file): {len(unsent)}")
    if sent_count == 0:
        print("No new high-scoring articles found today.")

    print(f"Run cost: ${RUN_COST.cost:.4f} "
          f"({RUN_COST.input_tokens:,} in / {RUN_COST.output_tokens:,} out tokens, "
          f"{RUN_COST.requests} request(s))")
    if RUN_COST.tripped:
        print(f"*** COST CEILING ABORT — exceeded ${RUN_COST_CEILING_USD:.2f}. "
              f"Scoring stopped early; already-scored articles were still "
              f"upserted and emailed. ***")
    return not RUN_COST.tripped


if __name__ == "__main__":
    # Non-zero exit on a cost-ceiling abort so the workflow shows red — an
    # unattended run is exactly where a runaway would otherwise go unnoticed.
    raise SystemExit(0 if run() else 1)
