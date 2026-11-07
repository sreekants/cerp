"""Request limits in front of the MCP endpoint (MPC001-035).

For requests to the MCP path, :class:`RequestLimits` enforces, in this order:

* header size over ``limits.max_header_bytes`` (default 16 KB) → **431**;
* rate over ``rate_per_second`` with bursts of ``burst`` (token bucket) → **429** with ``Retry-After``;
* more than ``concurrent_per_principal`` requests in flight → **429** with ``Retry-After: 1``;
* a request running longer than ``request_timeout_s`` (default 30 s) → **504**.

(Body size → 413 is :class:`cerp.app.jsonlimits.JsonLimits`.) A principal is a bearer token, keyed by its
SHA-256 so no token is kept in memory; requests without one share a stricter bucket per client IP (when
authentication is off, each client IP gets the normal limits).
Every refusal has a JSON-RPC error body with a correlation id and is logged. Limits are read from the live
settings on every request, so a reload applies at once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cerp.app.config import LimitsSection
from cerp.protocol import correlation

log = logging.getLogger("cerp.app")

RATE_LIMITED = -32029  # server-defined JSON-RPC code for "slow down"
TIMED_OUT = -32028
TOO_LARGE = -32600
MAX_KEYS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float
    in_flight: int = 0


class _Buckets:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._b: dict[str, _Bucket] = {}

    def acquire(self, key: str, rate: float, burst: int, concurrent: int) -> float | None:
        """None if admitted (and counted in flight); otherwise seconds to wait before retrying."""
        now = self._clock()
        with self._lock:
            b = self._b.get(key)
            if b is None:
                if len(self._b) >= MAX_KEYS:  # forget idle principals rather than grow without bound
                    self._b = {k: v for k, v in self._b.items() if v.in_flight}
                b = self._b[key] = _Bucket(float(burst), now)
            b.tokens = min(float(burst), b.tokens + (now - b.updated) * rate)
            b.updated = now
            if b.in_flight >= concurrent:
                return 1.0
            if b.tokens < 1.0:
                return (1.0 - b.tokens) / rate
            b.tokens -= 1.0
            b.in_flight += 1
            return None

    def release(self, key: str) -> None:
        with self._lock:
            b = self._b.get(key)
            if b is not None and b.in_flight > 0:
                b.in_flight -= 1


def _principal(scope: Scope) -> tuple[str, bool]:
    for k, v in scope.get("headers", []):
        if k.lower() == b"authorization" and v[:7].lower() == b"bearer ":
            return "t:" + hashlib.sha256(v[7:].strip()).hexdigest(), True
    client = scope.get("client") or ("?", 0)
    return f"ip:{client[0]}", False


async def _refuse(send: Send, status: int, code: int, message: str, retry_after: float | None = None) -> None:
    cid = correlation.new_correlation_id()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": message, "data": {"correlation_id": cid}},
        }
    ).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if retry_after is not None:
        headers.append((b"retry-after", str(max(1, math.ceil(retry_after))).encode()))
    log.info("request refused: %d %s [%s]", status, message, cid)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequestLimits:
    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        limits: Callable[[], LimitsSection],
        auth_enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.auth_enabled = auth_enabled
        self.path = path
        self.limits = limits
        self.buckets = _Buckets(clock)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return
        lim = self.limits()
        if sum(len(k) + len(v) + 4 for k, v in scope.get("headers", [])) > lim.max_header_bytes:
            await _refuse(send, 431, TOO_LARGE, "Request headers too large")
            return
        key, authenticated = _principal(scope)
        if authenticated or not self.auth_enabled:  # without auth every client gets the normal limits
            wait = self.buckets.acquire(key, lim.rate_per_second, lim.burst, lim.concurrent_per_principal)
        else:
            wait = self.buckets.acquire(
                key, lim.anonymous_rate_per_second, lim.anonymous_burst, lim.anonymous_concurrent
            )
        if wait is not None:
            await _refuse(send, 429, RATE_LIMITED, "Too many requests", retry_after=wait)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            with anyio.fail_after(lim.request_timeout_s):
                await self.app(scope, receive, tracked)
        except TimeoutError:
            if not started:
                await _refuse(send, 504, TIMED_OUT, "Request timed out")
            else:
                log.warning(
                    "request exceeded %.0f s after the response started; stream closed", lim.request_timeout_s
                )
        finally:
            self.buckets.release(key)
