"""Tests for openanalytics.

These exercise the real server: real SQLite file, real request/response
cycle, real endpoint shapes. No mocking of the storage layer.

    pytest -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

# Point the server at a throwaway database before importing it.
TEST_DIR = Path(__file__).resolve().parent
DB = TEST_DIR / "test_analytics.db"
os.environ["OPENANALYTICS_DB"] = str(DB)
os.environ["OPENANALYTICS_SALT"] = "test-salt"

import server  # noqa: E402
from sdk import OpenAnalytics  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    if DB.exists():
        DB.unlink()
    server._sync_init()
    yield
    for suffix in ("-journal", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()
    if DB.exists():
        DB.unlink()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(server.app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _pageview(client, url="https://example.com/", referrer=None):
    return client.post("/track", json={
        "url": url,
        "referrer": referrer,
        "event": "pageview",
        "timestamp": int(time.time() * 1000),
    })


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------


def test_track_single_pageview(client):
    r = _pageview(client, "https://example.com/pricing", "https://twitter.com")
    assert r.status_code == 200, r.text
    assert r.text == "ok"


def test_track_requires_url(client):
    r = client.post("/track", json={"event": "pageview", "timestamp": int(time.time() * 1000)})
    assert r.status_code == 400


def test_track_rejects_bad_json(client):
    r = client.post("/track", content=b"not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_track_accepts_json_without_content_type(client):
    """Regression: cross-origin sendBeacon strips Content-Type on non-safelisted
    values, so JSON hits arrive undeclared. They must still be accepted."""
    r = client.post("/track", content=b'{"url":"https://e.com/","event":"pageview"}')
    assert r.status_code == 200, r.text
    assert r.text == "ok"
    assert client.get("/api/health").json()["hits"] == 1


def test_track_accepts_json_as_text_plain(client):
    """Same as above but with a wrong-but-plausible declared type."""
    r = client.post("/track",
                    content=b'{"url":"https://e.com/","event":"pageview"}',
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200, r.text


def test_track_batch(client):
    body = [{
        "url": f"https://example.com/p{i}",
        "referrer": None,
        "event": "pageview",
        "timestamp": int(time.time() * 1000),
    } for i in range(3)]
    r = client.post("/track", json=body)
    assert r.status_code == 200, r.text
    assert r.text == "ok"


def test_track_custom_event_with_data(client):
    r = client.post("/track", json={
        "url": "https://example.com/checkout",
        "referrer": None,
        "event": "signup",
        "data": {"plan": "pro"},
        "timestamp": int(time.time() * 1000),
    })
    assert r.status_code == 200, r.text


def test_custom_event_counts_as_event_not_pageview(client):
    _pageview(client)
    client.post("/track", json={
        "url": "https://example.com/", "event": "signup",
        "data": {"plan": "pro"}, "timestamp": int(time.time() * 1000),
    })
    stats = client.get("/api/stats").json()
    assert stats["totals"]["pageviews"] == 1
    assert stats["totals"]["events"] == 1


# ---------------------------------------------------------------------------
# visitor pseudonymisation
# ---------------------------------------------------------------------------


def test_same_ip_and_ua_is_one_visitor(client):
    for _ in range(3):
        _pageview(client, "https://example.com/a", "https://x.com")
        _pageview(client, "https://example.com/b", "https://x.com")
    stats = client.get("/api/stats").json()
    assert stats["totals"]["visitors"] == 1
    assert stats["totals"]["pageviews"] == 6


def test_different_user_agents_are_different_visitors(client):
    r1 = client.post("/track", json={"url": "https://e.com/", "event": "pageview",
                                     "timestamp": int(time.time() * 1000)},
                     headers={"User-Agent": "Mozilla/1.0"})
    r2 = client.post("/track", json={"url": "https://e.com/", "event": "pageview",
                                     "timestamp": int(time.time() * 1000)},
                     headers={"User-Agent": "Mozilla/2.0"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert client.get("/api/stats").json()["totals"]["visitors"] == 2


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_shape(client):
    _pageview(client, "https://example.com/pricing", "https://twitter.com")
    stats = client.get("/api/stats?days=30").json()

    assert set(stats.keys()) >= {"version", "days", "generated_at", "totals",
                                 "series", "top_pages", "top_referrers", "top_events"}
    assert set(stats["totals"].keys()) == {"pageviews", "events", "visitors", "pages"}
    assert stats["days"] == 30
    assert len(stats["series"]) == 30
    assert isinstance(stats["top_pages"], list)


def test_stats_days_bounds(client):
    assert client.get("/api/stats?days=1").status_code == 200
    assert client.get("/api/stats?days=366").status_code == 200
    assert client.get("/api/stats?days=0").status_code == 422
    assert client.get("/api/stats?days=100000").status_code == 422


def test_top_pages_normalises_to_path(client):
    _pageview(client, "https://shop.example.com/pricing?id=1", "https://twitter.com")
    _pageview(client, "https://other.example.com/about?utm_source=twitter",
              "https://google.com")
    stats = client.get("/api/stats").json()
    paths = [p["path"] for p in stats["top_pages"]]
    assert "/pricing?id=1" in paths          # non-tracking param kept
    assert "/about" in paths                 # tracking param stripped
    assert all("utm" not in p for p in paths)


def test_top_referrers(client):
    _pageview(client, "https://e.com/", "https://twitter.com")
    _pageview(client, "https://e.com/", "https://twitter.com")
    _pageview(client, "https://e.com/", "https://google.com")
    stats = client.get("/api/stats").json()
    counts = {r["referrer"]: r["count"] for r in stats["top_referrers"]}
    assert counts["https://twitter.com"] == 2
    assert counts["https://google.com"] == 1


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_json(client):
    _pageview(client)
    r = client.get("/api/export?days=30&format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert set(body["hits"][0].keys()) >= {"timestamp", "url", "path", "referrer",
                                           "event", "data", "visitor", "page"}


def test_export_csv(client):
    _pageview(client)
    r = client.get("/api/export?days=30&format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].endswith("openanalytics-export.csv")
    text = r.text
    assert text.splitlines()[0].startswith("timestamp")
    assert len(text.splitlines()) == 2


def test_export_rejects_unknown_format(client):
    assert client.get("/api/export?format=xml").status_code == 422


# ---------------------------------------------------------------------------
# dashboard + snippet
# ---------------------------------------------------------------------------


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "openanalytics" in r.text
    assert "Unique visitors" in r.text
    assert "Top pages" in r.text


def test_snippet_served_with_host_substituted(client):
    r = client.get("/snippet.js", headers={
        "Host": "analytics.example.com",
        "X-Forwarded-Proto": "https",
    })
    assert r.status_code == 200
    assert "application/javascript" in r.headers["content-type"]
    assert "https://analytics.example.com/track" in r.text
    assert "__OPENANALYTICS_URL__" not in r.text


def test_snippet_scheme_follows_x_forwarded_proto(client):
    """Regression: the substituted URL must carry the real scheme.

    Hardcoding ``https://`` broke plain-HTTP installs (dev, LAN, http
    subdomains) with ``Failed to fetch``.
    """
    r = client.get("/snippet.js", headers={
        "Host": "127.0.0.1:8000",
        "X-Forwarded-Proto": "http",
    })
    assert "http://127.0.0.1:8000/track" in r.text
    assert "https://127.0.0.1:8000" not in r.text


def _strip_js_comments(text: str) -> str:
    """Remove ``//`` line comments and ``/* */`` block comments."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def test_snippet_is_self_contained():
    """The snippet must pull in no external libraries or tracking pixels."""
    text = _strip_js_comments(
        (TEST_DIR.parent / "static" / "snippet.js").read_text(encoding="utf-8")
    )
    assert "import " not in text
    assert "require(" not in text
    # no image-pixel / image-object tracking
    assert "new Image(" not in text
    assert "createElement('img')" not in text
    assert 'createElement("img")' not in text
    assert "document.cookie" not in text   # no cookies
    assert "localStorage" not in text
    assert "sessionStorage" not in text
    # and it must still be doing the actual work
    assert "sendBeacon" in text
    assert "fetch" in text
    assert "analytics" in text


def test_static_icon_served(client):
    r = client.get("/static/icon.svg")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]


def test_snippet_serves_minified_by_default(client):
    r = client.get("/snippet.js")
    text = r.text
    assert len(text.encode()) < 2048, f"snippet is {len(text.encode())} bytes, want < 2KB"
    assert "var c=window.OPENANALYTICS" in text     # minified shape
    # the dev build is larger and carries comments
    dev = (TEST_DIR.parent / "static" / "snippet.js").read_text(encoding="utf-8")
    assert len(dev.encode()) > len(text.encode())
    assert "function submit(ev)" in dev


def test_snippet_dev_build_is_selectable(client, monkeypatch):
    monkeypatch.setenv("OPENANALYTICS_SNIPPET", "dev")
    r = client.get("/snippet.js")
    assert "function send(ev)" in r.text     # readable source, not minified


def _run_snippet_in_vm(src, *, config=None, location="https://e.com/",
                       referrer="https://twitter.com/ref", no_fetch=False):
    """Execute the shipped snippet in a sandboxed node VM.

    Returns ``(script, node)``; append the assertions you need and call node
    with the script. ``fetch`` is primary and ``sendBeacon`` is the fallback,
    so the sandbox records calls to both in one flat ``sent`` list.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    src = src.replace("__OPENANALYTICS_URL__", "http://localhost:1")
    win = "{}".format(config or "{}")
    fetch_stub = (
        "function (u, o) { if (o && o.body) capture(o.body); return Promise.resolve({}); }"
        if not no_fetch else "undefined"
    )
    script = r"""
const vm = require('vm');
let sent = [];
const capture = (payload) =>
  sent.push(typeof payload === 'string' ? JSON.parse(payload) : JSON.parse(payload.text));
const sandbox = {
  window: { OPENANALYTICS: %s, addEventListener: () => {} },
  document: { readyState: 'complete', referrer: %r, addEventListener: () => {} },
  location: { href: %r },
  navigator: { sendBeacon: (u, b) => { capture(b); return true; } },
  Blob: class { constructor(p, o) { this.text = p.join(''); } },
  console: { log() {} },
  setTimeout: (fn) => { fn(); return 0; },
  fetch: %s,
  JSON, Object, Date, RegExp,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(%r, sandbox);
""" % (win, referrer, location, fetch_stub, src)
    return script, node


def _assert_payloads(script, expect=None):
    """Run ``script`` under node and return the parsed ``sent`` list."""
    out = subprocess.run([shutil.which("node"), "-e", script],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        pytest.fail(f"snippet runtime failed:\n{out.stderr}")
    events = json.loads(out.stdout.strip().splitlines()[-1])
    if expect is not None:
        assert len(events) == expect, events
    return events


MIN = (TEST_DIR.parent / "static" / "snippet.min.js").read_text(encoding="utf-8")


def test_snippet_sends_post_flush_events():
    """Regression: track() called *after* the initial pageview flush must send.

    Before the fix, late events were queued but never dispatched, so custom
    events fired after page load vanished silently. Run the real snippet in a
    sandboxed VM so we exercise the shipped code, not a re-implementation.
    """
    script, _ = _run_snippet_in_vm(MIN)
    script += r"""
sandbox.window.analytics.track('signup', { plan: 'pro' });
sandbox.window.analytics.pageview('https://e.com/x');
console.log(JSON.stringify(sent));
"""
    events = _assert_payloads(script)
    kinds = [(e["event"], e.get("data")) for e in events]
    # 1) initial auto pageview  2) track('signup')  3) pageview('/x')
    assert ("pageview", None) in kinds
    assert ("signup", {"plan": "pro"}) in kinds, kinds
    assert any(e["url"] == "https://e.com/x" for e in events)
    # the initial pageview must carry the referrer and a real timestamp
    first = events[0]
    assert first["referrer"] == "https://twitter.com/ref"
    assert isinstance(first["timestamp"], int) and first["timestamp"] > 1e12


def test_snippet_falls_back_to_send_beach_when_no_fetch():
    """Without fetch the snippet must still deliver via sendBeacon."""
    script, _ = _run_snippet_in_vm(MIN, no_fetch=True)
    script += "console.log(JSON.stringify(sent));\n"
    events = _assert_payloads(script, expect=1)
    assert events[0]["event"] == "pageview"
    assert events[0]["url"] == "https://e.com/"


def test_snippet_excludes_matching_urls():
    """An `exclude` pattern must suppress pageviews and events."""
    script, _ = _run_snippet_in_vm(
        MIN, config="{ exclude: [/\\/search\\?/] }",
        location="https://e.com/search?q=hello",
    )
    script += r"""
sandbox.window.analytics.track('x', null);
console.log(JSON.stringify(sent));
"""
    assert _assert_payloads(script, expect=0) == []


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_preflight_allowed(client):
    """Cross-origin POSTs are the common deployment case, so they must work."""
    r = client.options("/track", headers={
        "Origin": "https://shop.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type",
    })
    assert r.status_code == 200
    assert "access-control-allow-origin" in r.headers
    assert "POST" in r.headers["access-control-allow-methods"]
    assert "content-type" in r.headers["access-control-allow-headers"].lower()


def test_cors_headers_present_on_track(client):
    r = client.post("/track", json={"url": "https://e.com/", "event": "pageview"},
                    headers={"Origin": "https://e.com"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["hits"] == 0
    assert "version" in body and "db" in body and "time" in body


def test_health_reflects_ingestion(client):
    _pageview(client)
    _pageview(client)
    assert client.get("/api/health").json()["hits"] == 2


# ---------------------------------------------------------------------------
# Python SDK
# ---------------------------------------------------------------------------


def test_sdk_pageview_and_track(client):
    oa = OpenAnalytics("http://testserver")
    oa._endpoint = "/track"

    hit = oa.pageview("https://example.com/pricing", referrer="https://twitter.com")
    assert hit["event"] == "pageview"
    assert hit["url"] == "https://example.com/pricing"

    hit = oa.track("signup", {"plan": "pro"}, url="https://example.com/")
    assert hit["event"] == "signup"
    assert hit["data"] == {"plan": "pro"}


def test_sdk_requires_base_url():
    with pytest.raises(ValueError):
        OpenAnalytics(None)


def test_sdk_rejects_bad_event_name(client):
    oa = OpenAnalytics("http://testserver")
    with pytest.raises(ValueError):
        oa.track("")
    with pytest.raises(ValueError):
        oa.track(123)


def test_sdk_rejects_bad_data(client):
    oa = OpenAnalytics("http://testserver")
    with pytest.raises(TypeError):
        oa.track("x", data=object())


def test_sdk_batching(client, monkeypatch):
    oa = OpenAnalytics("http://testserver", batch_size=2, flush_interval=0)
    sent: list[object] = []
    monkeypatch.setattr(oa, "_send", lambda hits: sent.extend(hits))

    oa.pageview("https://e.com/a")
    assert sent == []                       # buffered
    oa.pageview("https://e.com/b")
    assert len(sent) == 2                   # flushed at batch_size

    oa.pageview("https://e.com/c")
    assert oa.flush() == 1                  # explicit flush
    assert len(sent) == 3


def test_sdk_swallows_send_errors(client, capsys):
    """A broken endpoint must never raise into the caller."""
    oa = OpenAnalytics("http://127.0.0.1:1", timeout=1)
    oa.pageview("https://e.com/")           # must not raise
    out = capsys.readouterr()
    assert "send failed" in out.out


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_to_path():
    assert server._to_path("https://shop.example.com/pricing?x=1") == "/pricing?x=1"
    assert server._to_path("https://shop.example.com") == "/"
    assert server._to_path("/already-relative") == "/already-relative"
    assert server._to_path("ftp://x/y") == "/y"
    assert server._to_path("https://s.com/a#frag") == "/a"


def test_to_path_strips_tracking_params():
    """UTM/campaign params must not split one page into many."""
    assert server._to_path("https://s.com/pricing?utm_source=twitter") == "/pricing"
    assert server._to_path(
        "https://s.com/pricing?utm_source=x&utm_medium=social&id=1"
    ) == "/pricing?id=1"
    assert server._to_path("https://s.com/?gclid=abc123") == "/"
    assert server._to_path("https://s.com/a?b=c&utm_campaign=z") == "/a?b=c"
    # non-tracking params survive, case-insensitive keys
    assert server._to_path("https://s.com/a?UTM_SOURCE=x&keep=1") == "/a?keep=1"


def test_same_page_with_different_utm_is_one_page(client):
    _pageview(client, "https://s.com/pricing?utm_source=twitter")
    _pageview(client, "https://s.com/pricing?utm_source=google")
    stats = client.get("/api/stats").json()
    assert stats["totals"]["pages"] == 1
    assert len(stats["top_pages"]) == 1
    assert stats["top_pages"][0]["path"] == "/pricing"
    assert stats["top_pages"][0]["count"] == 2


def test_stats_window_includes_today(client):
    """The N-day window must cover today, not exclude it."""
    _pageview(client)
    stats = client.get("/api/stats?days=7").json()
    today = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).date().isoformat()
    assert stats["series"][-1]["date"] == today
    assert sum(d["pageviews"] for d in stats["series"]) == 1


def test_stats_window_is_date_bucketed_not_rolling(client):
    """All hits land in today's bucket regardless of wall-clock time."""
    now = int(time.time())
    client.post("/track", json={"url": "https://e.com/", "event": "pageview",
                                "timestamp": now})
    yesterday = now - 86400
    client.post("/track", json={"url": "https://e.com/", "event": "pageview",
                                "timestamp": yesterday})
    stats = client.get("/api/stats?days=2").json()
    assert len(stats["series"]) == 2
    assert stats["totals"]["pageviews"] == 2
    assert sum(d["pageviews"] for d in stats["series"]) == 2


def test_visitor_id_is_stable_and_salt_sensitive():
    a = server._visitor_id("1.2.3.4", "UA-1")
    assert a == server._visitor_id("1.2.3.4", "UA-1")
    assert a != server._visitor_id("1.2.3.5", "UA-1")
    assert a != server._visitor_id("1.2.3.4", "UA-2")
    assert len(a) == 32
