"""resolve_lsp_linkedin.py — one-off setup script: resolve the 29 Local Sports
Partnerships to their LinkedIn company pages.

Run by hand, never by a workflow. Writes a PROPOSAL CSV
(events_pipeline/data/lsp_linkedin_resolved.csv) for human review. It does not
write the file the adapter reads (data/linkedin_seed_authors.csv) — a resolved
row only becomes a live seed after someone opens the LinkedIn page and confirms
it is the right organisation.

Why the two-file split, and why the corroboration gate:
CLAUDE.md's standing rule from the `onezero` and `ea` incidents applies exactly
here. Guessing `linkedin.com/company/<slug-from-name>` proves a page exists, not
that it is *this* organisation's page — and an LSP page mis-attributed into the
seed list would quietly pull an unrelated org's posts into event_leads every
Friday. So this script never guesses slugs. It asks Serper (Google's index) which
LinkedIn company page ranks for the LSP's exact name, then corroborates the
returned page title against the name before marking a row `verified`.

Each LSP is searched under several name variants (see _search_variants) because
Irish LSPs are inconsistently named; the corroboration gate applied to the
results is identical for every variant. Widening the search is safe, widening
the gate would not be.

Corroboration is deliberately strict and fails closed:
  - the county/place token must appear in the LinkedIn page title, AND
  - a sport/partnership token must appear in the title.
Anything else lands as needs_manual_review with the candidate URL recorded, so a
human has something to click rather than nothing. Expect false negatives; that is
the intended direction of the error.

The 29 LSP names and websites below were read off Sport Ireland's own LSP Contact
Finder (sportireland.ie/participation/lsp-contact-finder, 3 pages, "Displaying
1-12 of 29") on 2026-09-14 — not assembled from memory.

Usage:
    python events_pipeline/resolve_lsp_linkedin.py
    python events_pipeline/resolve_lsp_linkedin.py --only meath
    python events_pipeline/resolve_lsp_linkedin.py --out some/other.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/search"
_DEFAULT_OUT = Path(__file__).resolve().parent / "data" / "lsp_linkedin_resolved.csv"

# Polite gap between Serper calls. 29 calls is nothing against the free tier's
# 2,500/month, but this script is also the thing someone re-runs when a page
# moves, so it should stay well-behaved by default.
_THROTTLE_SECONDS = 1.0

# (place token used for corroboration, official name, Sport Ireland website,
#  stem used to build looser search variants — see _search_variants)
# Read from the LSP Contact Finder on 2026-09-14.
#
# `place` and `stem` differ where the official name is not "<County> Sports
# Partnership": `place` is the single token that must appear in a LinkedIn page
# title for the corroboration gate to pass, while `stem` is the human name the
# organisation is likely to be *listed under*, which for Dún Laoghaire-Rathdown
# and the two Dublins is more than one word.
LSPS: list[tuple[str, str, str, str]] = [
    ("carlow",      "Carlow Sports Partnership",                 "www.carlowsports.ie",                 "Carlow"),
    ("cavan",       "Cavan Sports Partnership",                  "www.cavansportspartnership.ie",       "Cavan"),
    ("clare",       "Clare Sports Partnership",                  "www.claresports.ie",                  "Clare"),
    ("cork",        "Cork Sports Partnership",                   "www.corksports.ie",                   "Cork"),
    ("donegal",     "Donegal Sports Partnership",                "www.activedonegal.com",               "Donegal"),
    ("dublin",      "Dublin City Sport & Wellbeing Partnership", "www.dcswphub.ie",                     "Dublin City"),
    ("laoghaire",   "Dun Laoghaire Rathdown Sports Partnership", "www.dlrsportspartnership.ie",         "Dun Laoghaire Rathdown"),
    ("fingal",      "Fingal Sports Partnership",                 "www.fingal.ie",                       "Fingal"),
    ("galway",      "Galway Sports Active",                      "www.galwayactive.ie",                 "Galway"),
    ("kerry",       "Kerry Recreation and Sports Partnership",   "(Facebook only)",                     "Kerry"),
    ("kildare",     "Kildare Sports Partnership",                "kildarecoco.ie/kildaresp/",           "Kildare"),
    ("kilkenny",    "Kilkenny Recreation & Sports Partnership",  "www.krsp.ie",                         "Kilkenny"),
    ("laois",       "Laois Sports Partnership",                  "www.laoissports.ie",                  "Laois"),
    ("leitrim",     "Leitrim Sports Partnership",                "www.leitrimsports.ie",                "Leitrim"),
    ("limerick",    "Limerick Sports Partnership",               "www.limericksports.ie",               "Limerick"),
    ("longford",    "Longford Sports",                           "www.longfordsports.ie",               "Longford"),
    ("louth",       "Louth Sports Partnership",                  "www.louthlsp.com",                    "Louth"),
    ("mayo",        "Mayo Sports Partnership",                   "www.mayosports.ie",                   "Mayo"),
    ("meath",       "Meath Local Sports Partnership",            "www.meathsports.ie",                  "Meath"),
    ("monaghan",    "Monaghan Sports Partnership",               "www.monaghansports.ie",               "Monaghan"),
    ("offaly",      "Offaly Sports Partnership",                 "www.offalysports.ie",                 "Offaly"),
    ("roscommon",   "Roscommon Sports Partnership",              "www.rosactive.org",                   "Roscommon"),
    ("sligo",       "Sligo Sport & Recreation Partnership",      "www.sligosportandrecreation.ie",      "Sligo"),
    ("dublin",      "South Dublin County Sports Partnership",    "www.sdcsp.ie",                        "South Dublin"),
    ("tipperary",   "Tipperary Sports",                          "www.tipperarysports.ie",              "Tipperary"),
    ("waterford",   "Waterford Sports Partnership",              "www.waterfordsportspartnership.ie",   "Waterford"),
    ("westmeath",   "Westmeath Sports Partnership",              "www.westmeathsports.ie",              "Westmeath"),
    ("wexford",     "Sports Active Wexford",                     "www.sportsactivewexford.ie",          "Wexford"),
    ("wicklow",     "Wicklow Local Sports Partnership",          "www.wicklowlsp.ie",                   "Wicklow"),
]

# At least one of these must appear in the LinkedIn page title for a row to pass.
# "partnership" is deliberately NOT in this list even though it is in almost
# every LSP's name: Ireland is full of county-level LEADER, local development and
# family-resource "partnerships", so place + "partnership" alone corroborates
# nothing. It let Kilkenny LEADER Partnership through as Kilkenny Recreation &
# Sports Partnership on the first run of this script (2026-09-14) — the same
# wrong-organisation-same-slug-shape failure the module docstring is about.
_SPORT_TOKENS = ("sport", "recreation", "active", "wellbeing")

_CSV_FIELDS = [
    "lsp_name",
    "website",
    "linkedin_url",
    "result_title",
    "verdict",
    "notes",
]


class SerperError(Exception):
    """Serper call failed. Aborts the script — a partial CSV is worse than none."""


def _normalise(text: str) -> str:
    """Lowercase, fold the Irish-language accents that appear in LSP names,
    strip punctuation, collapse whitespace."""
    text = (text or "").lower()
    for accented, plain in (
        ("ú", "u"), ("í", "i"), ("á", "a"),
        ("é", "e"), ("ó", "o"),
    ):
        text = text.replace(accented, plain)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _company_url(link: str) -> str | None:
    """Reduce a LinkedIn URL to its canonical /company/<slug> form, or None."""
    try:
        parsed = urlparse(link)
    except ValueError:
        return None
    if "linkedin.com" not in (parsed.netloc or ""):
        return None
    m = re.match(r"^/company/([^/?#]+)", parsed.path or "")
    if not m:
        return None
    return f"https://www.linkedin.com/company/{m.group(1)}"


def _serper_search(query: str, api_key: str) -> list[dict]:
    """POST one query to Serper; return organic results. Raises SerperError."""
    try:
        resp = requests.post(
            _SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        raise SerperError(f"network error: {exc}") from exc

    if resp.status_code in (401, 403):
        raise SerperError(f"HTTP {resp.status_code} — check SERPER_API_KEY")
    if resp.status_code == 429:
        raise SerperError("HTTP 429 — Serper free tier quota exhausted")
    if resp.status_code != 200:
        raise SerperError(f"HTTP {resp.status_code}")

    try:
        return resp.json().get("organic", []) or []
    except ValueError as exc:
        raise SerperError(f"invalid JSON: {exc}") from exc


def _search_variants(name: str, stem: str) -> list[str]:
    """Search queries to try for one LSP, most specific first.

    The first pass of this script (2026-09-14) used the official name alone and
    left 9 of 29 as not_found. The gate was not the problem — it is what caught
    Kilkenny LEADER Partnership — the *query* was. Irish LSPs are inconsistently
    named: "Longford Sports" and "Tipperary Sports" drop "Partnership" entirely,
    "Sports Active Wexford" inverts the word order, and several are listed on
    LinkedIn as "<County> Local Sports Partnership" or "<County> Sports &
    Recreation Partnership" regardless of what Sport Ireland calls them (Wicklow
    resolved to wicklow-sports-recreation-partnership, Louth to
    louth-local-sports-partnership). So widen the net, not the gate: every
    candidate these variants surface still has to pass the same
    place-token + sport-token check before it can be marked verified.

    Deduped, order preserved — several LSPs' official name IS one of the
    variants, and re-querying it would just spend a Serper call to get the same
    answer twice.
    """
    variants = [
        f'site:linkedin.com/company "{name}"',
        f'site:linkedin.com/company "{stem} Local Sports Partnership"',
        f'site:linkedin.com/company "{stem} Sports Partnership"',
        f'site:linkedin.com/company "{stem} Sports & Recreation Partnership"',
        f'site:linkedin.com/company "{stem} Sport & Recreation Partnership"',
        # Unquoted last: loosest, most likely to surface an unrelated org, and
        # therefore most reliant on the gate to reject it.
        f"site:linkedin.com/company {stem} sports partnership Ireland",
    ]
    seen: set[str] = set()
    return [v for v in variants if not (v in seen or seen.add(v))]  # type: ignore[func-returns-value]


def resolve_one(
    place: str,
    name: str,
    stem: str,
    api_key: str,
    claimed_urls: dict[str, str] | None = None,
) -> dict:
    """Resolve one LSP to a LinkedIn company page, with a corroboration verdict.

    Tries each search variant in turn and stops at the first result that passes
    BOTH the corroboration gate and the already-claimed check. If none does,
    reports the best candidate seen across all variants so a human has something
    to click, marked needs_manual_review.

    `claimed_urls` maps a LinkedIn company URL to the organisation already using
    it — the curated seeds plus every LSP resolved earlier in this run. One
    LinkedIn page cannot be two different organisations, so a candidate that is
    already spoken for is refused rather than verified.

    This guard exists because the token gate alone is not sufficient, and the
    widened variant search made that visible. On 2026-09-14 "Galway Sports
    Active" resolved to atu-galway-department-of-sport-exercise-nutrition: the
    title contains "galway" and "sport", so the gate passed — but that is ATU
    Galway's sport department, a university, and it was already a curated seed
    under its own name. The gate can only tell you the tokens match, never that
    the organisation does; this catches the subset where we can prove it doesn't.
    """
    claimed_urls = claimed_urls or {}
    row = {
        "lsp_name": name,
        "website": "",
        "linkedin_url": "",
        "result_title": "",
        "verdict": "not_found",
        "notes": "",
    }

    place_norm = _normalise(place)
    first_candidate: tuple[str, str, str] | None = None
    variants = _search_variants(name, stem)

    for variant in variants:
        results = _serper_search(variant, api_key)

        for result in results:
            url = _company_url(result.get("link", ""))
            if not url:
                continue
            title = (result.get("title") or "").strip()
            title_norm = _normalise(title)

            if first_candidate is None:
                first_candidate = (url, title, variant)

            if place_norm in title_norm and any(t in title_norm for t in _SPORT_TOKENS):
                owner = claimed_urls.get(url)
                if owner and _normalise(owner) != _normalise(name):
                    row.update({
                        "linkedin_url": url,
                        "result_title": title,
                        "verdict": "needs_manual_review",
                        "notes": (
                            f"token gate passed but this page is already "
                            f"{owner!r} — one LinkedIn page is not two organisations"
                        ),
                    })
                    return row

                row.update({
                    "linkedin_url": url,
                    "result_title": title,
                    "verdict": "verified",
                    "notes": (
                        f"title contains '{place}' + a sport token; "
                        f"matched on variant {variants.index(variant) + 1}/{len(variants)}"
                    ),
                })
                return row

        if variant is not variants[-1]:
            time.sleep(_THROTTLE_SECONDS)

    if first_candidate is not None:
        url, title, variant = first_candidate
        row.update({
            "linkedin_url": url,
            "result_title": title,
            "verdict": "needs_manual_review",
            "notes": (
                f"no result across {len(variants)} query variants corroborated "
                f"(expected '{place}' + a sport token in the title); "
                f"best candidate from: {variant}"
            ),
        })
    else:
        row["notes"] = (
            f"no linkedin.com/company result from any of {len(variants)} query variants"
        )

    return row


_SEED_CSV = Path(__file__).resolve().parent / "data" / "linkedin_seed_authors.csv"


def _load_claimed_urls() -> dict[str, str]:
    """Map NON-LSP seed pages to the organisation they are filed under.

    Deliberately skips category=lsp rows. Those rows are this script's own prior
    output, so treating them as conflicts makes the resolver fight its results
    from last run: Sport Ireland calls it "Cork Sports Partnership" and LinkedIn
    calls it "Cork Local Sports Partnership", and comparing those two strings
    says "different organisation" when it plainly is not. LSP-versus-LSP
    collisions are caught anyway, because every LSP verified earlier in the
    current run is added to this map as it resolves.

    What is left is the case the guard is actually for: a page already known to
    belong to an organisation of a different kind — a university department, an
    agency, a conference — which no amount of county-plus-sport token matching
    can distinguish from the county's LSP.

    Read regardless of the row's `enabled` flag: a page parked as enabled=false
    is still that organisation's page.
    """
    if not _SEED_CSV.exists():
        return {}
    try:
        with open(_SEED_CSV, encoding="utf-8-sig", newline="") as f:
            lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        return {
            (row.get("linkedin_url") or "").strip(): (row.get("name") or "").strip()
            for row in csv.DictReader(lines)
            if (row.get("linkedin_url") or "").strip()
            and (row.get("category") or "").strip().lower() != "lsp"
        }
    except Exception as exc:
        log.warning("Could not read seed CSV %s: %s", _SEED_CSV, exc)
        return {}


def main(only: str | None, out_path: Path) -> int:
    api_key = os.getenv("SERPER_API_KEY", "")
    if not api_key:
        log.error("SERPER_API_KEY is not set — cannot resolve LSP LinkedIn pages.")
        return 1

    targets = LSPS
    if only:
        needle = only.lower()
        targets = [t for t in LSPS if needle in t[1].lower() or needle == t[0]]
        if not targets:
            log.error("--only %r matched none of the %d LSPs", only, len(LSPS))
            return 1

    # Pages already spoken for: the curated seeds the adapter actually reads,
    # plus each LSP resolved earlier in this run. See resolve_one's docstring.
    claimed_urls = _load_claimed_urls()
    if claimed_urls:
        log.info("Loaded %d already-claimed LinkedIn pages from the seed CSV", len(claimed_urls))

    rows: list[dict] = []
    for i, (place, name, website, stem) in enumerate(targets, start=1):
        log.info("[%d/%d] resolving %s", i, len(targets), name)
        try:
            row = resolve_one(place, name, stem, api_key, claimed_urls)
        except SerperError as exc:
            log.error("Serper failed on %r: %s — aborting", name, exc)
            return 1
        row["website"] = website
        if row["verdict"] == "verified" and row["linkedin_url"]:
            claimed_urls[row["linkedin_url"]] = name
        rows.append(row)
        log.info("  -> %s %s", row["verdict"], row["linkedin_url"] or "(no url)")
        if i < len(targets):
            time.sleep(_THROTTLE_SECONDS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1

    log.info("Wrote %d rows to %s", len(rows), out_path)
    for verdict in ("verified", "needs_manual_review", "not_found"):
        log.info("  %-20s %d", verdict, counts.get(verdict, 0))
    log.info(
        "Next: review the CSV by hand, then copy confirmed rows into "
        "events_pipeline/data/linkedin_seed_authors.csv with enabled=true."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Resolve Local Sports Partnerships to LinkedIn company pages (one-off setup).",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="NAME",
        help="Resolve a single LSP by substring of its name (e.g. 'meath').",
    )
    parser.add_argument(
        "--out",
        default=str(_DEFAULT_OUT),
        metavar="PATH",
        help=f"Output CSV path (default: {_DEFAULT_OUT}).",
    )
    args = parser.parse_args()
    sys.exit(main(only=args.only, out_path=Path(args.out)))
