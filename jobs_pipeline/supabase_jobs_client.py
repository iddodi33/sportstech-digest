"""supabase_jobs_client.py — job-related Supabase operations for the jobs pipeline."""

import logging
import os

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


def get_client():
    """Public wrapper for the singleton Supabase client."""
    return _get_client()


def get_linkedin_sources() -> list[dict]:
    """Return active sources for linkedin_only and none_found platforms.

    Extends the standard company join to include is_fdi and is_irish_founded,
    which the LinkedIn adapter needs to build the correct Serper query.
    Also passes through linkedin_search_name from company_careers_sources
    (selected via *) — used by the adapter when the LinkedIn-listed name
    differs from companies.name.
    """
    client = _get_client()
    if client is None:
        return []
    try:
        result = (
            client.table("company_careers_sources")
            .select("*, companies(name, is_fdi, is_irish_founded)")
            .in_("ats_platform", ["linkedin_only", "none_found"])
            .eq("is_active", True)
            .execute()
        )
        sources = []
        for row in result.data:
            company_data = row.pop("companies", None) or {}
            row["company_name"] = company_data.get("name", "")
            row["is_fdi"] = bool(company_data.get("is_fdi", False))
            row["is_irish_founded"] = bool(company_data.get("is_irish_founded", False))
            sources.append(row)
        return sources
    except Exception as exc:
        log.error("get_linkedin_sources failed: %s", exc)
        return []


def get_active_sources(platform: str) -> list[dict]:
    """Return active company_careers_sources rows for the given ATS platform.

    Joins the companies table to include company_name in each returned dict.
    Returns empty list if the client is unavailable or the query fails.
    """
    client = _get_client()
    if client is None:
        return []
    try:
        result = (
            client.table("company_careers_sources")
            .select("*, companies(name)")
            .eq("ats_platform", platform)
            .eq("is_active", True)
            .execute()
        )
        sources = []
        for row in result.data:
            company_data = row.pop("companies", None) or {}
            row["company_name"] = company_data.get("name", "")
            sources.append(row)
        return sources
    except Exception as exc:
        log.error("get_active_sources failed for platform='%s': %s", platform, exc)
        return []


def upsert_job(
    url: str,
    title: str,
    source: str,
    sources_source_id: str,
    company_id: str,
    company_name: str,
    location_raw: str | None,
    summary: str | None,
    salary_range: str | None,
) -> dict:
    """Upsert a job record via the upsert_job RPC.

    Returns {id, was_inserted, was_reactivated} on success.
    Returns {} on failure — all errors are logged, never raised.
    """
    client = _get_client()
    if client is None:
        return {}
    try:
        result = client.rpc(
            "upsert_job",
            {
                "p_url": url,
                "p_title": title,
                "p_source": source,
                "p_sources_source_id": sources_source_id,
                "p_company_id": company_id,
                "p_company_name": company_name,
                "p_location_raw": location_raw,
                "p_summary": summary,
                "p_salary_range": salary_range,
            },
        ).execute()
        data = result.data
        if isinstance(data, list):
            return data[0] if data else {}
        return data or {}
    except Exception as exc:
        log.error("upsert_job RPC failed for '%s': %s", url[:80], exc)
        return {}
