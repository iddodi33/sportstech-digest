"""
weekly_linkedin_digest.py
Produces a weekly LinkedIn roundup post for Sports D3c0d3d and sends it as an HTML email
on Friday at 12:00 UTC. Reads scored articles from the hub Supabase news_items table.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import anthropic
from dotenv import load_dotenv
from email_client import send_email as _resend_send
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"
MIN_SCORE = 3

LINKEDIN_WEEKLY_SYSTEM = """You write a weekly LinkedIn roundup post for Sports D3c0d3d, an Irish sportstech intelligence company. The post will appear on the Sports D3c0d3d company page on LinkedIn.

Voice: confident, direct, informed. No corporate jargon. No emojis. No em dashes. No exclamation marks. British/Irish English spelling. Sounds like an Irish sports industry professional, not a press release.

PICKING LOGIC — select the 5 stories to feature:
1. Sort all input articles by score descending, then by published_at descending.
2. Walk the sorted list and select 5 stories with this hard constraint: no two picked stories may share the same source publication, AND no two picked stories may cover the same underlying news event (for example, three articles all covering the Champions League rights deal collapse to one). The source rule applies even if the two stories from the same source cover different topics. Concrete example: if Sport for Business has two articles in the input, only the higher-scored one can be picked. The other goes to alternates regardless of its individual merit. If applying this rule leaves fewer than 5 stories, fill remaining slots from alternates by score, and at that point the source dedup is dropped for the fill slots only.
3. Continue until you have 5 picked stories, or until you have exhausted the list.
4. If fewer than 5 unique-source-and-topic stories exist after strict filtering, fill remaining slots with the next-best available articles by score, ignoring source/topic uniqueness for the fill.
5. Every input article must appear in either "picked" or "alternates". Do not drop any. Do not add any.

OPENER — write a stat-or-contrast hook:
- 18 to 30 words.
- Lead with a specific tension, contrast, or juxtaposition drawn from at least two of the five picked stories. Reference the stories by their content, not their source name or headline.
- Examples of the register:
  "An MLS deal in Galway and a women's sport report flagging Ireland as a laggard. Both true. Both this week."
  "Enterprise Ireland reopens the funding tap on the same day Orreco lands MLS. Tells you where the week is going."
- Do not start with "Here's what" or any variation. No emojis. No em dashes. No exclamation marks.

Output a single JSON object with these fields and no other text:

{
  "opener": "stat-or-contrast hook, 18 to 30 words, references at least two specific picked stories by content",
  "picked": [
    {
      "url": "exact url from input",
      "headline": "5 to 12 word headline written by you, not a copy of the article title. Lead with what happened.",
      "relevance": "one sentence, 15 to 25 words, explaining why this matters for Irish sportstech or Ireland's sports tech ecosystem. Derive it from the article's summary and score_reason fields. Do not invent context. If the story is European or international (score 3), the relevance line must explicitly tie it back to Ireland, Irish founders, Irish clubs, or the Irish sportstech market."
    }
  ],
  "alternates": [
    {
      "url": "exact url from input",
      "headline": "5 to 12 word headline written by you",
      "relevance": "one sentence, 15 to 25 words, why this matters for Irish sportstech or Ireland"
    }
  ],
  "closing": "one forward-looking line, 12 to 20 words. No call to action. No links. No 'follow us'. No 'subscribe'.",
  "hashtags": ["#SportsTech", "#Ireland", "1 to 3 more based on dominant verticals or themes. Each hashtag is a single token, no spaces."]
}

CRITICAL ANTI-HALLUCINATION RULES (these are the same rules used in the daily monitor LinkedIn drafts):

1. Verify before naming any company. If you reference an Irish sportstech company in a relevance line, the company must actually be in the article's summary or score_reason or mentioned_companies field. Do not pattern-match company names to themes.

2. Per-company capability facts. Only describe Irish sportstech companies in terms of what they actually do:
   - STATSports: GPS performance tracking only. Not concussion, not biomarkers, not video analytics.
   - Orreco: biomarker science and bio-analytics only. Not GPS, not wearables hardware.
   - Output Sports: IMU-based movement and strength testing only. Not GPS, not nutrition.
   - Kitman Labs: athlete management software platform only. Not hardware, not biomarkers.
   - Hexis: nutrition planning software only. Not performance tracking.
   - Danu Sports: smart sock biomechanics only. Not wrist wearables, not video.
   - KinetikIQ: motion capture via LiDAR on mobile devices. Not biomarkers, not GPS.

3. White-space framing. If the week's stories cover a category where no Irish company has a genuine product, do not name an Irish company. The relevance line can frame it as an opportunity or gap for the Irish ecosystem instead.

4. Person versus company distinction. Éanna Falvey is a person, not a company. Treat similar cases the same way.

5. If a story is genuinely off-topic for Irish sportstech and you cannot construct an honest relevance line, write the relevance line as a neutral observation about the broader European or global sportstech context, with no forced Irish hook.

6. The closing line must be substantively defensible. Do not predict specific funding rounds, deals, or company outcomes.

Output only the JSON object. No preamble, no commentary, no markdown fences."""


def _h(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_date(dt: datetime) -> str:
    """Format as 'Saturday 26 Apr' — day number without leading zero, cross-platform."""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%b')}"


def compute_window() -> tuple[datetime, datetime]:
    """Return (window_start, window_end): rolling 7-day look-back from run time."""
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=7)
    return window_start, window_end


def fetch_articles(window_start: datetime, window_end: datetime) -> list[dict]:
    url = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.error("NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set.")
        return []
    client = create_client(url, key)
    result = (
        client.table("news_items")
        .select(
            "title,original_title,source,url,summary,score,score_reason,"
            "mentioned_companies,image_url,published_at,tags,verticals"
        )
        .gte("score", MIN_SCORE)
        .gte("published_at", window_start.isoformat())
        .lte("published_at", window_end.isoformat())
        .order("score", desc=True)
        .order("published_at", desc=True)
        .execute()
    )
    return result.data or []


def call_claude(articles: list[dict]) -> tuple[dict | None, str]:
    """Call Claude with compact article JSON. Returns (parsed_dict, raw_text)."""
    compact = [
        {
            "title": a.get("title", ""),
            "source": a.get("source", ""),
            "url": a.get("url", ""),
            "summary": a.get("summary", ""),
            "score": a.get("score"),
            "score_reason": a.get("score_reason", ""),
            "mentioned_companies": a.get("mentioned_companies") or [],
        }
        for a in articles
    ]
    raw = ""
    try:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model=MODEL,
            max_tokens=5000,
            temperature=0.4,
            system=LINKEDIN_WEEKLY_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(compact, ensure_ascii=False)}],
        )
        raw = response.content[0].text.strip()
        log.info(
            "Claude model call succeeded. stop_reason=%s length=%d chars.",
            response.stop_reason, len(raw),
        )

        # Strip markdown code fences if Claude wrapped the JSON
        cleaned = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*```\s*$', '', cleaned).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if not match:
                raise ValueError("No JSON object found in Claude response.")
            parsed = json.loads(match.group())

        required = {"opener", "picked", "alternates", "closing", "hashtags"}
        missing = required - set(parsed.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        for story in parsed.get("picked", []) + parsed.get("alternates", []):
            if not all(k in story for k in ("url", "headline", "relevance")):
                raise ValueError(f"Story missing required fields: {story}")

        return parsed, raw

    except Exception as exc:
        log.error("Claude output parse failed: %s\nRaw response: %s", exc, raw[:1000])
        return None, raw


def _story_line(story: dict) -> str:
    """Render one story as: 'Headline - Relevance. read more-URL'"""
    relevance = story["relevance"].rstrip(".")
    return f"{story['headline']} - {relevance}. read more-{story['url']}"


def build_post_text(parsed: dict) -> str:
    lines = [parsed["opener"], ""]
    for i, story in enumerate(parsed["picked"]):
        if i > 0:
            lines.append("")
        lines.append(_story_line(story))
    lines.append("")
    lines.append(parsed["closing"])
    lines.append("")
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in parsed["hashtags"])
    lines.append(hashtags)
    return "\n".join(lines)


def build_alternates_section(parsed: dict) -> str:
    alternates = parsed.get("alternates", [])
    if not alternates:
        return "<p>No alternates this week.</p>"
    lines = []
    for i, story in enumerate(alternates):
        if i > 0:
            lines.append("")
        lines.append(_story_line(story))
    return (
        "<pre style='background:#f5f5f5;padding:15px;border-radius:5px;"
        f"white-space:pre-wrap;font-family:monospace;'>{_h(chr(10).join(lines))}</pre>"
    )


def build_html_email(
    parsed: dict,
    articles: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> str:
    post_text = build_post_text(parsed)
    alternates_html = build_alternates_section(parsed)
    period = f"{_fmt_date(window_start)} to {_fmt_date(window_end)}"
    n_picked = len(parsed.get("picked", []))
    n_alts = len(parsed.get("alternates", []))
    return f"""<h2>Sports D3c0d3d weekly LinkedIn digest</h2>
<p><strong>Period:</strong> {_h(period)}<br>
<strong>Articles in window:</strong> {len(articles)} ({n_picked} picked, {n_alts} alternates)</p>

<hr>

<h3>LinkedIn post draft, top 5 stories (copy-paste ready)</h3>
<p><em>Copy, edit as needed, and post to the Sports D3c0d3d LinkedIn page:</em></p>
<pre style="background:#f5f5f5;padding:15px;border-radius:5px;white-space:pre-wrap;font-family:monospace;">{_h(post_text)}</pre>

<hr>

<h3>Swap-in candidates</h3>
<p>If you want to substitute any of the five stories above, here are the others from this week in the same format. Replace any line in the post above with one of these.</p>
{alternates_html}

<hr>
<p style="color:#888;font-size:12px;">Sent by Sports D3c0d3d weekly LinkedIn digest. Period: {_h(period)}.</p>"""


def send_email(html_body: str, subject: str) -> int:
    """Send email via Resend. Returns HTTP status code. Raises on failure."""
    return _resend_send(subject, html_body)


def run():
    window_start, window_end = compute_window()
    print(f"Window start : {window_start.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"Window end   : {window_end.strftime('%Y-%m-%d %H:%M:%S')} UTC")

    articles = fetch_articles(window_start, window_end)
    print(f"Articles from Supabase: {len(articles)}")

    subject = (
        f"Sports D3c0d3d weekly LinkedIn digest, "
        f"{_fmt_date(window_start)} to {_fmt_date(window_end)}"
    )

    if not articles:
        date_range = f"{_fmt_date(window_start)} to {_fmt_date(window_end)}"
        html_body = (
            f"<p>Weekly LinkedIn digest, {_h(date_range)}: "
            f"no score 3+ articles this week. No post drafted.</p>"
        )
        status = send_email(html_body, subject)
        print(f"Email send status: {status}")
        return

    parsed, raw = call_claude(articles)
    print(f"Model call   : {'success' if parsed is not None else 'FAILED'}")

    if parsed is None:
        html_body = (
            "<p>Weekly digest ran but Claude output failed to parse. Raw output below.</p>"
            f"<pre style='background:#f5f5f5;padding:15px;white-space:pre-wrap;'>{_h(raw)}</pre>"
        )
        status = send_email(html_body, subject)
        print(f"Email send status: {status}")
        return

    picked = parsed.get("picked", [])
    alternates = parsed.get("alternates", [])
    print(f"Picked       : {len(picked)}")
    print(f"Alternates   : {len(alternates)}")
    print(f"Picked stories: {', '.join(s['headline'] for s in picked)}")

    post_text = build_post_text(parsed)
    print(f"Post length  : {len(post_text)} chars")

    html_body = build_html_email(parsed, articles, window_start, window_end)
    status = send_email(html_body, subject)
    print(f"Email send status: {status}")


if __name__ == "__main__":
    run()
