"""apify_linkedin.py — LinkedIn job scraper adapter for `linkedin_only` sources, via Apify.

Companion to adapters/linkedin.py (Serper-based, covers `none_found` sources).
Serper reads Google's cached index and can surface snippets for jobs that
closed long ago; the Apify LinkedIn Jobs Scraper actor queries LinkedIn's own
public /jobs/search endpoint directly, which structurally never returns
closed postings — so liveness is guaranteed by construction, and the
freshness gate here is a "how recently posted" refinement, not an
existence check (see MAX_JOB_AGE_DAYS below).

Actor: curious_coder/linkedin-jobs-scraper (module constant, swappable).
Called via plain `requests` against the Apify REST API — no vendor SDK,
consistent with how adapters/linkedin.py calls Serper directly.

One actor call per company (not batched across all `linkedin_only` sources):
the actor's output items don't carry a field tying a row back to its
originating input URL, so per-company calls keep attribution unambiguous.
Low volume by design — only ~12 linkedin_only sources exist.

Missing APIFY_TOKEN is a clean-failure condition, not a crash: the token is
read lazily (not in __init__, unlike the Serper adapter's SERPER_API_KEY
check) so constructing the adapter never raises. fetch() raises
_ApifyTokenMissingError, which run() treats as an abort signal (same
severity as _SerperAuthError in linkedin.py) — logged once, source error
recorded, run() returns cleanly.
"""

import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import requests

from .base import BaseAdapter, dedupe_identical_listings
from .linkedin import (
    _canonical_url,
    _format_salary,
    _names_match,
    _normalise_company_name,
    _strip_html,
    _update_source_error,
)
from ..relevance_filter import check_relevance
from ..supabase_jobs_client import mark_job_seen, mark_source_attempted, mark_source_successful, upsert_job

log = logging.getLogger(__name__)

_ACTOR = "curious_coder~linkedin-jobs-scraper"
_ACTOR_URL = f"https://api.apify.com/v2/acts/{_ACTOR}/run-sync-get-dataset-items"

# Max jobs requested from the actor per search URL. Kept small — this path
# only exists to catch a handful of recent postings per company, not to
# exhaustively mirror LinkedIn's index.
_COUNT_PER_URL = 25

# LinkedIn's own f_TPR "past month" preset (seconds). Coarse pre-filter
# applied actor-side; MAX_JOB_AGE_DAYS below is the precise, configurable
# gate applied in Python against the actor's postedAt field.
_F_TPR_PAST_MONTH = "r2592000"

# Configurable freshness gate. Default 30 per the brief; override via env
# for ops tuning without a code change.
MAX_JOB_AGE_DAYS = int(os.getenv("MAX_JOB_AGE_DAYS", "30"))

_UK_LOCATION = "United Kingdom"
_IE_LOCATION = "Ireland"


class _ApifyTokenMissingError(Exception):
    """APIFY_TOKEN not set — abort run, do not crash."""


class _ApifyRequestError(Exception):
    """Apify API call failed (network/HTTP/parse) for one company — skip, do not abort."""


def _build_search_url(keywords: str, location: str) -> str:
    return (
        "https://www.linkedin.com/jobs/search/?"
        f"keywords={quote(keywords)}&location={quote(location)}&f_TPR={_F_TPR_PAST_MONTH}"
    )


def _parse_posted_at(value: object) -> int | None:
    """Return how many days ago a job was posted, or None if undetermined.

    Handles ISO 8601 timestamps and common relative-text forms ("3 days ago",
    "Today", "Yesterday", "Just now"). Returns None (not an error) when the
    actor's postedAt field is absent or unparseable — the caller treats None
    as "allow" for this adapter (see module docstring), unlike the stricter
    Serper adapter which treats an unparseable date as "reject".
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - dt).days, 0)
    except (ValueError, TypeError):
        pass

    lower = text.lower()
    if lower in ("just now", "today"):
        return 0
    if lower == "yesterday":
        return 1

    m = re.search(r"(\d+)\s*(hour|day|week|month|year)s?\s*ago", lower)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return {"hour": 0, "day": n, "week": n * 7, "month": n * 30, "year": n * 365}[unit]

    return None


class ApifyLinkedInAdapter(BaseAdapter):
    """Scrape linkedin_only companies via the Apify LinkedIn Jobs Scraper actor.

    Overrides BaseAdapter.run() to treat a missing APIFY_TOKEN as an abort
    signal. Populates self._last_audit after each fetch() call for dry-run
    reporting, mirroring adapters/linkedin.py's audit shape.
    """

    platform = "linkedin"

    def __init__(self) -> None:
        self._token: str = os.getenv("APIFY_TOKEN", "")
        self.abort: bool = False
        self._last_audit: dict = {}

    # ── Apify call ────────────────────────────────────────────────────────────

    def _call_actor(self, urls: list[str]) -> list[dict]:
        """POST to the Apify actor; return raw dataset items.

        Raises _ApifyRequestError on network/HTTP failure — caller treats
        this as a per-company skip, not a run-wide abort (a transient Apify
        hiccup for one company shouldn't stop the rest).
        """
        try:
            resp = requests.post(
                _ACTOR_URL,
                params={"token": self._token},
                json={"urls": urls, "count": _COUNT_PER_URL},
                timeout=180,
            )
        except requests.exceptions.RequestException as exc:
            raise _ApifyRequestError(f"network error: {exc}") from exc

        # run-sync-get-dataset-items returns 201 (Created) on a successful
        # synchronous run, not 200 — treat both as success.
        if resp.status_code not in (200, 201):
            raise _ApifyRequestError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise _ApifyRequestError(f"invalid JSON response: {exc}") from exc

        if not isinstance(data, list):
            raise _ApifyRequestError(f"unexpected response shape: {type(data).__name__}")

        return data

    # ── fetch() ───────────────────────────────────────────────────────────────

    def fetch(self, source: dict) -> list[dict]:
        """Build search URLs, call the actor, gate + normalise results.

        Raises _ApifyTokenMissingError if APIFY_TOKEN is unset.
        Raises _ApifyRequestError on Apify call failure for this company.
        Returns list of validated, upsert-ready job dicts.
        """
        if not self._token:
            raise _ApifyTokenMissingError("APIFY_TOKEN is not set")

        source_id = source.get("id")
        company_name = source.get("company_name", "")
        search_name = source.get("linkedin_search_name") or company_name
        is_fdi = bool(source.get("is_fdi", False))
        is_irish_founded = bool(source.get("is_irish_founded", False))
        is_allowlisted = bool(source.get("fdi_classifier_allowlisted", False))
        is_indigenous = not is_fdi or is_irish_founded

        if is_indigenous or not is_allowlisted:
            locations = [_IE_LOCATION]
        else:
            locations = [_IE_LOCATION, _UK_LOCATION]

        urls = [_build_search_url(search_name, loc) for loc in locations]

        audit: dict = {
            "urls_built": len(urls),
            "fetched": 0,
            "dropped_freshness": 0,
            "dropped_relevance": 0,
            "dropped_name_mismatch": 0,
            "validated": 0,
            "rejections": [],
        }

        items = self._call_actor(urls)
        audit["fetched"] = len(items)

        jobs: list[dict] = []
        for item in items:
            link = item.get("link") or item.get("jobUrl") or ""
            title = (item.get("title") or "").strip()
            if not link or not title:
                continue
            url = _canonical_url(link)

            hiring_org = (item.get("companyName") or "").strip()
            if not hiring_org:
                audit["dropped_name_mismatch"] += 1
                audit["rejections"].append((url, "no_hiring_org"))
                continue
            # Unlike linkedin.py's Serper path, `override` is never a reason to
            # skip this check here: Serper's site:linkedin.com/jobs/view "X"
            # query is a precise quoted-phrase Google search, so a bare
            # linkedin_search_name override can safely trust "any result at
            # all is close enough". The Apify actor instead runs LinkedIn's
            # own loose native keyword search (keywords=X&location=Y), which
            # surfaces anything LinkedIn's relevance ranking associates with
            # the term — not just postings at that company. Verified empirically:
            # un-gated, EA Sports/Stats Perform/Danu Sport searches returned 24/25,
            # 22/25, 25/25 unrelated companies (Sony, Rockstar, PayPal, Ryanair...).
            # `override` still changes WHICH name is compared (search_name already
            # substitutes linkedin_search_name for company_name) — just never
            # whether the comparison happens.
            norm_source = _normalise_company_name(search_name)
            norm_linkedin = _normalise_company_name(hiring_org)
            if not _names_match(norm_source, norm_linkedin):
                audit["dropped_name_mismatch"] += 1
                audit["rejections"].append(
                    (url, f"name_mismatch ({norm_source!r} != {norm_linkedin!r})")
                )
                continue

            days_ago = _parse_posted_at(item.get("postedAt"))
            if days_ago is not None and days_ago > MAX_JOB_AGE_DAYS:
                audit["dropped_freshness"] += 1
                audit["rejections"].append((url, f"stale ({days_ago}d)"))
                continue

            is_relevant, reason = check_relevance(title, source_id=source_id)
            if not is_relevant:
                audit["dropped_relevance"] += 1
                audit["rejections"].append((url, reason))
                continue

            summary = _strip_html(item.get("descriptionText") or item.get("descriptionHtml") or "") or None
            salary_range = _format_salary(item.get("salaryInfo"))
            location_raw = item.get("location") or None

            jobs.append({
                "url": url,
                "title": title,
                "location_raw": location_raw,
                "summary": summary,
                "salary_range": salary_range,
            })
            audit["validated"] += 1

        self._last_audit = audit

        log.info(
            "apify_linkedin: '%s' urls=%d fetched=%d validated=%d "
            "dropped: freshness=%d relevance=%d name_mismatch=%d",
            company_name, audit["urls_built"], audit["fetched"], audit["validated"],
            audit["dropped_freshness"], audit["dropped_relevance"], audit["dropped_name_mismatch"],
        )

        return jobs

    # ── run() override ────────────────────────────────────────────────────────

    def run(self, source: dict) -> dict:
        """Fetch + upsert with Apify-specific error handling.

        _ApifyTokenMissingError → abort (token is a run-wide condition, not
            a per-company one; no point repeating the identical failure).
        _ApifyRequestError → skip this company only, do not abort.
        """
        run_started_at = datetime.now(timezone.utc)
        source_name = source.get("company_name") or source.get("id", "unknown")
        source_id = source.get("id")
        stats = {
            "source_name": source_name,
            "jobs_found": 0,
            "inserted": 0,
            "updated": 0,
            "reactivated": 0,
            "errors": 0,
        }
        upserted_count = 0

        try:
            try:
                jobs = self.fetch(source)

            except _ApifyTokenMissingError as exc:
                log.error("apify_linkedin: %s — aborting run", exc)
                if source_id:
                    _update_source_error(source_id, "apify_token_missing")
                self.abort = True
                stats["errors"] += 1
                return stats

            except _ApifyRequestError as exc:
                log.warning("apify_linkedin: '%s' — request failed: %s", source_name, exc)
                if source_id:
                    _update_source_error(source_id, f"apify_error: {exc}")
                stats["errors"] += 1
                return stats

            except Exception as exc:
                log.error("apify_linkedin: unexpected error for '%s': %s", source_name, exc)
                stats["errors"] += 1
                return stats

            jobs = dedupe_identical_listings(jobs, source_name)
            stats["jobs_found"] = len(jobs)

            for job in jobs:
                url = (job.get("url") or "").strip()
                title = (job.get("title") or "").strip()
                if not url or not title:
                    log.warning("apify_linkedin: [%s] skipping job missing url/title", source_name)
                    stats["errors"] += 1
                    continue

                try:
                    result = upsert_job(
                        url=url,
                        title=title,
                        source=self.platform,
                        sources_source_id=source["id"],
                        company_id=source["company_id"],
                        company_name=source.get("company_name", ""),
                        location_raw=job.get("location_raw"),
                        summary=job.get("summary"),
                        salary_range=job.get("salary_range"),
                    )
                    if not result:
                        stats["errors"] += 1
                    else:
                        upserted_count += 1
                        job_id = result.get("id")
                        if job_id:
                            mark_job_seen(job_id, run_started_at)
                        if result.get("was_inserted"):
                            stats["inserted"] += 1
                        elif result.get("was_reactivated"):
                            stats["reactivated"] += 1
                        else:
                            stats["updated"] += 1
                except Exception as exc:
                    log.error("apify_linkedin: upsert_job failed for '%s': %s", url[:80], exc)
                    stats["errors"] += 1

            return stats

        finally:
            if source_id:
                if upserted_count > 0:
                    mark_source_successful(source_id, run_started_at)
                else:
                    mark_source_attempted(source_id, run_started_at)
