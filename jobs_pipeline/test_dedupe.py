"""test_dedupe.py — unit tests for the identical-listing dedupe rule.

Covers adapters/base.py's dedupe_identical_listings(): conservative match on
(normalised title, normalised location) applied to a source's fetched jobs
before upsert. Motivating case: VALD (breezy) posting an identical
"Business Development Manager" listing once per country — but since each
posting has a genuinely different location, the conservative rule does NOT
collapse those (by design; see module docstring in base.py).

No pytest dependency — run directly:
    python jobs_pipeline/test_dedupe.py
Exits non-zero if any assertion fails (CI-friendly).
"""

import os
import sys

# Support running as: python jobs_pipeline/test_dedupe.py from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jobs_pipeline.adapters.base import dedupe_identical_listings  # noqa: E402

_passed = 0
_failed = 0


def check(label: str, actual, expected) -> None:
    global _passed, _failed
    ok = actual == expected
    status = "PASS" if ok else "FAIL"
    print(f"{status}  {label}: got={actual!r} expected={expected!r}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def _job(title: str, location: str | None, tag: str) -> dict:
    # 'tag' lets assertions identify which instance survived (e.g. earliest-seen).
    return {"title": title, "location_raw": location, "tag": tag}


# ── Exact duplicate: same title, same location → collapse ────────────────────

result = dedupe_identical_listings([
    _job("Business Development Manager", "Dublin, Ireland", "first"),
    _job("Business Development Manager", "Dublin, Ireland", "second"),
])
check("exact duplicate (same title+location) collapses to 1", len(result), 1)
check("exact duplicate keeps earliest-seen instance", result[0]["tag"], "first")

# ── Both locations null → collapse (location null counts as identical) ───────

result = dedupe_identical_listings([
    _job("Software Engineer", None, "first"),
    _job("Software Engineer", None, "second"),
])
check("both-null-location duplicate collapses to 1", len(result), 1)

# ── VALD case: same title, DIFFERENT locations → both kept ───────────────────

result = dedupe_identical_listings([
    _job("Business Development Manager", "Marseille, France", "france"),
    _job("Business Development Manager", "Warsaw, Poland", "poland"),
    _job("Business Development Manager", "Astana, Kazakhstan", "kazakhstan"),
])
check("same title, different locations — all kept (VALD case)", len(result), 3)

# ── Different titles, same location → both kept ──────────────────────────────

result = dedupe_identical_listings([
    _job("Software Engineer", "Dublin, Ireland", "eng"),
    _job("Product Manager", "Dublin, Ireland", "pm"),
])
check("different titles, same location — both kept", len(result), 2)

# ── One location null, one populated → NOT a match (conservative) ────────────

result = dedupe_identical_listings([
    _job("Business Development Manager", None, "unknown_loc"),
    _job("Business Development Manager", "Dublin, Ireland", "known_loc"),
])
check("null location vs populated location — not merged", len(result), 2)

# ── Case/whitespace normalisation on both title and location ─────────────────

result = dedupe_identical_listings([
    _job("Business Development Manager", "Dublin, Ireland", "first"),
    _job("  business development manager  ", "dublin,   ireland", "second"),
])
check("case/whitespace-insensitive title+location match collapses to 1", len(result), 1)
check("keeps earliest-seen on normalised match too", result[0]["tag"], "first")

# ── Three-way: two exact duplicates + one distinct kept ──────────────────────

result = dedupe_identical_listings([
    _job("Customer Success Manager", "Cork, Ireland", "a"),
    _job("Customer Success Manager", "Cork, Ireland", "b"),
    _job("Customer Success Manager", "Galway, Ireland", "c"),
])
check("2 exact dupes + 1 distinct -> 2 survive", len(result), 2)
check("survivors are earliest-seen dupe + the distinct one",
      [j["tag"] for j in result], ["a", "c"])


print()
if _failed == 0:
    print(f"All {_passed} assertions passed.")
else:
    print(f"{_failed} FAILED, {_passed} passed.")
    raise SystemExit(1)
