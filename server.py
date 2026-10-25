#!/usr/bin/env python3
"""openanalytics — a privacy-first, single-file web analytics server.

A drop-in, cookieless replacement for Google Analytics. One FastAPI
process, one SQLite file, no third-party services, no tracking cookies.

Endpoints
---------
POST  /track         receive a pageview or custom event
GET   /              render the dashboard
GET   /snippet.js    serve the client-side tracking snippet
GET   /api/stats     aggregated stats for the dashboard (JSON)
GET   /api/export    raw hits export (CSV or JSON)
GET   /api/health    health check

Run it
------
    python server.py                 # -> http://localhost:8000
    uvicorn server:app --reload      # developer mode
    OPENANALYTICS_DB=/data/oa.db OPENANALYTICS_PORT=9000 python server.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

__version__ = "0.1.0"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("OPENANALYTICS_DB", BASE_DIR / "analytics.db"))
HOST = os.environ.get("OPENANALYTICS_HOST", "0.0.0.0")
PORT = int(os.environ.get("OPENANALYTICS_PORT", "8000"))

#: Origins allowed to POST hits. Comma-separated; default ``*``.
#: The analytics server is usually on a different hostname from the site it
#: tracks, so cross-origin POSTs are the normal case, not the exception.
#: Set ``OPENANALYTICS_CORS=shop.example.com,shop.com`` to tighten it.
CORS_ORIGINS = [o.strip() for o in
                os.environ.get("OPENANALYTICS_CORS", "*").split(",") if o.strip()]

SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    url       TEXT    NOT NULL,
    path      TEXT    NOT NULL,
    referrer  TEXT,
    event     TEXT    NOT NULL DEFAULT 'pageview',
    data      TEXT,
    visitor   TEXT    NOT NULL,
    page      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hits_ts ON hits(ts);
CREATE INDEX IF NOT EXISTS idx_hits_visitor_ts ON hits(visitor, ts);
CREATE INDEX IF NOT EXISTS idx_hits_path ON hits(path);
CREATE INDEX IF NOT EXISTS idx_hits_referrer ON hits(referrer);
CREATE INDEX IF NOT EXISTS idx_hits_event ON hits(event);
"""

# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------


def _sync_init() -> None:
    """Initialise the SQLite schema at startup (synchronous, fast)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        # Forward migration for databases created before the `path` column.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(hits)")}
        if "path" not in cols:
            conn.execute("ALTER TABLE hits ADD COLUMN path TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE hits SET path = '' WHERE path = ''")
            # best-effort backfill from the stored URL
            rows = conn.execute("SELECT id, url FROM hits WHERE path = ''").fetchall()
            conn.executemany("UPDATE hits SET path = ? WHERE id = ?",
                             [(_to_path(u), i) for i, u in rows])
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hits_path ON hits(path)")


def _visitor_id(ip: str, user_agent: str) -> str:
    """Deterministic pseudonym for a visitor.

    We hash IP + User-Agent so we never store either raw value. The salt is
    derived from the database path, so two independent installs cannot link
    visitors to each other and the hash is stable for de-duplication.
    """
    salt = os.environ.get("OPENANALYTICS_SALT", str(DB_PATH))
    raw = f"{ip}|{user_agent}|{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _client_ip(request: Request) -> str:
    """Best-effort client IP, respecting a single proxy hop."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


#: Query parameters that identify the *campaign*, not the page. Keeping them
#: would turn ``/pricing?utm_source=twitter`` and ``/pricing?utm_source=google``
#: into two different pages and inflate every page-level number. They're
#: already captured in the referrer.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_cjb",
    "mc_cid", "mc_eid",                      # Mailchimp
    "gclid", "gclsrc", "dclid",               # Google Ads
    "fbclid", "igshid",                       # Facebook / Instagram
    "twclid",                                 # Twitter
    "msclkid",                                # Microsoft
    "yclid",                                  # Yandex
    "vero_id",                                # Criteo
    "ref", "ref_src", "ref_url",
}


def _to_path(url: str) -> str:
    """Reduce an absolute URL to ``path?query`` for readable page lists.

    ``https://shop.example.com/pricing?utm_source=x&id=1`` -> ``/pricing?id=1``.
    Falls back to the original string when there is no scheme. Tracking
    parameters (see ``_TRACKING_PARAMS``) are dropped so they don't split one
    page into many.
    """
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+(/[^?#]*)?(\?[^#]*)?(#.*)?$", url)
    if not m:
        return url
    path = m.group(1) or "/"
    query = m.group(2) or ""
    if query:
        kept = []
        for part in query[1:].split("&"):
            if not part:
                continue
            key = part.split("=", 1)[0].lower()
            if key not in _TRACKING_PARAMS:
                kept.append(part)
        if kept:
            return path + "?" + "&".join(kept)
    return path


def _normalize_ts(value: Any) -> int:
    """Coerce a client timestamp to **unix seconds**.

    Browsers give us milliseconds (``Date.now()``) and most JS trackers send
    those; the Python SDK sends seconds. Anything above ``1e12`` is
    unambiguously milliseconds, so we divide. Anything below is treated as
    seconds. This keeps every stored value in one unit, which is what makes
    ``date(ts, 'unixepoch')`` and the retention cron work correctly.
    """
    ts = float(value)
    if ts > 1e12:  # milliseconds
        ts /= 1000.0
    return int(ts)


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _sync_init()
    yield


app = FastAPI(
    title="openanalytics",
    version=__version__,
    description="Privacy-first, cookieless web analytics. SQLite, one file.",
    lifespan=lifespan,
)

# Cross-origin access is the common case: the analytics server usually lives
# on a different hostname from the sites it tracks. Origins are echoed back
# via `allow_origin_regex` so `credentials: omit` fetches keep working.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=".*" if "*" in CORS_ORIGINS else None,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --------------------------------------------------------------------------
# ingestion
# --------------------------------------------------------------------------


@app.post("/track", response_class=PlainTextResponse)
async def track(request: Request):
    """Record a pageview or custom event.

    Accepts either a single hit object or an array of hits (used by the
    Python SDK's batching mode). Each hit carries ``url`` / ``referrer`` /
    ``event`` / ``data`` / ``timestamp``; the server adds the visitor
    pseudonym and the ``page`` flag (a pageview is any hit with no event
    name other than ``pageview``).
    """
    try:
        body = await request.json()
    except Exception:
        # `navigator.sendBeacon()` posts a `application/json` Blob, but it
        # runs in no-cors mode when the analytics host differs from the
        # page's origin. Browsers strip the Content-Type on non-safelisted
        # values, so the JSON arrives undeclared and `request.json()` fails.
        # Fall back to parsing the raw body so those hits aren't lost.
        raw = (await request.body()).decode("utf-8", "replace").strip()
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(status_code=400, detail="expected JSON body")

    hits = body if isinstance(body, list) else [body]
    if not hits:
        raise HTTPException(status_code=400, detail="empty payload")

    visitor = _visitor_id(_client_ip(request), request.headers.get("user-agent", ""))
    rows: list[tuple] = []

    for item in hits:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="each hit must be an object")
        event = str(item.get("event") or "pageview").strip() or "pageview"
        url = str(item.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        referrer = str(item.get("referrer") or "") or None
        data = item.get("data")
        try:
            ts = _normalize_ts(item.get("timestamp") or datetime.now(timezone.utc).timestamp())
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timestamp must be a number")

        page = 1 if event == "pageview" else 0
        path = _to_path(url) if page else url
        v = str(item.get("visitor") or "") or visitor
        rows.append((ts, url, path, referrer, event,
                     json.dumps(data) if data is not None else None, v, page))

    conn = await _connect()
    try:
        await conn.executemany(
            "INSERT INTO hits (ts, url, path, referrer, event, data, visitor, page) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()
    finally:
        await conn.close()

    return "ok"


# --------------------------------------------------------------------------
# query / stats
# --------------------------------------------------------------------------


async def _rowcount(conn: aiosqlite.Connection, sql: str, params: tuple = ()) -> int:
    cur = await conn.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row else 0


@app.get("/api/stats", response_class=JSONResponse)
async def api_stats(days: int = Query(default=30, ge=1, le=366)):
    """Aggregated statistics for the dashboard.

    Returns totals plus the last ``days`` day-by-day series, top pages,
    top referrers and top events.
    """
    conn = await _connect()
    try:
        now = datetime.now(timezone.utc)
        # A N-day window means the N most recent days *including today*.
        # Bucketing on date boundaries (not rolling 24h) means the chart
        # fills in as the day progresses instead of shifting under you.
        today = now.date()
        first_day = today - timedelta(days=days - 1)
        since = int(
            datetime.combine(first_day, dt_time.min, tzinfo=timezone.utc).timestamp()
        )

        totals = {}
        totals["pageviews"] = await _rowcount(conn, "SELECT COUNT(*) FROM hits WHERE page = 1 AND ts >= ?", (since,))
        totals["events"] = await _rowcount(conn, "SELECT COUNT(*) FROM hits WHERE page = 0 AND ts >= ?", (since,))
        totals["visitors"] = await _rowcount(conn, "SELECT COUNT(DISTINCT visitor) FROM hits WHERE ts >= ?", (since,))
        totals["pages"] = await _rowcount(conn, "SELECT COUNT(DISTINCT path) FROM hits WHERE ts >= ? AND page = 1", (since,))

        # day-by-day series (local dates, UTC day buckets)
        cur = await conn.execute(
            "SELECT date(ts, 'unixepoch') AS d, "
            "COUNT(*) AS pageviews, "
            "COUNT(DISTINCT visitor) AS visitors "
            "FROM hits WHERE ts >= ? AND page = 1 "
            "GROUP BY d ORDER BY d",
            (since,),
        )
        rows = await cur.fetchall()
        by_day = {r["d"]: {"pageviews": r["pageviews"], "visitors": r["visitors"]} for r in rows}

        series: list[dict[str, Any]] = []
        for i in range(days):
            day = (first_day + timedelta(days=i)).isoformat()
            bucket = by_day.get(day, {"pageviews": 0, "visitors": 0})
            series.append({"date": day, **bucket})

        top_pages = await _fetch_top(conn, "path", "path",
                                    "SELECT path, COUNT(*) AS c FROM hits "
                                    "WHERE page = 1 AND ts >= ? GROUP BY path "
                                    "ORDER BY c DESC, path LIMIT 20", (since,))
        top_referrers = await _fetch_top(conn, "referrer", "referrer",
                                         "SELECT referrer, COUNT(*) AS c FROM hits "
                                         "WHERE referrer IS NOT NULL AND ts >= ? "
                                         "GROUP BY referrer ORDER BY c DESC LIMIT 20", (since,))
        top_events = await _fetch_top(conn, "event", "event",
                                      "SELECT event, COUNT(*) AS c FROM hits "
                                      "WHERE event != 'pageview' AND ts >= ? "
                                      "GROUP BY event ORDER BY c DESC LIMIT 20", (since,))

        return {
            "version": __version__,
            "days": days,
            "generated_at": now.isoformat(),
            "totals": totals,
            "series": series,
            "top_pages": top_pages,
            "top_referrers": top_referrers,
            "top_events": top_events,
        }
    finally:
        await conn.close()


async def _fetch_top(conn: aiosqlite.Connection, db_col: str, out_key: str,
                     sql: str, params: tuple) -> list[dict]:
    """Run a GROUP-BY query and normalise its rows to ``[{out_key, count}]``."""
    cur = await conn.execute(sql, params)
    rows = await cur.fetchall()
    return [{out_key: r[db_col] or "(direct)", "count": int(r["c"])} for r in rows]


@app.get("/api/export", response_class=JSONResponse)
async def api_export(days: int = Query(default=30, ge=1, le=366),
                     format: str = Query(default="json", pattern="^(json|csv)$")):
    """Export raw hits as JSON or CSV."""
    conn = await _connect()
    try:
        since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        cur = await conn.execute(
            "SELECT ts, url, path, referrer, event, data, visitor, page FROM hits "
            "WHERE ts >= ? ORDER BY ts",
            (since,),
        )
        rows = await cur.fetchall()
        items = [
            {
                "timestamp": datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(),
                "url": r["url"],
                "path": r["path"],
                "referrer": r["referrer"],
                "event": r["event"],
                "data": r["data"],
                "visitor": r["visitor"],
                "page": bool(r["page"]),
            }
            for r in rows
        ]
        if format == "csv":
            import csv
            import io
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(items[0].keys()) if items
                                    else ["timestamp", "url", "path", "referrer", "event", "data", "visitor", "page"])
            writer.writeheader()
            writer.writerows(items)
            from fastapi.responses import StreamingResponse
            return StreamingResponse(
                iter([buf.getvalue()]),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=openanalytics-export.csv"},
            )
        return {"days": days, "count": len(items), "hits": items}
    finally:
        await conn.close()


@app.get("/api/health", response_class=JSONResponse)
async def api_health():
    conn = await _connect()
    try:
        count = await _rowcount(conn, "SELECT COUNT(*) FROM hits")
    finally:
        await conn.close()
    return {"status": "ok", "version": __version__, "hits": count,
            "db": str(DB_PATH), "time": datetime.now(timezone.utc).isoformat()}


# --------------------------------------------------------------------------
# dashboard + snippet
# --------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the dashboard, pre-populated with the last 30 days of stats."""
    stats = await api_stats(days=30)
    return templates.TemplateResponse(request, "dashboard.html", {
        "stats": stats,
        "version": __version__,
    })


@app.get("/snippet.js", response_class=PlainTextResponse)
async def snippet(request: Request):
    """Serve the tracking snippet with this request's base URL substituted in.

    Ships the minified build (~1.7 KB) by default. Set
    ``OPENANALYTICS_SNIPPET=dev`` to get the readable, commented source
    instead — useful when debugging your own install. The readable source
    is also always available verbatim at ``/static/snippet.js``.

    The substitution includes the *scheme*, taken from ``X-Forwarded-Proto``
    when present, so the snippet works whether the server is reached over
    plain HTTP (dev, LAN) or HTTPS (prod behind a proxy).
    """
    host = request.headers.get("host") or f"{HOST}:{PORT}"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return PlainTextResponse(
        _snippet_for(f"{proto}://{host}"), media_type="application/javascript"
    )


def _snippet_for(base_url: str) -> str:
    """Render the snippet, substituting the analytics endpoint's base URL.

    ``base_url`` must include the scheme, e.g. ``https://analytics.example.com``
    or ``http://127.0.0.1:8000``.
    """
    name = "snippet.js" if os.environ.get("OPENANALYTICS_SNIPPET") == "dev" else "snippet.min.js"
    text = (BASE_DIR / "static" / name).read_text(encoding="utf-8")
    return text.replace("__OPENANALYTICS_URL__", base_url)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    _sync_init()
    print(f"openanalytics {__version__} -> http://{HOST}:{PORT}  (db: {DB_PATH})")
    uvicorn.run(app, host=HOST, port=PORT)
