"""
test_daily_monitor.py
Offline tests for the 2026-09-26 daily-monitor fixes: normalise_url(), the URL
blocklist, seen-list backward compatibility, Resend 429 retry/throttle, and the
send loop surviving a failed email. No network, no API keys, no billed calls.

    python test_daily_monitor.py
"""

import json
import os
import tempfile
from unittest import mock

import requests

import daily_monitor as dm
import email_client as ec


def _resp(status, headers=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = b'{"id":"x"}' if status < 400 else b'{"name":"rate_limit_exceeded"}'
    r.url = ec.RESEND_ENDPOINT
    return r


# ---------------------------------------------------------------------------
# normalise_url + blocklist
# ---------------------------------------------------------------------------

def test_normalise_url():
    cases = {
        # locale variants of one match collapse to the same key
        "https://www.sheepesports.com/us/rl/matches/1803ed2064a990b7":
            "https://www.sheepesports.com/matches/1803ed2064a990b7",
        "https://www.sheepesports.com/kr/rl/matches/1803ed2064a990b7":
            "https://www.sheepesports.com/matches/1803ed2064a990b7",
        "https://WWW.SheepEsports.com/en/rl/matches/1803ed2064a990b7/":
            "https://www.sheepesports.com/matches/1803ed2064a990b7",
        # only the LEADING run of 2-letter segments is stripped (idempotency)
        "https://www.sheepesports.com/en/cs/articles/x/en":
            "https://www.sheepesports.com/articles/x/en",
        "https://www.bbc.com/sport/football/articles/abc":
            "https://www.bbc.com/sport/football/articles/abc",
        "https://example.ie/eng/story": "https://example.ie/eng/story",
        # trailing slash, tracking params and fragment dropped; real params kept
        "https://irishtimes.com/a/b/?utm_source=x&utm_medium=y&id=7&fbclid=z#top":
            "https://irishtimes.com/a/b?id=7",
        "https://example.ie/": "https://example.ie",
        "https://example.ie/en": "https://example.ie",
        # Google search fallback keeps its q param
        "https://www.google.com/search?q=Hello%20World":
            "https://www.google.com/search?q=Hello+World",
        "": "",
    }
    for raw, want in cases.items():
        got = dm.normalise_url(raw)
        assert got == want, f"normalise_url({raw!r}) = {got!r}, want {want!r}"
        assert dm.normalise_url(got) == got, f"not idempotent for {raw!r}"


def test_blocklist():
    blocked = [
        "https://www.sheepesports.com/us/rl/matches/1803ed2064a990b7",
        "https://sheepesports.com/rl/matches/abc",
        "https://www.sheepesports.com/en/lol/matches/abc?utm_source=g",
    ]
    allowed = [
        "https://www.sheepesports.com/en/cs/articles/nlc-will-be-split",
        "https://www.bbc.com/sport/football/matches/abc",
        "https://www.irishtimes.com/sport/2026/09/26/story/",
    ]
    for u in blocked:
        assert dm.is_blocked_url(u), f"should be blocked: {u}"
    for u in allowed:
        assert not dm.is_blocked_url(u), f"should not be blocked: {u}"


def test_load_seen_normalises_legacy_entries():
    legacy = {"seen_urls": [
        "https://www.sheepesports.com/en/rl/matches/1803ed2064a990b7",
        "https://www.irishtimes.com/sport/story/",
    ]}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "seen.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        with mock.patch.object(dm, "SEEN_FILE", path):
            seen = dm.load_seen()
    # the /us/ variant fetched on 2026-09-26 now matches the /en/ entry
    assert dm.normalise_url("https://www.sheepesports.com/us/rl/matches/1803ed2064a990b7") in seen
    assert dm.normalise_url("https://www.irishtimes.com/sport/story") in seen


def test_real_seen_file_loads():
    seen = dm.load_seen()
    assert len(seen) > 250, len(seen)
    assert all(dm.normalise_url(u) == u for u in seen)


# ---------------------------------------------------------------------------
# email_client: throttle + 429 retry
# ---------------------------------------------------------------------------

_ENV = {"RESEND_API_KEY": "re_test", "ALERT_FROM": "a@x.ie", "ALERT_TO": "b@x.ie"}


def test_429_then_success_uses_retry_after():
    responses = [_resp(429, {"retry-after": "2"}), _resp(200)]
    with mock.patch.dict(os.environ, _ENV), \
         mock.patch.object(ec.requests, "post", side_effect=responses) as post, \
         mock.patch.object(ec.time, "sleep") as sleep:
        assert ec.send_email("s", "<p>b</p>") == 200
    assert post.call_count == 2
    assert 2.0 in [c.args[0] for c in sleep.call_args_list], sleep.call_args_list


def test_429_exponential_backoff_without_headers():
    responses = [_resp(429), _resp(429), _resp(200)]
    with mock.patch.dict(os.environ, _ENV), \
         mock.patch.object(ec.requests, "post", side_effect=responses), \
         mock.patch.object(ec.time, "sleep") as sleep:
        assert ec.send_email("s", "<p>b</p>") == 200
    backoffs = [c.args[0] for c in sleep.call_args_list if c.args[0] >= 1.0]
    assert backoffs == [1.0, 2.0], backoffs


def test_429_ratelimit_reset_header():
    responses = [_resp(429, {"ratelimit-reset": "1"}), _resp(200)]
    with mock.patch.dict(os.environ, _ENV), \
         mock.patch.object(ec.requests, "post", side_effect=responses), \
         mock.patch.object(ec.time, "sleep") as sleep:
        ec.send_email("s", "<p>b</p>")
    assert 1.0 in [c.args[0] for c in sleep.call_args_list]


def test_429_raises_after_retries_exhausted():
    responses = [_resp(429)] * (ec.MAX_429_RETRIES + 1)
    with mock.patch.dict(os.environ, _ENV), \
         mock.patch.object(ec.requests, "post", side_effect=responses) as post, \
         mock.patch.object(ec.time, "sleep"):
        try:
            ec.send_email("s", "<p>b</p>")
        except requests.HTTPError as exc:
            assert exc.response.status_code == 429
        else:
            raise AssertionError("expected HTTPError after retries")
    assert post.call_count == ec.MAX_429_RETRIES + 1


def test_non_429_error_not_retried():
    with mock.patch.dict(os.environ, _ENV), \
         mock.patch.object(ec.requests, "post", side_effect=[_resp(422)]) as post, \
         mock.patch.object(ec.time, "sleep"):
        try:
            ec.send_email("s", "<p>b</p>")
        except requests.HTTPError:
            pass
        else:
            raise AssertionError("expected HTTPError")
    assert post.call_count == 1


def test_throttle_spaces_consecutive_sends():
    with mock.patch.dict(os.environ, _ENV), \
         mock.patch.object(ec.requests, "post", side_effect=[_resp(200)] * 3), \
         mock.patch.object(ec.time, "sleep") as sleep:
        for _ in range(3):
            ec.send_email("s", "<p>b</p>")
    # sends 2 and 3 land well inside 150ms of the previous one, so both must wait
    throttle_waits = [c.args[0] for c in sleep.call_args_list]
    assert len(throttle_waits) >= 2 and all(0 < w <= ec.MIN_SEND_INTERVAL_S for w in throttle_waits)


# ---------------------------------------------------------------------------
# run(): a failed email must not kill the run or lose the seen list
# ---------------------------------------------------------------------------

def test_run_survives_failed_email_and_saves_seen():
    arts = [
        {"title": f"Story {i}", "link": f"https://news.ie/en/story-{i}/", "score": 4,
         "source": "x", "pubDate": "2026-09-26T08:00:00+00:00"}
        for i in range(4)
    ]
    blocked_art = {"title": "Match", "link": "https://www.sheepesports.com/us/rl/matches/abc",
                   "source": "x", "pubDate": ""}
    calls = {"n": 0}

    def fake_send(subject, html, cc=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise requests.HTTPError("429 after retries")

    scored_inputs = []

    def fake_score(articles):
        scored_inputs.extend(articles)
        return [dict(a) for a in arts]

    with tempfile.TemporaryDirectory() as d:
        seen_path = os.path.join(d, "seen.json")
        cwd = os.getcwd()
        os.chdir(d)  # unsent file lands in the temp dir
        try:
            with mock.patch.object(dm, "SEEN_FILE", seen_path), \
                 mock.patch.object(dm, "fetch_recent_articles", return_value=(arts + [blocked_art], 5)), \
                 mock.patch.object(dm, "score_articles", side_effect=fake_score), \
                 mock.patch.object(dm, "deduplicate_by_story", side_effect=lambda a: a), \
                 mock.patch.object(dm, "build_news_item", side_effect=lambda a: a), \
                 mock.patch.object(dm, "upsert_news_item", return_value={"ok": True}), \
                 mock.patch.object(dm, "_send_email", side_effect=fake_send):
                assert dm.run() is True
            with open(seen_path, encoding="utf-8") as f:
                saved = set(json.load(f)["seen_urls"])
            unsent_files = [p for p in os.listdir(d) if p.startswith("daily_alerts_unsent_")]
        finally:
            os.chdir(cwd)

    assert calls["n"] == 4, "loop stopped after the failed email"
    assert blocked_art not in scored_inputs, "blocklisted article reached scoring"
    assert saved == {"https://news.ie/story-0", "https://news.ie/story-2", "https://news.ie/story-3"}, saved
    assert len(unsent_files) == 1


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
