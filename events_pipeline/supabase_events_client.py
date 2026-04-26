"""supabase_events_client.py — event-related Supabase operations for the events pipeline."""

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


def get_supabase_client():
    """Public wrapper for the singleton Supabase client."""
    return _get_client()


# Valid source identifiers for the events pipeline.
VALID_SOURCES = frozenset({
    "sport_for_business",
    "irish_geeks_united",
    "eventbrite_ireland",
    "sportstech_ireland",
    "tu_dublin",
    "manual",
    "test",
})


def upsert_event(extraction: dict, source: str) -> tuple[str | None, bool]:
    """Insert or update an event record via the upsert_event_if_new RPC.

    Falls back to a manual SELECT + INSERT/UPDATE if the RPC is unavailable,
    mirroring the fallback pattern used in supabase_client.py for news items.

    Returns (event_id, was_inserted).
    Returns (None, False) on any failure — errors are logged, never raised.
    """
    client = _get_client()
    if client is None:
        return None, False

    url = extraction.get("url", "")
    if not url:
        log.warning("upsert_event: extraction has no url — skipping")
        return None, False

    # ── Attempt RPC ───────────────────────────────────────────────────────────
    try:
        result = client.rpc(
            "upsert_event_if_new",
            {
                "p_url":         url,
                "p_name":        extraction.get("name"),
                "p_date":        extraction.get("date"),
                "p_end_date":    extraction.get("end_date"),
                "p_start_time":  extraction.get("start_time"),
                "p_location":    extraction.get("location"),
                "p_area":        extraction.get("area"),
                "p_format":      extraction.get("format"),
                "p_organiser":   extraction.get("organiser"),
                "p_description": extraction.get("description"),
                "p_image_url":   extraction.get("image_url"),
                "p_recurrence":  extraction.get("recurrence"),
                "p_source":      source,
                "p_extraction":  extraction,
            },
        ).execute()
        data = result.data
        if isinstance(data, list) and data:
            row = data[0]
            return row.get("id"), bool(row.get("was_inserted", False))
        return None, False
    except Exception as rpc_exc:
        log.debug(
            "upsert_event_if_new RPC unavailable (%s) — using fallback",
            rpc_exc,
        )

    # ── Fallback: manual SELECT + INSERT/UPDATE ────────────────────────────
    try:
        existing = (
            client.table("events")
            .select("id, status")
            .eq("url", url)
            .execute()
        )
        now_ts = datetime.now(timezone.utc).isoformat()

        if existing.data:
            row = existing.data[0]
            event_id = row["id"]
            current_status = row.get("status", "pending")

            # Always update last_seen_at and extraction audit log.
            update = {"last_seen_at": now_ts, "extraction": extraction}

            # Only overwrite content fields if admin hasn't reviewed yet.
            if current_status == "pending":
                update.update({
                    "name":        extraction.get("name"),
                    "date":        extraction.get("date"),
                    "end_date":    extraction.get("end_date"),
                    "start_time":  extraction.get("start_time"),
                    "location":    extraction.get("location"),
                    "area":        extraction.get("area"),
                    "format":      extraction.get("format"),
                    "organiser":   extraction.get("organiser"),
                    "description": extraction.get("description"),
                    "image_url":   extraction.get("image_url"),
                    "recurrence":  extraction.get("recurrence"),
                })

            client.table("events").update(update).eq("id", event_id).execute()
            return event_id, False

        else:
            insert_data = {
                "url":         url,
                "name":        extraction.get("name"),
                "date":        extraction.get("date"),
                "end_date":    extraction.get("end_date"),
                "start_time":  extraction.get("start_time"),
                "location":    extraction.get("location"),
                "area":        extraction.get("area"),
                "format":      extraction.get("format"),
                "organiser":   extraction.get("organiser"),
                "description": extraction.get("description"),
                "image_url":   extraction.get("image_url"),
                "recurrence":  extraction.get("recurrence"),
                "source":      source,
                "extraction":  extraction,
                "status":      "pending",
                "scraped_at":  now_ts,
                "first_seen_at": now_ts,
                "last_seen_at":  now_ts,
            }
            result = client.table("events").insert(insert_data).execute()
            if result.data:
                return result.data[0]["id"], True
            return None, False

    except Exception as exc:
        log.error("upsert_event fallback failed for '%s': %s", url[:80], exc)
        return None, False
