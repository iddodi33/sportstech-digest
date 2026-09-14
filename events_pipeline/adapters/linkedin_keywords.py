"""linkedin_keywords.py — 6th event source: LinkedIn, searched by keyword and by
a curated list of organiser pages.

Why this exists
---------------
The other 5 event adapters watch 5 known listing sites. Meath Sports
Partnership's Women in Sport Conference 2026 was never on any of them, was not
found by a manual web search while building the newsletter, and would not have
been caught by the hub's LinkedIn Radar either — that radar tracks companies
already known to the hub, and a county Local Sports Partnership is not one. It
surfaced only because Iddo happened to see it on LinkedIn. This adapter searches
LinkedIn by topic and by organiser rather than by already-known-company.

What it deliberately does NOT do
--------------------------------
- It does not touch public.events. Output lands in public.event_leads, a
  separate review queue, because a keyword-matched LinkedIn post is a much
  weaker signal than a structured listing page. Nothing reaches events.* except
  by a human promoting a lead.
- It does not run Claude over the posts. v1 produces a raw list of links for
  manual triage; structured extraction can be layered on once the real
  false-positive rate is known rather than guessed at.
- It does not scrape /in/ profile pages. Posts and company/organiser pages only.

Actor: harvestapi/linkedin-post-search (module constant, swappable).
PAY_PER_EVENT, $0.002/post at BRONZE tier, no login or cookies required —
pricing re-verified against the actor's own pricing block 2026-09-14. Called via
plain `requests` against the Apify REST API, no vendor SDK, consistent with
jobs_pipeline/adapters/apify_linkedin.py, whose retry/backoff, `_squash()` and
`_last_audit` patterns this module mirrors.

Async run shape, and why it isn't the sibling's one-shot call
------------------------------------------------------------
jobs_pipeline/adapters/apify_linkedin.py uses /run-sync-get-dataset-items, which
hands back the items in a single call — but with no run id, and therefore no way
to know what the call cost. The other synchronous endpoint, /run-sync, returns
the actor's OUTPUT key-value record, not the run object, so it is no better.
This module therefore POSTs to /runs, polls /actor-runs/{id} to completion, GETs
the dataset, and re-reads the run for its billed charges. Several cheap HTTP
calls buy a real per-run dollar figure instead of arithmetic over rates we
hardcoded, which is the entire point of the telemetry this feeds (see
run_telemetry.record_apify_run) and the only thing standing in for a cost
ceiling. See charged_cost_usd() for which of the run object's two cost fields is
authoritative and why, and _settle_charges() for why the read is separate.

One actor call per keyword, one per seed author
-----------------------------------------------
`maxPosts` is documented as per-search-query, so batching queries into one call
makes the per-query volume — and therefore the per-query cost — unpredictable.
Per-query calls also keep attribution unambiguous: the actor echoes the query
back on each item as `query.search`, but only for keyword runs, and a batched
authorUrls run gives no per-author breakdown at all. Since "which keyword is
earning its cost" is the first question anyone will ask of this data, the calls
stay split. Volume is low by design (7 keywords x 3 geographies + ~17 seeds).

NO COST CEILING — this is deliberate and is a knowing exception
---------------------------------------------------------------
CLAUDE.md's house rule is that unattended cron scripts get an aborting cost
ceiling. This adapter has none, on Iddo's explicit call: "let's run it and see
where it was sent" before deciding whether a ceiling belongs here. Flagged here
so the omission is read as a decision rather than an oversight. What stands in
for the ceiling meanwhile: MAX_POSTS_PER_QUERY caps the actor-side volume of
every call, the query set is a fixed-length list rather than anything derived at
runtime, and every call's real billed cost is written to
scripts/data/apify_spend.jsonl so the question can be answered from data after
one or two Friday runs. Revisit once that log has something in it.
"""

from __future__ import annotations

import csv
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# Public: the entry point records this on every telemetry row.
ACTOR = "harvestapi~linkedin-post-search"
_RUN_START_URL = f"https://api.apify.com/v2/acts/{ACTOR}/runs"
_RUN_DETAIL_URL = "https://api.apify.com/v2/actor-runs/{run_id}"
_DATASET_ITEMS_URL = "https://api.apify.com/v2/datasets/{dataset_id}/items"

# Apify caps waitForFinish at 60s per call, so a long run is several polls.
_WAIT_FOR_FINISH_SECONDS = 60
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})

# How long, and how many times, to wait before re-reading a run whose
# pay-per-event charges have not landed yet. See _settle_charges().
_CHARGE_SETTLE_SECONDS = 3.0
_CHARGE_SETTLE_ATTEMPTS = 2

SOURCE_NAME = "linkedin_keywords"

_SEED_AUTHORS_CSV = Path(__file__).resolve().parent.parent / "data" / "linkedin_seed_authors.csv"

# Max posts the actor is asked for per query. The effective volume cap in the
# absence of a dollar ceiling — see the module docstring.
MAX_POSTS_PER_QUERY = int(os.getenv("LINKEDIN_LEADS_MAX_POSTS", "25"))

# Only posts from the past week. The adapter runs weekly, so a wider window
# just re-pays $0.002 for posts already in event_leads.
POSTED_LIMIT = os.getenv("LINKEDIN_LEADS_POSTED_LIMIT", "week")

# Actor-side run timeout (seconds). A query that has not returned by then is
# almost certainly wedged rather than slow.
_ACTOR_TIMEOUT_SECONDS = 300

# Base keywords. Grounded in the hub's own verified events, not guessed cold.
# Every one carries a sport qualifier: the existing adapters already over-scrape
# generic AI/tech noise (64 of the hub's 149 rejected events are tagged
# ai_tech_ireland_auto_reject), and an unqualified "AI" or "tech" keyword search
# on LinkedIn would be strictly worse than that.
KEYWORDS = [
    "sportstech",
    "sports technology",
    "sport innovation",
    "women in sport",
    "sport and AI",
    "sports data",
    "sports analytics conference",
]

# Geography qualifiers appended to each keyword. Broad on purpose — Iddo's
# explicit call. Note this multiplies the query count (and the cost) by 3.
GEOGRAPHIES = ["Ireland", "UK", "Europe"]

# Transient-failure retry, same shape as jobs_pipeline/adapters/apify_linkedin.py.
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 5.0


class ApifyTokenMissingError(Exception):
    """APIFY_TOKEN not set — abort run, do not crash."""


class ApifyRequestError(Exception):
    """Apify call failed for one query — skip that query, do not abort.

    `run_id` is set when the failure happened *after* an actor run had already
    been started, so a retry can resume that run instead of starting (and
    paying for) a second one.
    """

    def __init__(self, message: str, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class ApifyTransientError(ApifyRequestError):
    """Retryable Apify failure (5xx / 429 / network), after retries were exhausted."""


def _squash(body: str, limit: int = 120) -> str:
    """Collapse an error body to one short line.

    Apify's 502 returns a full nginx HTML page; stored verbatim that makes a log
    line an unreadable multi-line blob.
    """
    text = re.sub(r"<[^>]+>", " ", body or "")
    text = " ".join(text.split())
    return text[:limit] if text else "(empty body)"


def charged_cost_usd(run: dict) -> float | None:
    """Cost of one run, from Apify's own event counts x Apify's own event prices.

    Both halves come off the run object: `chargedEventCounts` (how many posts,
    actor starts and 0-result queries were billed) and
    `pricingInfo.pricingPerEvent.actorChargeEvents.*.eventPriceUsd` (what each
    costs for this account's tier). Nothing here is hardcoded, so a tier change
    or a vendor price change is picked up automatically.

    Why not just read `usageTotalUsd`: the two fields settle at different speeds
    after a run reports SUCCEEDED. `chargedEventCounts` is populated within a
    second or two; `usageTotalUsd` lags far behind it and reads as little as the
    actor-start charge alone in the meantime. Measured 2026-09-14: a 10-post run
    read $0.0001 immediately after completion and $0.02005 a minute later — and
    $0.02005 is exactly what this function computes from the event counts at the
    first read. The derived figure is not an estimate standing in for the real
    one; it is the same arithmetic Apify itself does, run earlier.

    Returns None when the run carries no pricing block, so the caller can fall
    back to `usageTotalUsd` rather than silently reporting zero.
    """
    counts = run.get("chargedEventCounts") or {}
    prices = (
        (run.get("pricingInfo") or {})
        .get("pricingPerEvent", {})
        .get("actorChargeEvents", {})
    )
    if not prices:
        return None
    total = 0.0
    for event, count in counts.items():
        price = (prices.get(event) or {}).get("eventPriceUsd")
        if price is None or not count:
            continue
        total += float(price) * int(count)
    return round(total, 6)


def build_keyword_queries() -> list[str]:
    """Cartesian product of KEYWORDS x GEOGRAPHIES, as LinkedIn search strings."""
    return [f"{kw} {geo}" for kw in KEYWORDS for geo in GEOGRAPHIES]


def load_seed_authors(path: Path | None = None) -> list[dict]:
    """Read enabled seed author rows from the reviewed CSV.

    Returns [] (with a warning) if the file is missing — a missing seed list
    degrades this adapter to keyword-only rather than aborting the run, the same
    way a missing APIFY_TOKEN degrades the jobs pipeline's linkedin_apify step.

    Comment (#) and blank lines before and between rows are skipped, so a seed
    can be parked with an explanatory note instead of being deleted.
    """
    csv_path = path or _SEED_AUTHORS_CSV
    if not csv_path.exists():
        log.warning("linkedin_keywords: seed author CSV not found at %s", csv_path)
        return []

    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        rows = list(csv.DictReader(lines))
    except Exception as exc:
        log.error("linkedin_keywords: failed to read %s: %s", csv_path, exc)
        return []

    seeds = []
    for row in rows:
        url = (row.get("linkedin_url") or "").strip()
        enabled = (row.get("enabled") or "").strip().lower() == "true"
        if not url or not enabled:
            continue
        if "/company/" not in url:
            # Out of scope by decision: posts and company/organiser pages only.
            log.warning(
                "linkedin_keywords: skipping non-company seed URL %r (%s)",
                url, row.get("name", ""),
            )
            continue
        seeds.append({
            "name": (row.get("name") or "").strip(),
            "linkedin_url": url,
            "category": (row.get("category") or "").strip(),
        })

    log.info("linkedin_keywords: loaded %d enabled seed authors from %s", len(seeds), csv_path.name)
    return seeds


def _post_id_from(item: dict) -> str:
    """The actor's own post id, falling back to the post URL's activity id."""
    raw_id = str(item.get("id") or "").strip()
    if raw_id:
        return raw_id
    m = re.search(r"activity-(\d+)", item.get("linkedinUrl") or "")
    return m.group(1) if m else ""


def _parse_posted_at(value: object) -> str | None:
    """Normalise the actor's postedAt.date to an ISO 8601 string, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


class LinkedInKeywordsAdapter:
    """Search LinkedIn posts by keyword and by seed organiser page.

    Does NOT subclass BaseEventAdapter. That base class's contract is
    discover_event_urls() -> list[str], and every URL it returns is handed to the
    Claude extractor and upserted into public.events — which is exactly the
    behaviour this adapter is specified not to have. Sharing the base class would
    make it one careless registry edit away from doing so. It is wired into
    events_weekly.yml as its own step instead (see run_linkedin_leads.py).

    Populates self._last_audit after each run for dry-run reporting, mirroring
    the audit shape in jobs_pipeline/adapters/apify_linkedin.py.
    """

    source_name = SOURCE_NAME

    def __init__(self, seed_authors_path: Path | None = None) -> None:
        self._token: str = os.getenv("APIFY_TOKEN", "")
        self.abort: bool = False
        self._last_audit: dict = {}
        self.transient_retries: int = 0
        self.failed_queries: list[tuple[str, str]] = []
        self._seed_authors_path = seed_authors_path

    # ── Apify call ────────────────────────────────────────────────────────────

    def _request_json(self, method: str, url: str, **kwargs) -> dict:
        """One Apify REST call returning its `data` envelope.

        Raises ApifyTransientError on network errors and retryable HTTP statuses;
        ApifyRequestError on everything else.
        """
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise ApifyTransientError(f"network error: {exc}") from exc

        if resp.status_code not in (200, 201):
            detail = f"HTTP {resp.status_code}: {_squash(resp.text)}"
            if resp.status_code in _RETRY_STATUS:
                raise ApifyTransientError(detail)
            raise ApifyRequestError(detail)

        try:
            return resp.json().get("data") or {}
        except ValueError as exc:
            raise ApifyRequestError(f"invalid JSON: {exc}") from exc

    def _call_actor_once(
        self, payload: dict, existing_run_id: str | None = None,
    ) -> tuple[list[dict], dict]:
        """Start the actor (or resume a started run), wait, then read its dataset.

        Returns (items, run_object), the run object carrying the charge fields
        the telemetry needs — see charged_cost_usd() for which one is used.

        Three-step rather than one-shot because neither synchronous endpoint
        gives both halves: /run-sync returns the actor's OUTPUT key-value record
        (not the run object at all), and /run-sync-get-dataset-items returns the
        items with no run id and therefore no cost. So: POST /runs to start,
        poll /actor-runs/{id} to completion, GET the dataset.

        `existing_run_id` is how a retry avoids paying twice. Every failure past
        the start POST carries the run id on the exception, and _call_actor feeds
        it back in; without that, a blip while polling or fetching the dataset
        would start a second billed run for a query already paid for. That
        matters more here than in the sibling jobs adapter because this path has
        no cost ceiling to catch the duplicate.

        Raises ApifyTransientError on network errors and retryable HTTP statuses;
        ApifyRequestError on everything else.
        """
        if existing_run_id:
            run_id = existing_run_id
            run = {"id": run_id, "status": "RUNNING"}
            log.info("linkedin_keywords: resuming already-started run %s", run_id)
        else:
            run = self._request_json(
                "POST",
                _RUN_START_URL,
                params={
                    "token": self._token,
                    "timeout": _ACTOR_TIMEOUT_SECONDS,
                    "waitForFinish": _WAIT_FOR_FINISH_SECONDS,
                },
                json=payload,
                timeout=_WAIT_FOR_FINISH_SECONDS + 30,
            )
            run_id = run.get("id")
            if not run_id:
                raise ApifyRequestError("run start returned no run id")

        deadline = time.time() + _ACTOR_TIMEOUT_SECONDS + 60
        while run.get("status") not in _TERMINAL_STATUSES:
            if time.time() > deadline:
                raise ApifyTransientError(
                    f"run {run_id} still {run.get('status')} after "
                    f"{_ACTOR_TIMEOUT_SECONDS + 60}s",
                    run_id=run_id,
                )
            try:
                run = self._request_json(
                    "GET",
                    _RUN_DETAIL_URL.format(run_id=run_id),
                    params={"token": self._token, "waitForFinish": _WAIT_FOR_FINISH_SECONDS},
                    timeout=_WAIT_FOR_FINISH_SECONDS + 30,
                )
            except ApifyRequestError as exc:
                exc.run_id = run_id
                raise

        status = run.get("status")
        if status != "SUCCEEDED":
            detail = f"run {run_id} finished {status}"
            # An aborted or timed-out run is the platform misbehaving rather
            # than our input being wrong, so it is worth another attempt — and
            # that attempt does need a fresh run, since this one is over. A
            # FAILED run means the actor rejected the input and will again.
            if status in ("ABORTED", "TIMED-OUT"):
                raise ApifyTransientError(detail)
            raise ApifyRequestError(detail)

        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise ApifyRequestError("run object carried no defaultDatasetId", run_id=run_id)

        try:
            items_resp = requests.get(
                _DATASET_ITEMS_URL.format(dataset_id=dataset_id),
                params={"token": self._token, "clean": "true", "format": "json"},
                timeout=120,
            )
        except requests.exceptions.RequestException as exc:
            raise ApifyTransientError(
                f"dataset fetch network error: {exc}", run_id=run_id,
            ) from exc

        if items_resp.status_code != 200:
            detail = f"dataset HTTP {items_resp.status_code}: {_squash(items_resp.text)}"
            if items_resp.status_code in _RETRY_STATUS:
                raise ApifyTransientError(detail, run_id=run_id)
            raise ApifyRequestError(detail, run_id=run_id)

        try:
            items = items_resp.json()
        except ValueError as exc:
            raise ApifyRequestError(
                f"invalid JSON dataset: {exc}", run_id=run_id,
            ) from exc

        if not isinstance(items, list):
            raise ApifyRequestError(
                f"unexpected dataset shape: {type(items).__name__}", run_id=run_id,
            )

        return items, self._settle_charges(run_id, run, len(items))

    def _settle_charges(self, run_id: str, run: dict, item_count: int) -> dict:
        """Re-read the run once its pay-per-event charges have landed.

        `usageTotalUsd` is not final the instant a run reports SUCCEEDED — the
        per-event charges settle slightly behind it. The first real call made
        from this module logged $0.0001 for a run Apify ultimately billed at
        $0.02005 (10 posts), purely because it read the figure at the moment the
        status flipped. Under-reporting cost in the one log that exists to make
        cost visible is the worst possible bug here, so the run is re-read after
        the dataset fetch, and re-read once more if the post charge still has
        not appeared for a run that plainly returned posts.

        Never raises: a settled cost is worth a retry, not the whole query.
        """
        for attempt in range(_CHARGE_SETTLE_ATTEMPTS):
            try:
                fresh = self._request_json(
                    "GET",
                    _RUN_DETAIL_URL.format(run_id=run_id),
                    params={"token": self._token},
                    timeout=60,
                )
            except ApifyRequestError as exc:
                log.warning(
                    "linkedin_keywords: could not re-read run %s for final cost "
                    "(%s) — cost for this query may be understated",
                    run_id, exc,
                )
                return run

            if fresh:
                run = fresh
            charged_posts = (run.get("chargedEventCounts") or {}).get("post", 0)
            if item_count == 0 or charged_posts:
                return run
            if attempt == 0:
                time.sleep(_CHARGE_SETTLE_SECONDS)

        log.warning(
            "linkedin_keywords: run %s returned %d posts but reports no post "
            "charge — $%s is a floor, not the settled cost",
            run_id, item_count, run.get("usageTotalUsd"),
        )
        return run

    def _call_actor(self, payload: dict, label: str) -> tuple[list[dict], dict]:
        """POST to the actor, retrying transient failures with backoff.

        Non-transient failures (bad token, 4xx, malformed body) raise on the
        first attempt — retrying those just repeats the same failure.
        """
        last_exc: ApifyRequestError | None = None
        # Set once a run has been started, so a retry resumes it rather than
        # starting a second billed run. See _call_actor_once.
        started_run_id: str | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                return self._call_actor_once(payload, existing_run_id=started_run_id)
            except ApifyTransientError as exc:
                last_exc = exc
                started_run_id = exc.run_id
                if attempt == _MAX_ATTEMPTS:
                    break
                delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.25)
                log.warning(
                    "linkedin_keywords: transient failure on %s (attempt %d/%d): %s "
                    "— retrying in %.1fs",
                    label, attempt, _MAX_ATTEMPTS, exc, delay,
                )
                self.transient_retries += 1
                time.sleep(delay)

        if last_exc is None:  # unreachable: loop exits via return or a caught transient
            raise ApifyRequestError("actor call failed with no recorded error")

        log.error(
            "linkedin_keywords: giving up on %s after %d attempts: %s",
            label, _MAX_ATTEMPTS, last_exc,
        )
        raise last_exc

    # ── Normalisation ─────────────────────────────────────────────────────────

    def _to_lead(self, item: dict, matched_by: str, matched_value: str) -> dict | None:
        """Map one actor item to an event_leads row. None if unusable."""
        post_id = _post_id_from(item)
        post_url = (item.get("linkedinUrl") or "").strip()
        if not post_id or not post_url:
            return None

        author = item.get("author") or {}
        posted = item.get("postedAt") or {}
        engagement = item.get("engagement") or {}

        return {
            "linkedin_post_id": post_id,
            "post_url": post_url,
            "author_name": (author.get("name") or "").strip() or None,
            "author_linkedin_url": (author.get("linkedinUrl") or "").strip() or None,
            "author_type": (author.get("type") or "").strip() or None,
            "matched_by": matched_by,
            "matched_value": matched_value,
            "content": item.get("content") or None,
            "posted_at": _parse_posted_at(posted.get("date")),
            "reactions": int(engagement.get("likes") or 0),
            "comments": int(engagement.get("comments") or 0),
            "raw": item,
        }

    # ── Query execution ───────────────────────────────────────────────────────

    def _run_query(self, payload: dict, matched_by: str, matched_value: str) -> dict:
        """Run one actor query and return a per-query result dict.

        Never raises for a per-query failure — a wedged keyword must not cost the
        run its other 20-odd queries. Failures are recorded in failed_queries and
        in the returned dict, and are still logged to telemetry by the caller,
        because a failed call can still have been billed.
        """
        label = f"{matched_by}={matched_value!r}"
        result = {
            "matched_by": matched_by,
            "matched_value": matched_value,
            "leads": [],
            "posts_fetched": 0,
            "run_id": "",
            "usage_total_usd": None,
            "usage_reported_usd": None,
            "charged_events": {},
            "status": "ok",
            "error": "",
        }

        try:
            items, run = self._call_actor(payload, label)
        except ApifyRequestError as exc:
            result["status"] = "transient_exhausted" if isinstance(exc, ApifyTransientError) else "error"
            result["error"] = str(exc)
            self.failed_queries.append((label, str(exc)))
            log.warning("linkedin_keywords: %s failed (%s): %s", label, result["status"], exc)
            return result

        result["run_id"] = run.get("id", "") or ""
        reported = run.get("usageTotalUsd")
        reported = float(reported) if reported is not None else None
        derived = charged_cost_usd(run)
        # Prefer the derived figure: same arithmetic Apify does, but available
        # now rather than a minute after the run ends (see charged_cost_usd).
        # Both are kept so a later reader can see the two agree — or catch it if
        # they ever stop agreeing, which would mean the pricing block moved.
        result["usage_total_usd"] = derived if derived is not None else reported
        result["usage_reported_usd"] = reported
        result["charged_events"] = run.get("chargedEventCounts") or {}
        result["posts_fetched"] = len(items)

        leads = []
        for item in items:
            lead = self._to_lead(item, matched_by, matched_value)
            if lead is not None:
                leads.append(lead)
        result["leads"] = leads

        log.info(
            "linkedin_keywords: %s → %d posts, %d usable, $%s",
            label, len(items), len(leads),
            f"{result['usage_total_usd']:.4f}" if result["usage_total_usd"] is not None else "?",
        )
        return result

    def fetch_keyword(self, query: str) -> dict:
        """Run one keyword+geography search query."""
        return self._run_query(
            {
                "searchQueries": [query],
                "maxPosts": MAX_POSTS_PER_QUERY,
                "postedLimit": POSTED_LIMIT,
                "sortBy": "date",
                "profileScraperMode": "short",
            },
            matched_by="keyword",
            matched_value=query,
        )

    def fetch_seed_author(self, seed: dict) -> dict:
        """Fetch recent posts from one seed organiser page.

        No searchQueries here: seed pages are curated organisers, so everything
        they posted this week is worth a look. Adding a keyword filter would
        re-introduce exactly the blind spot this adapter exists to close — the
        Meath conference post does not contain the word "sportstech".
        """
        return self._run_query(
            {
                "authorUrls": [seed["linkedin_url"]],
                "maxPosts": MAX_POSTS_PER_QUERY,
                "postedLimit": POSTED_LIMIT,
                "sortBy": "date",
                "profileScraperMode": "short",
            },
            matched_by="seed_author",
            matched_value=seed["linkedin_url"],
        )

    # ── run() ─────────────────────────────────────────────────────────────────

    def run(
        self,
        *,
        keywords_only: bool = False,
        authors_only: bool = False,
        limit: int | None = None,
    ) -> dict:
        """Run every query, dedupe across them, return leads plus an audit.

        Deduping is by linkedin_post_id across the whole run. The same post
        routinely matches several keywords — and that overlap is itself a triage
        signal, so every matcher is kept in the lead's `matched_all` rather than
        the first one winning silently.

        Raises ApifyTokenMissingError if APIFY_TOKEN is unset — a run-wide
        condition, not a per-query one.
        """
        if not self._token:
            self.abort = True
            raise ApifyTokenMissingError("APIFY_TOKEN is not set")

        queries: list[tuple[str, object]] = []
        if not authors_only:
            queries += [("keyword", q) for q in build_keyword_queries()]
        if not keywords_only:
            queries += [("seed_author", s) for s in load_seed_authors(self._seed_authors_path)]

        if limit is not None:
            queries = queries[:limit]

        audit = {
            "queries_run": 0,
            "queries_failed": 0,
            "keyword_queries": 0,
            "seed_author_queries": 0,
            "posts_fetched": 0,
            "posts_from_keywords": 0,
            "posts_from_seed_authors": 0,
            "unique_leads": 0,
            "usage_total_usd": 0.0,
            "usage_unreported_queries": 0,
            "per_query": [],
        }

        by_post_id: dict[str, dict] = {}

        for kind, target in queries:
            if kind == "keyword":
                result = self.fetch_keyword(str(target))
                audit["keyword_queries"] += 1
                audit["posts_from_keywords"] += result["posts_fetched"]
            else:
                result = self.fetch_seed_author(dict(target))  # type: ignore[arg-type]
                audit["seed_author_queries"] += 1
                audit["posts_from_seed_authors"] += result["posts_fetched"]

            audit["queries_run"] += 1
            audit["posts_fetched"] += result["posts_fetched"]
            if result["status"] != "ok":
                audit["queries_failed"] += 1

            if result["usage_total_usd"] is not None:
                audit["usage_total_usd"] += result["usage_total_usd"]
            else:
                # Apify did not report a cost for this call. Counted rather than
                # treated as zero, so the run total is never quietly understated.
                audit["usage_unreported_queries"] += 1

            audit["per_query"].append({
                "matched_by": result["matched_by"],
                "matched_value": result["matched_value"],
                "posts_fetched": result["posts_fetched"],
                "usage_total_usd": result["usage_total_usd"],
                "usage_reported_usd": result["usage_reported_usd"],
                "charged_events": result["charged_events"],
                "run_id": result["run_id"],
                "status": result["status"],
                "error": result["error"],
            })

            for lead in result["leads"]:
                post_id = lead["linkedin_post_id"]
                existing = by_post_id.get(post_id)
                matcher = f"{lead['matched_by']}:{lead['matched_value']}"
                if existing is None:
                    lead["matched_all"] = [matcher]
                    by_post_id[post_id] = lead
                elif matcher not in existing["matched_all"]:
                    existing["matched_all"].append(matcher)

        audit["unique_leads"] = len(by_post_id)
        audit["usage_total_usd"] = round(audit["usage_total_usd"], 6)
        self._last_audit = audit

        log.info(
            "linkedin_keywords: %d queries (%d keyword, %d seed author), "
            "%d posts fetched, %d unique leads, $%.4f billed",
            audit["queries_run"], audit["keyword_queries"], audit["seed_author_queries"],
            audit["posts_fetched"], audit["unique_leads"], audit["usage_total_usd"],
        )
        if audit["queries_failed"]:
            log.warning("linkedin_keywords: %d queries failed", audit["queries_failed"])

        return {"leads": list(by_post_id.values()), "audit": audit}
