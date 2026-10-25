"""openanalytics SDK — server-side tracking for Python.

The companion to the browser snippet. Use it from your backend whenever a
pageview or event happens outside the browser: server-rendered routes,
webhooks, background jobs, cron scripts, CLI tools.

Sync usage (default, no extra dependencies)
-------------------------------------------
    from openanalytics_sdk import OpenAnalytics
    oa = OpenAnalytics("https://analytics.example.com")

    oa.pageview("https://shop.example.com/pricing", referrer="https://twitter.com")
    oa.track("signup", {"plan": "pro"})

Async usage (requires ``httpx``)
--------------------------------
    import asyncio
    from openanalytics_sdk import AsyncOpenAnalytics

    async with AsyncOpenAnalytics("https://analytics.example.com") as oa:
        await oa.pageview("https://shop.example.com/pricing")
        await oa.track("signup", {"plan": "pro"})

Framework helpers
-----------------
    # Flask
    @app.after_request
    def _track(resp):
        oa.pageview(request.url, referrer=request.referrer_url,
                    visitor=client_ip(request)); return resp

    # FastAPI — the OpenAnalyticsMiddleware shown below
    # Django  — call oa.pageview() at the end of your view, or in a middleware
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__version__ = "0.1.0"

DEFAULT_TIMEOUT = 5.0
DEFAULT_ENDPOINT = "/track"


class OpenAnalytics:
    """Client for an openanalytics server.

    :param base_url: e.g. ``"https://analytics.example.com"`` (no trailing
        slash required). Also reads ``OPENANALYTICS_URL`` from the
        environment when omitted.
    :param endpoint: path relative to ``base_url``. Defaults to ``/track``.
    :param timeout: per-request timeout in seconds.
    :param batch_size: when > 1, hits are buffered and sent as a single
        array once the buffer reaches this size. Pass ``flush_interval`` as
        a positive number to enable an automatic background flusher that
        also drains the buffer on that interval. ``batch_size=1`` (the
        default) sends every hit immediately.
    :param flush_interval: seconds between automatic buffer flushes; ``0``
        (default) disables the timer. Always call ``flush()`` before a
        process exits, or use the client as a context manager.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT,
        flush_interval: float = 0.0,
        batch_size: int = 1,
    ) -> None:
        self.base_url = (base_url or os.environ.get("OPENANALYTICS_URL") or "").rstrip("/")
        if not self.base_url:
            raise ValueError(
                "OpenAnalytics needs a base_url, e.g. "
                "OpenAnalytics('https://analytics.example.com') "
                "or set OPENANALYTICS_URL."
            )
        self.endpoint = endpoint
        self.timeout = timeout
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self._queue: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def pageview(
        self,
        url: str,
        referrer: Optional[str] = None,
        visitor: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> dict[str, Any]:
        """Record a pageview for ``url``. Returns the hit dict."""
        return self._hit(
            event="pageview",
            data=None,
            url=url,
            referrer=referrer,
            visitor=visitor,
            timestamp=timestamp,
        )

    def track(
        self,
        event: str,
        data: Optional[Union[Mapping[str, Any], str, int, float, None]] = None,
        url: Optional[str] = None,
        referrer: Optional[str] = None,
        visitor: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> dict[str, Any]:
        """Record a custom event.

        :param event: event name, e.g. ``"signup"``.
        :param data: optional payload. Must be JSON-serialisable.
        :param url: page the event happened on (required — defaults to
            ``/`` which is usually wrong, so pass it explicitly).
        :param visitor: optional stable pseudonym. If omitted the server
            derives one from the caller's IP and User-Agent.
        """
        if not event or not isinstance(event, str):
            raise ValueError("event must be a non-empty string")
        if data is not None and not isinstance(data, (str, int, float, Mapping)):
            raise TypeError("data must be a mapping, string, number, or None")
        return self._hit(
            event=event,
            data=data,
            url=url or "/",
            referrer=referrer,
            visitor=visitor,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _hit(
        self,
        event: str,
        data: Any,
        url: str,
        referrer: Optional[str],
        visitor: Optional[str],
        timestamp: Optional[float],
    ) -> dict[str, Any]:
        hit = {
            "event": event,
            "data": data,
            "url": url,
            "referrer": referrer,
            "timestamp": int((timestamp or time.time()) * 1000),
        }
        if visitor:
            hit["visitor"] = visitor

        if self.batch_size and self.batch_size > 1:
            self._queue.append(hit)
            if len(self._queue) >= self.batch_size:
                self.flush()
            return hit

        self._send([hit])
        return hit

    def flush(self) -> int:
        """Send any queued hits. Returns the number sent."""
        if not self._queue:
            return 0
        batch, self._queue = self._queue, []
        self._send(batch)
        return len(batch)

    def _send(self, hits: list[dict[str, Any]]) -> None:
        url = self.base_url + self.endpoint
        body = json.dumps(hits[0] if len(hits) == 1 else hits).encode("utf-8")
        req = Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"openanalytics-sdk/{__version__}",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except (HTTPError, URLError, OSError, ValueError) as exc:
            # Analytics must never take your app down with it.
            print(f"[openanalytics] send failed: {exc}", flush=True)

    def __enter__(self) -> "OpenAnalytics":
        return self

    def __exit__(self, *exc: object) -> None:
        self.flush()


class AsyncOpenAnalytics(OpenAnalytics):
    """Async client. Requires ``httpx``.

    Usage::

        async with AsyncOpenAnalytics("https://analytics.example.com") as oa:
            await oa.pageview("https://shop.example.com/pricing")
            await oa.track("signup", {"plan": "pro"})
    """

    async def pageview(self, url, referrer=None, visitor=None, timestamp=None):
        return await self._ahit("pageview", None, url, referrer, visitor, timestamp)

    async def track(self, event, data=None, url=None, referrer=None,
                    visitor=None, timestamp=None):
        if not event or not isinstance(event, str):
            raise ValueError("event must be a non-empty string")
        return await self._ahit(event, data, url or "/", referrer, visitor, timestamp)

    async def _ahit(self, event, data, url, referrer, visitor, timestamp):
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "AsyncOpenAnalytics requires httpx: pip install httpx"
            ) from exc

        hit = {
            "event": event,
            "data": data,
            "url": url,
            "referrer": referrer,
            "timestamp": int((timestamp or time.time()) * 1000),
        }
        if visitor:
            hit["visitor"] = visitor

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    self.base_url + self.endpoint,
                    json=hit,
                    headers={"User-Agent": f"openanalytics-sdk/{__version__}"},
                )
                r.raise_for_status()
        except Exception as exc:
            print(f"[openanalytics] send failed: {exc}", flush=True)
        return hit

    async def __aenter__(self) -> "AsyncOpenAnalytics":
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass
