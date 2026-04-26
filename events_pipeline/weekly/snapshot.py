"""snapshot.py — fetch current events DB state for the weekly summary email."""

import logging
from datetime import date

log = logging.getLogger(__name__)


def fetch_snapshot(client) -> dict:
    """Query hub Supabase for a current events pipeline health snapshot.

    Returns a dict with keys:
      verified_upcoming, pending_review, rejected_lifetime
    """
    snapshot = {
        "verified_upcoming": 0,
        "pending_review": 0,
        "rejected_lifetime": 0,
    }
    today = date.today().isoformat()

    try:
        resp = (
            client.table("events")
            .select("id", count="exact")
            .eq("status", "verified")
            .gte("date", today)
            .execute()
        )
        snapshot["verified_upcoming"] = resp.count if resp.count is not None else len(resp.data)
    except Exception as exc:
        log.warning("snapshot: verified_upcoming count failed: %s", exc)

    try:
        resp = (
            client.table("events")
            .select("id", count="exact")
            .eq("status", "pending")
            .execute()
        )
        snapshot["pending_review"] = resp.count if resp.count is not None else len(resp.data)
    except Exception as exc:
        log.warning("snapshot: pending_review count failed: %s", exc)

    try:
        resp = (
            client.table("events")
            .select("id", count="exact")
            .eq("status", "rejected")
            .execute()
        )
        snapshot["rejected_lifetime"] = resp.count if resp.count is not None else len(resp.data)
    except Exception as exc:
        log.warning("snapshot: rejected_lifetime count failed: %s", exc)

    log.info(
        "Snapshot: %d verified upcoming, %d pending, %d rejected",
        snapshot["verified_upcoming"], snapshot["pending_review"], snapshot["rejected_lifetime"],
    )
    return snapshot
