"""audit_alerts_vs_hub.py — audit Google Alerts against the SD3 news hub.

Standalone, read-only with respect to production. Uses Google Alerts as an
independent ground-truth source to find sportstech stories that daily_monitor's
RSS + Google News sources never surfaced into news_items.

Phases:
  1 fetch    — fetch a real meta-description snippet for every alert URL
  2 estimate — build the real batch prompts and count their tokens (free),
               then hard-gate on the literal input "RUN"
  3 score    — score with daily_monitor.score_articles, with a per-batch
               actual-spend circuit breaker
  4 compare  — diff scored 4/5 alerts against news_items

Usage:
    python scripts/audit_alerts_vs_hub.py --phase fetch
    python scripts/audit_alerts_vs_hub.py --phase estimate
    python scripts/audit_alerts_vs_hub.py --phase score      # prompts for RUN
    python scripts/audit_alerts_vs_hub.py --phase compare
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from supabase_client import _OG_HEADERS, _get_client, extract_publisher  # noqa: E402

try:
    import cloudscraper
except ImportError:  # pragma: no cover
    cloudscraper = None

log = logging.getLogger("audit")

# --- constants -------------------------------------------------------------

ALERTS_CSV = Path(
    r"G:\My Drive\SportsTech D3c0d3d\Newsletter\unread inbox export new\alerts part 1.csv"
)
DATA_DIR = Path(__file__).resolve().parent / "data"
SNIPPETS_FILE = DATA_DIR / "alerts_snippets.json"
SNIPPETS_LOG = DATA_DIR / "alerts_snippets.jsonl"
TOKENS_FILE = DATA_DIR / "alerts_token_estimate.json"
SCORED_CSV = DATA_DIR / "alerts_scored.csv"
MISSING_CSV = DATA_DIR / "alerts_missing_from_hub.csv"

SNIPPET_CAP = 150          # matches production's snippet[:150] in score_articles
FETCH_TIMEOUT = 10
FETCH_WORKERS = 9
FETCH_RETRIES = 1          # one retry, timeout/connection errors only

# claude-sonnet-4-5-20250929 standard (non-batch) rates, claude.com/pricing,
# verified 2026-09-03. Re-verify if this script is run much later than that.
PRICE_IN_PER_MTOK = 3.00
PRICE_OUT_PER_MTOK = 15.00
PRICING_VERIFIED_ON = "2026-09-03"
MODEL_RETIREMENT_NOT_BEFORE = "2026-09-29"

# Rubric output size per article.
OUT_TOKENS_PER_ARTICLE_LOW = 146    # score 1/2/5 — relevance is null
OUT_TOKENS_PER_ARTICLE_HIGH = 170   # score 3/4 — extra relevance sentence

SPEND_CEILING_USD = 8.00

INPUT_COLUMNS = ["date", "alert term", "headline", "url", "message id"]
OUTPUT_COLUMNS = INPUT_COLUMNS + [
    "score", "category", "score_reason", "summary", "relevance",
    "tags", "verticals", "mentioned_companies", "fetch_status",
]


# --- shared helpers --------------------------------------------------------

def row_key(row: dict) -> str:
    """Checkpoint key: message id + url (message ids repeat across alerts)."""
    return f"{row['message id']}|{row['url']}"


def load_alerts() -> list[dict]:
    with open(ALERTS_CSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for c in INPUT_COLUMNS:
            r[c] = (r.get(c) or "").strip()
    return rows


def _to_iso(alert_date: str) -> str:
    """'2026-09-03 11:42' -> ISO-8601 UTC, matching the pubDate production sends."""
    try:
        dt = datetime.strptime(alert_date, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return alert_date


def build_articles(rows: list[dict], snippets: dict) -> list[dict]:
    """Article dicts using exactly the keys score_articles reads."""
    articles = []
    for r in rows:
        rec = snippets.get(row_key(r), {})
        articles.append({
            "title":        r["headline"],
            "source":       extract_publisher(r["url"]),
            "pubDate":      _to_iso(r["date"]),
            "snippet":      rec.get("snippet", ""),
            # audit-only passthrough, ignored by score_articles
            "_row":          r,
            "_fetch_status": rec.get("fetch_status", "failed"),
        })
    return articles


def batches_of(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


# ===========================================================================
# Phase 1 — fetch real snippets
# ===========================================================================

_snip_lock = threading.Lock()
_BLOCKED_CODES = {401, 402, 403, 405, 406, 429, 451}

# Some sites soft-block with HTTP 200 and a JS device-check interstitial rather
# than a 403 — thesun.ie (toadmash) does this for every article. Those are
# blocked, not genuinely description-less, and no header/cookie/AMP variation
# gets past them without a JS runtime.
_INTERSTITIAL_MARKERS = (
    b"toadmash",
    b"Verifying Device",
    b"Verifying your device",
    b"Just a moment...",
    b"Attention Required! | Cloudflare",
    b"Checking your browser before accessing",
    b"Please enable JS and disable any ad blocker",
)


def _is_interstitial(body: bytes) -> bool:
    head = body[:4096]
    return any(m in head for m in _INTERSTITIAL_MARKERS)


def _extract_snippet(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, "html.parser")

    def _meta(attr, val):
        tag = soup.find("meta", attrs={attr: val})
        return (tag.get("content") or "") if tag else ""

    text = (
        _meta("name", "description")
        or _meta("property", "og:description")
        or _meta("name", "og:description")
        or _meta("property", "description")
    )
    if not text.strip():
        for p in soup.find_all("p"):
            candidate = p.get_text(" ", strip=True)
            if len(candidate) >= 60:
                text = candidate
                break
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SNIPPET_CAP]


_local = threading.local()


def _sessions():
    """Reused per-thread sessions.

    Building a scraper per request means a fresh TLS handshake per URL, which
    the local Norton MITM proxy throttles into mass connection failures — the
    same domains then show up as both ok and failed. Keep-alive fixes it.
    """
    if not hasattr(_local, "sessions"):
        sess = []
        if cloudscraper is not None:
            try:
                sess.append(cloudscraper.create_scraper())
            except Exception:
                pass
        sess.append(requests.Session())
        _local.sessions = sess
    return _local.sessions


def _fetch_one(url: str) -> dict:
    """Return {'snippet': str, 'fetch_status': ok|no_snippet|blocked|failed}."""
    last_status = None
    getters = [
        (lambda u, s=s: s.get(u, headers=_OG_HEADERS, timeout=FETCH_TIMEOUT))
        for s in _sessions()
    ]

    for get in getters:
        for attempt in range(FETCH_RETRIES + 1):
            try:
                resp = get(url)
            except (requests.Timeout, requests.ConnectionError):
                if attempt < FETCH_RETRIES:
                    time.sleep(1.0 + attempt)   # back off before the retry
                    continue                    # timeout / connection only
                break                 # fall through to the next getter
            except Exception:
                break                 # non-retryable — next getter
            last_status = resp.status_code
            if resp.status_code == 200:
                if _is_interstitial(resp.content):
                    return {"snippet": "", "fetch_status": "blocked"}
                try:
                    snippet = _extract_snippet(resp.content)
                except Exception:
                    snippet = ""
                return {
                    "snippet": snippet,
                    "fetch_status": "ok" if snippet else "no_snippet",
                }
            break                     # non-200 — try the next getter

    if last_status in _BLOCKED_CODES:
        return {"snippet": "", "fetch_status": "blocked"}
    return {"snippet": "", "fetch_status": "failed"}


def _save_snippets(snippets: dict) -> None:
    """Rewrite the consolidated JSON checkpoint.

    Norton's on-access scanner intermittently holds a lock on freshly written
    files, so os.replace can raise WinError 5 — retry, then fall back to an
    in-place write (the per-fetch JSONL log is the durable record either way).
    """
    tmp = SNIPPETS_FILE.with_suffix(".json.tmp")
    payload = json.dumps(snippets, ensure_ascii=False, indent=1)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    for attempt in range(5):
        try:
            os.replace(tmp, SNIPPETS_FILE)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    with open(SNIPPETS_FILE, "w", encoding="utf-8") as f:
        f.write(payload)
    try:
        tmp.unlink()
    except OSError:
        pass


def _log_snippet(key: str, rec: dict) -> None:
    """Durable per-fetch checkpoint — one append-only line, no rename."""
    with open(SNIPPETS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, **rec}, ensure_ascii=False) + "\n")
        f.flush()


def load_snippets() -> dict:
    """Consolidated JSON checkpoint, with the append-only log replayed over it."""
    snippets: dict = {}
    if SNIPPETS_FILE.exists():
        try:
            snippets = json.loads(SNIPPETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            snippets = {}
    if SNIPPETS_LOG.exists():
        with open(SNIPPETS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # torn final line from a hard kill
                snippets[rec.pop("key")] = rec
    return snippets


def phase_fetch() -> dict:
    rows = load_alerts()
    snippets = load_snippets()

    todo = [r for r in rows if row_key(r) not in snippets]
    print(f"Phase 1 — {len(rows)} alerts, {len(snippets)} already checkpointed, "
          f"{len(todo)} to fetch.")
    if not todo:
        return snippets

    # The same URL can appear under several message ids — fetch it once.
    url_to_keys: dict[str, list[str]] = {}
    for r in todo:
        url_to_keys.setdefault(r["url"], []).append(row_key(r))

    done = 0
    total = len(url_to_keys)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, u): u for u in url_to_keys}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                rec = fut.result()
            except Exception as exc:
                log.warning("fetch crashed for %s — %s", url[:80], exc)
                rec = {"snippet": "", "fetch_status": "failed"}
            rec["url"] = url
            with _snip_lock:
                for k in url_to_keys[url]:
                    snippets[k] = dict(rec)
                    _log_snippet(k, rec)          # checkpoint every fetch
                done += 1
                if done % 25 == 0 or done == total:
                    _save_snippets(snippets)
                    print(f"  fetched {done}/{total} urls", flush=True)

    _save_snippets(snippets)

    counts: dict[str, int] = {}
    for rec in snippets.values():
        st = rec.get("fetch_status", "?")
        counts[st] = counts.get(st, 0) + 1
    print("Phase 1 fetch_status:", dict(sorted(counts.items())))
    return snippets


# ===========================================================================
# Phase 2 — build the REAL prompts and count their tokens (free), then gate
# ===========================================================================

class _PromptRecorder:
    """Stands in for anthropic.Anthropic inside score_articles so we can capture
    the exact prompt production would send, without making a billed call."""

    captured: list[str] = []

    def __init__(self, *a, **kw):
        self.messages = self
        self.beta = self

    def create(self, **kwargs):
        _PromptRecorder.captured.append(kwargs["messages"][0]["content"])

        class _Stub:
            content = [type("T", (), {"text": "[]"})()]
            usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()

        return _Stub()


def capture_real_prompts(articles: list[dict]) -> list[str]:
    """Run score_articles with a recording client — byte-identical prompts,
    zero API calls."""
    import anthropic as _anthropic
    import daily_monitor

    _PromptRecorder.captured = []
    real_cls = _anthropic.Anthropic
    daily_monitor.anthropic.Anthropic = _PromptRecorder
    try:
        daily_monitor.score_articles(articles)
    finally:
        daily_monitor.anthropic.Anthropic = real_cls
    return list(_PromptRecorder.captured)


def phase_estimate(articles: list[dict]) -> dict:
    import anthropic
    from daily_monitor import BATCH_SIZE, MODEL

    prompts = capture_real_prompts(articles)
    n_batches = len(prompts)
    expected = -(-len(articles) // BATCH_SIZE)
    print(f"\nPhase 2 — built {n_batches} real batch prompts "
          f"(BATCH_SIZE={BATCH_SIZE}, expected {expected}).")

    fingerprint = hashlib.sha256("\x00".join(prompts).encode("utf-8")).hexdigest()
    cached = {}
    if TOKENS_FILE.exists():
        try:
            cached = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception:
            cached = {}

    if cached.get("fingerprint") == fingerprint:
        per_batch = cached["per_batch_input_tokens"]
        print("  reusing cached token counts (prompts unchanged).")
    else:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        per_batch = []
        for i, prompt in enumerate(prompts, 1):
            res = client.messages.count_tokens(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            per_batch.append(res.input_tokens)
            if i % 10 == 0 or i == n_batches:
                print(f"  counted {i}/{n_batches} batches "
                      f"({sum(per_batch):,} input tokens so far)", flush=True)
        TOKENS_FILE.write_text(json.dumps({
            "fingerprint": fingerprint,
            "model": MODEL,
            "per_batch_input_tokens": per_batch,
        }, indent=1), encoding="utf-8")

    total_in = sum(per_batch)
    n_articles = len(articles)
    max_tokens = 4000

    cost_in = total_in / 1_000_000 * PRICE_IN_PER_MTOK
    ceiling_out = n_batches * max_tokens / 1_000_000 * PRICE_OUT_PER_MTOK
    exp_out_low = n_articles * OUT_TOKENS_PER_ARTICLE_LOW / 1_000_000 * PRICE_OUT_PER_MTOK
    exp_out_high = n_articles * OUT_TOKENS_PER_ARTICLE_HIGH / 1_000_000 * PRICE_OUT_PER_MTOK

    print(f"""
======================= COST VERIFICATION (Phase 2) =======================
Model                        : {MODEL}
Articles / batches           : {n_articles} / {n_batches}  (BATCH_SIZE={BATCH_SIZE})
Real input tokens (counted)  : {total_in:,}   <- Anthropic count_tokens, free, exact
  min / mean / max per batch : {min(per_batch):,} / {total_in // n_batches:,} / {max(per_batch):,}

EXACT input cost             : ${cost_in:.4f}   ({total_in:,} tok @ ${PRICE_IN_PER_MTOK}/MTok)

Output cost, ABSOLUTE CEILING: ${ceiling_out:.4f}
  = {n_batches} batches x max_tokens {max_tokens} @ ${PRICE_OUT_PER_MTOK}/MTok
  (hard maximum — the API cannot bill more output than max_tokens per call)
Output cost, expected range  : ${exp_out_low:.4f} - ${exp_out_high:.4f}
  = {n_articles} articles x {OUT_TOKENS_PER_ARTICLE_LOW}-{OUT_TOKENS_PER_ARTICLE_HIGH} tok

TOTAL, expected range        : ${cost_in + exp_out_low:.4f} - ${cost_in + exp_out_high:.4f}
TOTAL, absolute worst case   : ${cost_in + ceiling_out:.4f}
Phase 3 hard-stop ceiling    : ${SPEND_CEILING_USD:.2f}

Pricing: ${PRICE_IN_PER_MTOK}/MTok in, ${PRICE_OUT_PER_MTOK}/MTok out — standard
(non-batch) rate per claude.com/pricing, verified {PRICING_VERIFIED_ON}.
Note: {MODEL} is listed as legacy, tentative retirement not before
{MODEL_RETIREMENT_NOT_BEFORE} — after that date the hardcoded MODEL in
daily_monitor.py may need changing too.
===========================================================================""")

    return {
        "prompts": prompts,
        "total_input_tokens": total_in,
        "cost_in": cost_in,
        "ceiling_out": ceiling_out,
        "exp_out_low": exp_out_low,
        "exp_out_high": exp_out_high,
    }


def cost_gate() -> bool:
    print("\nType exactly RUN to proceed to Phase 3 (anything else aborts): ",
          end="", flush=True)
    try:
        answer = sys.stdin.readline()
    except Exception:
        answer = ""
    if answer.strip() != "RUN":
        print(f"\nGot {answer.strip()!r} — not 'RUN'. Aborting before any billed call.")
        return False
    print("Confirmed. Proceeding to Phase 3.\n")
    return True


# ===========================================================================
# Phase 3 — score with a real circuit breaker
# ===========================================================================

class SpendCeilingHit(Exception):
    pass


class _MeteredClient:
    """Delegates to the real Anthropic client, but meters every response's real
    billed token usage so the run can be stopped at the spend ceiling."""

    total_in = 0
    total_out = 0
    spend = 0.0
    _real = None

    def __init__(self, *a, **kw):
        self.messages = self
        self.beta = self

    def create(self, **kwargs):
        resp = _MeteredClient._real.messages.create(**kwargs)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            _MeteredClient.total_in += usage.input_tokens
            _MeteredClient.total_out += usage.output_tokens
            _MeteredClient.spend = (
                _MeteredClient.total_in / 1_000_000 * PRICE_IN_PER_MTOK
                + _MeteredClient.total_out / 1_000_000 * PRICE_OUT_PER_MTOK
            )
        return resp


def _scored_row(article: dict) -> dict:
    r = article["_row"]
    out = {c: r.get(c, "") for c in INPUT_COLUMNS}
    out.update({
        "score":               article.get("score", ""),
        "category":            article.get("category", ""),
        "score_reason":        article.get("reason", ""),
        "summary":             article.get("summary", ""),
        "relevance":           article.get("relevance") or "",
        "tags":                json.dumps(article.get("tags", []), ensure_ascii=False),
        "verticals":           json.dumps(article.get("verticals", []), ensure_ascii=False),
        "mentioned_companies": json.dumps(article.get("mentioned_companies", []),
                                          ensure_ascii=False),
        "fetch_status":        article.get("_fetch_status", ""),
    })
    return out


def phase_score(articles: list[dict]) -> None:
    import anthropic
    import daily_monitor
    from daily_monitor import BATCH_SIZE

    done_keys: set[str] = set()
    if SCORED_CSV.exists():
        with open(SCORED_CSV, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                done_keys.add(f"{r['message id']}|{r['url']}")
        print(f"Resuming — {len(done_keys)} rows already in {SCORED_CSV.name}.")
    else:
        with open(SCORED_CSV, "w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS).writeheader()

    all_batches = batches_of(articles, BATCH_SIZE)
    n = len(all_batches)

    _MeteredClient._real = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    real_cls = daily_monitor.anthropic.Anthropic
    daily_monitor.anthropic.Anthropic = _MeteredClient

    stopped = False
    scored_total = 0
    try:
        for i, batch in enumerate(all_batches, 1):
            if all(row_key(a["_row"]) in done_keys for a in batch):
                print(f"Batch {i}/{n} — already scored, skipping.")
                continue

            scored = daily_monitor.score_articles(batch)
            if len(scored) != len(batch):
                log.warning("Batch %d returned %d/%d scored articles.",
                            i, len(scored), len(batch))

            with open(SCORED_CSV, "a", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
                for a in scored:
                    if row_key(a["_row"]) in done_keys:
                        continue
                    w.writerow(_scored_row(a))
                    done_keys.add(row_key(a["_row"]))
                    scored_total += 1

            print(f"Batch {i}/{n} — actual spend so far: "
                  f"${_MeteredClient.spend:.2f} "
                  f"({_MeteredClient.total_in:,} in / {_MeteredClient.total_out:,} out tok)",
                  flush=True)

            if _MeteredClient.spend > SPEND_CEILING_USD:
                stopped = True
                break
    finally:
        daily_monitor.anthropic.Anthropic = real_cls
        # keep any parse-failure debug files out of the repo root
        for p in Path.cwd().glob("claude_debug_daily_batch_*.txt"):
            try:
                p.replace(DATA_DIR / p.name)
            except Exception:
                pass

    print(f"\nPhase 3 done — {scored_total} newly scored this run, "
          f"{len(done_keys)} total rows in {SCORED_CSV.name}.")
    print(f"FINAL ACTUAL SPEND: ${_MeteredClient.spend:.4f} "
          f"({_MeteredClient.total_in:,} input / {_MeteredClient.total_out:,} output tokens)")

    if stopped:
        print(f"\n*** HARD STOP — running spend ${_MeteredClient.spend:.2f} exceeded the "
              f"${SPEND_CEILING_USD:.2f} ceiling. ***")
        print(f"{len(done_keys)}/{len(articles)} alerts scored and saved. Raise "
              f"SPEND_CEILING_USD and rerun --phase score to continue where this stopped.")
        raise SpendCeilingHit()


# ===========================================================================
# Phase 4 — compare against the hub
# ===========================================================================

def normalize_url(url: str) -> str:
    """lowercase host, strip www., drop query + fragment, drop trailing slash."""
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return (url or "").strip().lower()
    host = (p.hostname or "").lower().removeprefix("www.")
    path = (p.path or "").rstrip("/")
    return f"{host}{path}" if host else (url or "").strip().lower()


def url_domain(url: str) -> str:
    try:
        return (urlparse((url or "").strip()).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").lower().strip())


def fetch_news_items() -> list[dict]:
    client = _get_client()
    if client is None:
        raise RuntimeError(
            "Supabase client unavailable — NEXT_PUBLIC_SUPABASE_URL / "
            "SUPABASE_SERVICE_ROLE_KEY not set."
        )
    rows: list[dict] = []
    page, size = 0, 1000
    while True:
        res = (
            client.table("news_items")
            .select("id,url,title,score,published_at,status")
            .range(page * size, page * size + size - 1)
            .execute()
        )
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return rows


def phase_compare() -> None:
    if not SCORED_CSV.exists():
        raise SystemExit(f"{SCORED_CSV} not found — run --phase score first.")

    with open(SCORED_CSV, encoding="utf-8", newline="") as f:
        scored = list(csv.DictReader(f))

    hub = fetch_news_items()
    print(f"\nPhase 4 — {len(scored)} scored alerts vs {len(hub)} news_items rows "
          f"(all statuses).")

    by_url: dict[str, dict] = {}
    by_domain: dict[str, list[dict]] = {}
    for h in hub:
        n = normalize_url(h.get("url", ""))
        if n:
            by_url.setdefault(n, h)
        by_domain.setdefault(url_domain(h.get("url", "")), []).append(h)

    counts = {str(s): 0 for s in range(1, 6)}
    matched_exact = matched_fuzzy = 0
    high: list[dict] = []
    missing: list[dict] = []

    for row in scored:
        s = str(row.get("score", "")).strip()
        if s in counts:
            counts[s] += 1
        if s not in ("4", "5"):
            continue
        high.append(row)

        hit = by_url.get(normalize_url(row["url"]))
        how = "exact"
        if hit is None:
            how = "fuzzy"
            target = _norm_title(row["headline"])
            best, best_ratio = None, 0.0
            for h in by_domain.get(url_domain(row["url"]), []):
                ratio = difflib.SequenceMatcher(
                    None, target, _norm_title(h.get("title", ""))
                ).ratio()
                if ratio > best_ratio:
                    best, best_ratio = h, ratio
            if best is not None and best_ratio >= 0.82:
                hit = best

        if hit is None:
            missing.append(row)
        elif how == "exact":
            matched_exact += 1
        else:
            matched_fuzzy += 1

    with open(MISSING_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        w.writeheader()
        for row in missing:
            w.writerow({c: row.get(c, "") for c in OUTPUT_COLUMNS})

    uniq_missing = len({normalize_url(r["url"]) for r in missing})
    print(f"""
============================ PHASE 4 SUMMARY ============================
Total alerts scored     : {len(scored)}
  score 1               : {counts['1']}
  score 2               : {counts['2']}
  score 3               : {counts['3']}
  score 4               : {counts['4']}
  score 5               : {counts['5']}
Score 4 or 5            : {len(high)}
  matched (exact url)   : {matched_exact}
  matched (fuzzy title) : {matched_fuzzy}
  MISSING from hub      : {len(missing)}  ({uniq_missing} unique urls)

Written: {MISSING_CSV}
=========================================================================""")


# ===========================================================================

def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all",
                    choices=["fetch", "estimate", "score", "compare", "all"])
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.phase == "compare":
        phase_compare()
        return

    if args.phase == "fetch":
        phase_fetch()
        return

    snippets: dict = {}
    if args.phase == "all":
        snippets = phase_fetch()
    else:
        snippets = load_snippets()

    rows = load_alerts()
    missing_snips = [r for r in rows if row_key(r) not in snippets]
    if missing_snips:
        raise SystemExit(
            f"{len(missing_snips)} alerts have no snippet checkpoint — "
            f"run --phase fetch first."
        )

    articles = build_articles(rows, snippets)
    phase_estimate(articles)

    if args.phase == "estimate":
        print("\n(--phase estimate: stopping here. No billed call made.)")
        return

    if not cost_gate():
        return

    try:
        phase_score(articles)
    except SpendCeilingHit:
        return
    phase_compare()


if __name__ == "__main__":
    main()
