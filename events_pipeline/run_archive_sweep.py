"""run_archive_sweep.py — reject pending events whose date has already passed.

Unlike jobs_pipeline, the events table has no analogous sweep: nothing ever
removed a stale pending event from the review queue, so past-dated rows
accumulated indefinitely (oldest found in the 2026-07-14 audit: 2025-02-24 —
see STATUS.md). Events have no `archived` status (no CHECK constraint on
events.status; only pending/rejected/verified are used in practice), so this
sweep reuses `rejected` with a distinguishing `rejected_reason` rather than
inventing a new status value the hub frontend may not render.

Usage:
  python events_pipeline/run_archive_sweep.py            # live — rejects candidates
  python events_pipeline/run_archive_sweep.py --dry-run  # log only, no writes
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from events_pipeline.supabase_events_client import get_supabase_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

REJECTED_REASON = "event_date_passed"


def run_sweep(dry_run: bool = False) -> dict:
    """Reject pending events whose `date` is before today.

    Only acts on rows with a non-null date — undated pending events are a
    separate extraction-quality issue, left untouched here. Returns a stats
    dict for the weekly email; never raises (failures logged, counted).
    """
    client = get_supabase_client()
    if client is None:
        log.error("Supabase client unavailable — cannot run archive sweep")
        return {"status": "failed", "rejected": 0, "failed": 0, "error_message": "Supabase client unavailable"}

    today = date.today().isoformat()

    try:
        result = (
            client.table("events")
            .select("id, name, date")
            .eq("status", "pending")
            .not_.is_("date", "null")
            .lt("date", today)
            .execute()
        )
        candidates = result.data
    except Exception as exc:
        log.error("Failed to fetch stale pending events: %s", exc)
        return {"status": "failed", "rejected": 0, "failed": 0, "error_message": str(exc)}

    log.info("Candidates for rejection (pending, date < %s): %d", today, len(candidates))

    rejected = 0
    failed = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for ev in candidates:
        name = ev.get("name") or "untitled"
        ev_date = ev.get("date")

        if dry_run:
            log.info("WOULD REJECT: %s — %s (date: %s)", ev["id"], name, ev_date)
            rejected += 1
            continue

        try:
            client.table("events").update({
                "status": "rejected",
                "rejected_reason": REJECTED_REASON,
                "reviewed_at": now_iso,
            }).eq("id", ev["id"]).eq("status", "pending").execute()
            log.info("REJECTED: %s — %s (date: %s)", ev["id"], name, ev_date)
            rejected += 1
        except Exception as exc:
            log.error("Failed to reject event %s: %s", ev["id"], exc)
            failed += 1

    verb = "Would reject" if dry_run else "Rejected"
    log.info("=== Archive Sweep Complete ===")
    log.info("Total stale pending events found: %d", len(candidates))
    log.info("%s: %d", verb, rejected)
    if failed:
        log.error("Failed to reject: %d", failed)

    return {
        "status": "success" if not failed else "failed",
        "rejected": rejected,
        "failed": failed,
        "error_message": None if not failed else f"{failed} update(s) failed",
    }


def main(dry_run: bool = False) -> None:
    mode = "DRY RUN" if dry_run else "live"
    log.info("Events archive sweep starting [%s]", mode)
    result = run_sweep(dry_run=dry_run)
    if result["status"] == "failed" and result.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reject pending events whose date has already passed",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be rejected without making any changes",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
