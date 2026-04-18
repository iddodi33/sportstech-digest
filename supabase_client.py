"""supabase_client.py — writes scored news items to the hub's Supabase DB."""

import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

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
        "source":              article.get("source", ""),
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
