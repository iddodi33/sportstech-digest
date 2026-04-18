"""supabase_client.py — writes scored news items to the hub's Supabase DB."""

import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_PUBLISHER_MAP = {
    "siliconrepublic.com":    "Silicon Republic",
    "sportforbusiness.com":   "Sport for Business",
    "businesspost.ie":        "Business Post",
    "irishtimes.com":         "Irish Times",
    "irishexaminer.com":      "Irish Examiner",
    "independent.ie":         "Irish Independent",
    "rte.ie":                 "RTÉ",
    "thejournal.ie":          "The Journal",
    "techcentral.ie":         "TechCentral",
    "irishtechnews.ie":       "Irish Tech News",
    "businessplus.ie":        "Business Plus",
    "thinkbusiness.ie":       "Think Business",
    "enterprise-ireland.com": "Enterprise Ireland",
    "sportireland.ie":        "Sport Ireland",
    "bebeez.eu":              "Bebeez",
    "eu-startups.com":        "EU-Startups",
    "sifted.eu":              "Sifted",
    "techcrunch.com":         "TechCrunch",
    "theathletic.com":        "The Athletic",
    "espn.com":               "ESPN",
    "reuters.com":            "Reuters",
    "bloomberg.com":          "Bloomberg",
    "forbes.com":             "Forbes",
    "ft.com":                 "Financial Times",
    "sportstechx.com":        "SportsTechX",
    "sportspro.com":          "SportsPro",
    "sbcnews.co.uk":          "SBC News",
    "sustainhealth.fit":      "Sustain Health Magazine",
    "mshale.com":             "Mshale",
    "sportstourismnews.com":  "Sports Tourism News",
}


_MULTI_TLDS = [
    ".co.uk", ".co.nz", ".com.au", ".co.ie",
    ".co.za", ".org.uk", ".ac.uk", ".gov.uk",
]


def extract_publisher(url: str) -> str:
    """Return a clean publisher name derived from the article URL domain."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    host = host.lower().removeprefix("www.")
    if host in _PUBLISHER_MAP:
        return _PUBLISHER_MAP[host]
    # Strip TLD to get the registrable stem
    stem = host
    for multi in _MULTI_TLDS:
        if stem.endswith(multi):
            stem = stem[: -len(multi)]
            break
    else:
        # Single-segment TLD — drop the last dotted part
        if "." in stem:
            stem = stem.rsplit(".", 1)[0]
    return stem.replace("-", " ").title()


_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client
    url = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        log.warning(
            "NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — "
            "Supabase writes disabled."
        )
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception as exc:
        log.error("Failed to create Supabase client: %s", exc)
        return None


def build_news_item(article: dict, scoring_result: dict | None = None) -> dict:
    """Assemble a news_items row from a raw article plus Claude's scoring output.

    If scoring_result is None the fields are read from article directly
    (useful when scoring fields have already been merged in).
    """
    sr = scoring_result or article
    published_at = article.get("pubDate") or datetime.now(timezone.utc).isoformat()
    return {
        "url":                 article.get("link", ""),
        "title":               article.get("title", ""),
        "source":              extract_publisher(article.get("link", "")),
        "published_at":        published_at,
        "score":               int(sr.get("score", article.get("score", 0))),
        "score_reason":        sr.get("score_reason", article.get("reason", "")),
        "summary":             sr.get("summary", article.get("summary", "")),
        "tags":                sr.get("tags", article.get("tags", [])) or [],
        "verticals":           sr.get("verticals", article.get("verticals", [])) or [],
        "mentioned_companies": sr.get("mentioned_companies", article.get("mentioned_companies", [])) or [],
        "status":              "pending",
    }


def upsert_news_item(item: dict) -> dict | None:
    """Upsert a news item to the hub DB, only overwriting if the new score is higher.

    Tries the upsert_news_item_if_higher_score RPC first; falls back to a
    manual SELECT + INSERT/UPDATE if the RPC is not available.
    Returns the upserted row on success, None on failure.
    """
    client = _get_client()
    if client is None:
        return None

    url = item.get("url", "")
    if not url:
        log.warning("upsert_news_item: item has no url — skipping.")
        return None

    # --- attempt RPC ---
    try:
        result = client.rpc(
            "upsert_news_item_if_higher_score",
            {
                "p_url":                 item["url"],
                "p_title":               item["title"],
                "p_source":              item["source"],
                "p_published_at":        item["published_at"],
                "p_score":               item["score"],
                "p_score_reason":        item["score_reason"],
                "p_summary":             item["summary"],
                "p_tags":                item["tags"],
                "p_verticals":           item["verticals"],
                "p_mentioned_companies": item["mentioned_companies"],
            },
        ).execute()
        return result.data
    except Exception as rpc_exc:
        log.debug(
            "RPC upsert_news_item_if_higher_score unavailable (%s) — using fallback.",
            rpc_exc,
        )

    # --- fallback: SELECT then INSERT or UPDATE ---
    try:
        existing = (
            client.table("news_items").select("id,score").eq("url", url).execute()
        )
        if existing.data:
            existing_score = int(existing.data[0].get("score", 0))
            if item["score"] <= existing_score:
                log.debug(
                    "Skipping upsert for '%s' — existing score %d >= new score %d",
                    url[:80], existing_score, item["score"],
                )
                return existing.data[0]
            row_id = existing.data[0]["id"]
            result = (
                client.table("news_items").update(item).eq("id", row_id).execute()
            )
        else:
            result = client.table("news_items").insert(item).execute()
        return result.data[0] if result.data else None
    except Exception as exc:
        log.error("Supabase upsert failed for '%s': %s", url[:80], exc)
        return None
