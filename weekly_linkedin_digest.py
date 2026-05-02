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
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5-20250929"
MIN_SCORE = 3

LINKEDIN_WEEKLY_SYSTEM = """You write a weekly LinkedIn roundup post for Sports D3c0d3d, an Irish sportstech intelligence company. The post will appear on the Sports D3c0d3d company page on LinkedIn.

Voice: confident, direct, informed. No corporate jargon. No emojis. No em dashes. No exclamation marks. British/Irish English spelling. Sounds like an Irish sports industry professional, not a press release.

Output a single JSON object with these fields and no other text:

{
  "opener": "one line, around 12 to 18 words, in the register of 'Here's what moved in Irish SportsTech this week' but freshly worded each week. If a clear theme runs through the week's stories, hint at it. Otherwise a neutral framing. Never repeat the literal phrase 'Here's what moved' verbatim.",
  "stories": [
    {
      "url": "exact url from input",
      "headline": "5 to 12 word headline written by you, not a copy of the article title. Lead with what happened.",
      "relevance": "one sentence, 15 to 25 words, explaining why this matters for Irish sportstech or for Ireland's sports tech ecosystem. Derive it from the article's summary and score_reason fields. Do not invent context. If the story is European or international (score 3), the relevance line must explicitly tie it back to Ireland, Irish founders, Irish clubs, or the Irish sportstech market."
    }
  ],
  "closing": "one forward-looking line, 12 to 20 words. Something like 'Watch this space' or a substantive forward observation if the week's stories support one. No call to action. No links. No 'follow us'. No 'subscribe'.",
  "hashtags": ["3 to 5 hashtags chosen from the week's themes. Always include #SportsTech and #Ireland. Add 1 to 3 more based on the dominant verticals or themes in the week's stories. Examples: #AI, #Wearables, #FanEngagement, #PerformanceAnalytics, #Investment, #Startups. Each hashtag is a single token, no spaces."]
}

Order the stories by score descending, then by published date descending. Include every article supplied. Do not drop any. Do not add any.

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
            max_tokens=4000,
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

        required = {"opener", "stories", "closing", "hashtags"}
        missing = required - set(parsed.keys())
        if missing:
            raise ValueError(f"Missing required fields: {missing}")
        for story in parsed.get("stories", []):
            if not all(k in story for k in ("url", "headline", "relevance")):
                raise ValueError(f"Story missing required fields: {story}")

        return parsed, raw

    except Exception as exc:
        log.error("Claude output parse failed: %s\nRaw response: %s", exc, raw[:1000])
        return None, raw


def build_post_text(parsed: dict) -> str:
    lines = [parsed["opener"], ""]
    for i, story in enumerate(parsed["stories"]):
        if i > 0:
            lines.append("")
        lines.append(story["headline"])
        lines.append(f"Why it matters: {story['relevance']}")
        lines.append(story["url"])
    lines.append("")
    lines.append(parsed["closing"])
    lines.append("")
    hashtags = " ".join(f"#{h.lstrip('#')}" for h in parsed["hashtags"])
    lines.append(hashtags)
    return "\n".join(lines)


def build_source_table(parsed: dict, articles: list[dict]) -> str:
    url_to_article = {a.get("url", ""): a for a in articles}
    cols = ("Score", "Source", "Headline", "Relevance", "Published")
    header = "<tr>" + "".join(
        f"<th style='padding:6px 10px;border:1px solid #ddd;background:#f0f0f0;text-align:left;'>{c}</th>"
        for c in cols
    ) + "</tr>"
    rows = []
    for story in parsed.get("stories", []):
        url = story.get("url", "")
        art = url_to_article.get(url, {})
        pub = (art.get("published_at", "") or "")[:10]
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{_h(art.get('score', ''))}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{_h(art.get('source', ''))}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>"
            f"<a href='{url}'>{_h(art.get('title', url))}</a></td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{_h(story.get('relevance', ''))}</td>"
            f"<td style='padding:6px 10px;border:1px solid #ddd;'>{_h(pub)}</td>"
            f"</tr>"
        )
    return (
        "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
        f"{header}{''.join(rows)}"
        "</table>"
    )


def build_html_email(
    parsed: dict,
    articles: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> str:
    post_text = build_post_text(parsed)
    source_table = build_source_table(parsed, articles)
    period = f"{_fmt_date(window_start)} to {_fmt_date(window_end)}"
    return f"""<h2>Sports D3c0d3d weekly LinkedIn digest</h2>
<p><strong>Period:</strong> {_h(period)}<br>
<strong>Articles included:</strong> {len(articles)}</p>

<hr>

<h3>LinkedIn post draft (copy-paste ready)</h3>
<p><em>Copy, edit as needed, and post to the Sports D3c0d3d LinkedIn page:</em></p>
<pre style="background:#f5f5f5;padding:15px;border-radius:5px;white-space:pre-wrap;font-family:monospace;">{_h(post_text)}</pre>

<hr>

<h3>Source table for your reference</h3>
{source_table}

<hr>
<p style="color:#888;font-size:12px;">Sent by Sports D3c0d3d weekly LinkedIn digest. Period: {_h(period)}.</p>"""


def send_email(html_body: str, subject: str) -> int | None:
    """Send email via SendGrid. Returns HTTP status code, or None on exception."""
    sg_key = os.getenv("SENDGRID_API_KEY")
    alert_from = os.getenv("ALERT_FROM")
    alert_to = os.getenv("ALERT_TO")
    if not sg_key or not alert_from or not alert_to:
        log.error("SENDGRID_API_KEY, ALERT_FROM, or ALERT_TO not set.")
        return None
    message = Mail(
        from_email=alert_from,
        to_emails=alert_to,
        subject=subject,
        html_content=html_body,
    )
    try:
        sg = SendGridAPIClient(sg_key)
        response = sg.send(message)
        log.info("SendGrid status: %s", response.status_code)
        if response.status_code >= 400:
            log.error("SendGrid error %s: %s", response.status_code, response.body)
        return response.status_code
    except Exception as exc:
        log.error("SendGrid send failed: %s", exc)
        return None


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

    post_text = build_post_text(parsed)
    print(f"Post length  : {len(post_text)} chars")

    html_body = build_html_email(parsed, articles, window_start, window_end)
    status = send_email(html_body, subject)
    print(f"Email send status: {status}")


if __name__ == "__main__":
    run()
