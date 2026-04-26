"""snapshot.py — fetch current DB state for the weekly summary email."""

import logging

log = logging.getLogger(__name__)


def fetch_snapshot(client) -> dict:
    """Query hub Supabase for a current jobs pipeline health snapshot.

    Returns a dict with keys:
      approved_jobs, pending_jobs, archived_jobs,
      pending_null_function, sources_never_scraped (list of names)
    """
    snapshot: dict = {
        "approved_jobs": 0,
        "pending_jobs": 0,
        "archived_jobs": 0,
        "pending_null_function": 0,
        "sources_never_scraped": [],
    }

    # ── Job counts by status ───────────────────────────────────────────────────
    for status, key in [
        ("approved", "approved_jobs"),
        ("pending",  "pending_jobs"),
        ("archived", "archived_jobs"),
    ]:
        try:
            resp = (
                client.table("jobs")
                .select("id", count="exact")
                .eq("status", status)
                .execute()
            )
            snapshot[key] = resp.count if resp.count is not None else len(resp.data)
        except Exception as exc:
            log.warning("snapshot: %s count failed: %s", key, exc)

    # ── Pending jobs missing job_function ──────────────────────────────────────
    try:
        resp = (
            client.table("jobs")
            .select("id", count="exact")
            .eq("status", "pending")
            .is_("job_function", "null")
            .execute()
        )
        snapshot["pending_null_function"] = resp.count if resp.count is not None else len(resp.data)
    except Exception as exc:
        log.warning("snapshot: pending_null_function count failed: %s", exc)

    # ── Active sources never scraped successfully ──────────────────────────────
    try:
        resp = (
            client.table("company_careers_sources")
            .select("id, companies(name)")
            .is_("last_successful_scrape_at", "null")
            .eq("is_active", True)
            .execute()
        )
        names: list[str] = []
        for row in resp.data:
            company = (row.get("companies") or {})
            names.append(company.get("name") or row["id"])
        snapshot["sources_never_scraped"] = names
    except Exception as exc:
        log.warning("snapshot: sources_never_scraped query failed: %s", exc)

    log.info(
        "Snapshot: %d approved, %d pending, %d archived, %d pending null-function, %d sources never scraped",
        snapshot["approved_jobs"], snapshot["pending_jobs"], snapshot["archived_jobs"],
        snapshot["pending_null_function"], len(snapshot["sources_never_scraped"]),
    )
    return snapshot
