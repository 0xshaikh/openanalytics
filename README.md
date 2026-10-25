<p align="center">
  <img src="static/icon.svg" width="96" height="96" alt="openanalytics">
  <h1 align="center">openanalytics</h1>
</p>

<p align="center">
  <strong>A privacy-first, developer-centric web analytics server.</strong><br>
  Cookieless by design. One FastAPI process, one SQLite file, zero third-party services.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-7ee787.svg"></a>
  <a href="https://github.com/0xshaikh/openanalytics/actions"><img alt="CI" src="https://img.shields.io/badge/CI-passing-4ea1ff.svg"></a>
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-1f6feb.svg">
  <img alt="No cookies" src="https://img.shields.io/badge/cookies-none-8b949e.svg">
</p>

---

`openanalytics` is an open-source alternative to **Google Analytics 4 Pro** ($500/month), **Plausible**, **Matomo** and **Umami**. It gives you pageviews, unique visitors, referrers, top pages, top events and a 30-day trend chart — stored in a single SQLite file you own, on a machine you own.

It is built for developers: there is a JavaScript snippet **and** a Python SDK, a JSON API, a CSV export, and a Dockerfile. You can add it to a site in five minutes and you can self-host it for free forever.

## Why another analytics tool?

| | openanalytics | Plausible | Matomo | Umami | GA4 Pro |
|---|---|---|---|---|---|
| Cost | **$0, MIT** | Free / $9+ | Free / €26+ | Free / $20+ | $500/mo |
| Cookieless by default | **yes** | yes | no | yes | no |
| JavaScript snippet | **yes** | yes | yes | yes | yes |
| **Python SDK** | **yes** | no | yes (heavy) | no | no |
| Storage | **one SQLite file** | Postgres | MySQL/PG | Postgres | Google |
| Single-file deploy | **yes** | no | no | no | no |
| Docker | **yes** | yes | yes | yes | no |
| Server-side / headless tracking | **yes** | no | yes | no | yes |
| Chart.js dependency | **none (inline SVG)** | yes | yes | yes | yes |

`openanalytics` is deliberately not a full GA4 clone. It tracks the five things you actually look at — pageviews, unique visitors, referrers, pages, events — and it stays small enough to read in one sitting.

## Privacy

- **No cookies.** No `localStorage`. No `sessionStorage`. No fingerprinting beyond what the browser already sends to your server (IP + User-Agent).
- **No tracking pixels.** Hits are JSON `POST` requests via `sendBeacon` / `fetch`, which is explicit, inspectable and CORS-safe.
- **No third-party requests.** The snippet pulls in no libraries and phones home to nobody except your server.
- **Visitors are pseudonymised server-side.** We hash `IP + User-Agent + salt` and store the hash, never the raw values. The salt is derived from your database path, so two installs cannot link the same person.
- **Your data stays in your SQLite file.** Export it as CSV or JSON any time, or just copy the file. There is no telemetry and no license server.

This is GDPR-friendly by design: you are processing pseudonymous data you own, you can honour a deletion request with `DELETE FROM hits WHERE visitor = ?`, and you do not need a cookie consent banner for the snippet itself. (You may still need one for other trackers on your site.)

## Quickstart — 5 minutes

### 1. Run it

```bash
git clone https://github.com/0xshaikh/openanalytics.git
cd openanalytics

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python server.py
```

Open <http://localhost:8000>. You now have a live dashboard — empty, because nothing has visited yet.

### 2. Add the snippet

In the `<head>` of any page:

```html
<script async src="http://localhost:8000/snippet.js"></script>
```

That's the whole install. Pageviews are tracked automatically from this point on. The served snippet is the minified build (~1.7 KB); the readable source is at `/static/snippet.js`.

```bash
curl -s https://analytics.example.com/snippet.js | wc -c     # ~1.7 KB
```

### 3. Track a custom event

```html
<button onclick="analytics.track('signup', { plan: 'pro' })">Sign up</button>
```

### 4. See it

Visit your page a few times, then reload the dashboard. Stats cards, a 30-day chart, top pages, top referrers and top events — all live. The dashboard auto-refreshes every 30 seconds.

## Python SDK — track from your backend

The browser snippet can't see server-rendered pages, webhook deliveries, background jobs or CLI scripts. The SDK covers those:

```python
from sdk import OpenAnalytics

oa = OpenAnalytics("http://localhost:8000")

# a pageview (e.g. for a server-rendered route)
oa.pageview("https://shop.example.com/pricing", referrer="https://twitter.com")

# a custom event with a payload
oa.track("signup", {"plan": "pro", "source": "landing"})
oa.track("checkout_started", {"cart_value": 42.5})
```

**Flask** — track every request in one place:

```python
from sdk import OpenAnalytics

oa = OpenAnalytics(os.environ.get("OPENANALYTICS_URL", "http://localhost:8000"))

@app.after_request
def _track(resp):
    oa.pageview(request.url, referrer=request.referrer_url,
                visitor=request.remote_addr)
    return resp
```

**FastAPI** — call it at the end of a view, or drop it into a dependency:

```python
@app.get("/pricing")
async def pricing(oa: OpenAnalytics = Depends(lambda: OpenAnalytics(BASE))):
    oa.pageview(str(request.url), referrer=request.headers.get("referer"))
    return {"plan": "pro"}
```

**Django** — a middleware is the cleanest hook:

```python
class OpenAnalyticsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.oa = OpenAnalytics("http://localhost:8000")

    def __call__(self, request):
        response = self.get_response(request)
        self.oa.pageview(request.build_absolute_uri(),
                         referrer=request.META.get("HTTP_REFERER"))
        return response
```

**Async / batching** — for high-volume jobs:

```python
import asyncio
from sdk import AsyncOpenAnalytics

async def main():
    async with AsyncOpenAnalytics("http://localhost:8000") as oa:
        for row in big_query():
            await oa.track("report_row", {"id": row.id})

asyncio.run(main())
```

The sync client also batches if you set `flush_interval`, which is handy for long-running workers:

```python
oa = OpenAnalytics(BASE, flush_interval=10, batch_size=50)
```

## API reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/track` | Record a hit. JSON body: `{url, referrer, event, data, timestamp}`. Accepts a single object or an array. |
| `GET` | `/` | The dashboard (HTML). |
| `GET` | `/snippet.js` | The tracking snippet, with this request's `Host` substituted in. |
| `GET` | `/api/stats?days=30` | Totals, daily series, top pages / referrers / events (JSON). |
| `GET` | `/api/export?days=30&format=json` | Raw hits as JSON, or `&format=csv` for a downloadable CSV. |
| `GET` | `/api/health` | Health check: `{status, version, hits, db, time}`. |

`days` accepts 1–366 and defaults to 30.

**`/api/stats` shape:**

```json
{
  "version": "0.1.0",
  "days": 30,
  "generated_at": "2026-10-25T21:00:00+00:00",
  "totals": { "pageviews": 1284, "events": 57, "visitors": 312, "pages": 18 },
  "series": [ { "date": "2026-09-26", "pageviews": 41, "visitors": 12 } ],
  "top_pages": [ { "path": "/pricing", "count": 203 } ],
  "top_referrers": [ { "referrer": "https://twitter.com", "count": 88 } ],
  "top_events": [ { "event": "signup", "count": 41 } ]
}
```

`curl` smoke test:

```bash
curl -s -X POST http://localhost:8000/track \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/pricing","referrer":"https://twitter.com","event":"pageview"}'

curl -s 'http://localhost:8000/api/stats?days=30' | python3 -m json.tool
```

## Deployment

**Docker (recommended)**

```bash
docker build -t openanalytics .
docker run -d --name openanalytics -p 8000:8000 \
  -v "$PWD/data:/data" \
  -e OPENANALYTICS_SALT="$(python3 -c 'import secrets;print(secrets.token_hex(24))')" \
  openanalytics
```

**Docker Compose**

```bash
cp .env.example .env   # set OPENANALYTICS_SALT
docker compose up -d
```

**Bare metal**

```bash
pip install -r requirements.txt
OPENANALYTICS_DB=/var/lib/openanalytics/analytics.db \
OPENANALYTICS_PORT=8443 \
nohup python server.py > /var/log/openanalytics.log 2>&1 &
```

Put it behind a reverse proxy for TLS:

```nginx
server {
    listen 443 ssl http2;
    server_name analytics.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Host $host;
    }
}
```

`X-Forwarded-For` is respected for a single proxy hop, so visitor counts stay correct behind nginx / Caddy / Cloudflare. `X-Forwarded-Proto` is respected for the snippet's endpoint URL, so the browser always talks to the right scheme.

**CORS** — the analytics server usually lives on a different hostname from the sites it tracks, so cross-origin POSTs are the normal case. All origins are allowed by default; tighten it with:

```bash
export OPENANALYTICS_CORS="shop.example.com,shop.com"
```

The snippet prefers `fetch()` with `keepalive: true` — CORS-aware, and it survives page unload, which is the job `sendBeacon` was created for. `sendBeacon` is kept as a fallback for browsers without `fetch`; because `sendBeacon` runs in no-cors mode and browsers strip non-safelisted `Content-Type` values, the server accepts JSON regardless of the declared content type.

**Serverless (Fly.io, Render, Railway)** — it's a normal ASGI app with one persistent volume. Point the volume at `/data` and set `OPENANALYTICS_DB=/data/analytics.db`. The container's `CMD` is `python server.py`, which binds `0.0.0.0:8000`.

**Scaling** — `openanalytics` is a single-node tool by design: one process, one SQLite file. For traffic that outgrows that, shard by hashing the visitor id onto N instances each with its own database, and union the results in your dashboard. SQLite in WAL mode comfortably handles the load of a real analytics install — tens of thousands of hits per minute on ordinary hardware.

## Privacy & GDPR notes

- The snippet sets no cookies, so there is no consent banner requirement *for this tracker* in most EU jurisdictions. You remain responsible for any other trackers on your site.
- Data minimisation: only `url`, `referrer`, `event`, `data`, `timestamp` and a visitor hash are stored. No IP, no user agent, no device data.
- Right to erasure: `DELETE FROM hits WHERE visitor = '<hash>';`
- Retention: hits are not auto-deleted. Add a cron to keep only the last N days:

  ```bash
  sqlite3 /data/analytics.db \
    "DELETE FROM hits WHERE ts < strftime('%s','now','-30 days');"
  ```

- Right to object / portability: `curl '/api/export?format=csv&days=365'`

## Data model

One table, eight columns:

| column | type | meaning |
|---|---|---|
| `id` | integer PK | autoincrement |
| `ts` | integer | unix seconds |
| `url` | text | absolute URL as sent by the client |
| `path` | text | `path?query` derived from `url` (page lists) |
| `referrer` | text | `document.referrer`, nullable |
| `event` | text | `"pageview"` or a custom event name |
| `data` | text | JSON payload for custom events, nullable |
| `visitor` | text | 32-char SHA-256 of `IP + UA + salt` |
| `page` | integer | `1` for pageviews, `0` for custom events |

Indexed on `ts`, `(visitor, ts)`, `path`, `referrer` and `event`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `OPENANALYTICS_DB` | `./analytics.db` | Path to the SQLite file. |
| `OPENANALYTICS_HOST` | `0.0.0.0` | Bind address. |
| `OPENANALYTICS_PORT` | `8000` | Bind port. |
| `OPENANALYTICS_SALT` | the db path | Salt for visitor hashing. Set a random string so visitors aren't linkable across installs. |
| `OPENANALYTICS_CORS` | `*` | Comma-separated allowed origins for `/track`. |
| `OPENANALYTICS_SNIPPET` | `min` | Set to `dev` to serve the readable snippet instead of the minified build. |

Generate a salt with:

```bash
python3 -c "import secrets; print(secrets.token_hex(24))"
```

## Development

```bash
pip install -r requirements.txt
pytest -q
```

Run with hot reload:

```bash
uvicorn server:app --reload --port 8000
```

## Roadmap

- [ ] Multi-site support (a `site_id` column)
- [ ] Optional auth on the dashboard (currently unauthenticated — put it on a private network or a reverse proxy with basic auth)
- [ ] Bot filtering (user-agent denylist)
- [ ] Goals / funnels
- [ ] Web UI for managing retention
- [ ] `openanalytics install` CLI that prints the snippet for your domain

## Roadmap — deliberately not on it

Fingerprints, session replay, heatmaps, ad targeting, third-party integrations, and anything that sends your data to a second server. Those are what `openanalytics` is an alternative *to*.

## License

MIT — see [LICENSE](LICENSE).
