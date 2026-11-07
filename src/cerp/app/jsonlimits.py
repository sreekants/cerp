"""Strict, bounded JSON for the MCP endpoint (MPC001-010).

ASGI middleware in front of the SDK. For POSTs to the MCP path it reads the whole body (up to the size limit)
and rejects, before the SDK sees it:

* bodies over the limit (default 1 MB): HTTP 413, JSON-RPC ``-32600``;
* invalid UTF-8, malformed JSON, ``NaN``/``Infinity``, duplicate object keys: HTTP 400, ``-32700``;
* nesting deeper than 64 or strings longer than 1 MB: HTTP 400, ``-32600``.

Depth is checked by a linear scan before parsing, so a 10,000-deep array is refused in microseconds and
``RecursionError`` can never escape. Accepted bodies are replayed to the SDK unchanged. Errors carry a
correlation id and no detail about the input.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cerp.protocol import correlation
from cerp.protocol.errors import INVALID_REQUEST, PARSE_ERROR

log = logging.getLogger("cerp.app")

MAX_BODY = 1024 * 1024
MAX_DEPTH = 64
MAX_STRING = 1024 * 1024
_QUOTE, _BACKSLASH = 0x22, 0x5C
_OPEN, _CLOSE = (0x5B, 0x7B), (0x5D, 0x7D)  # [ {   ] }


class JsonRejected(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _depth(raw: bytes, limit: int) -> None:
    depth = 0
    in_str = False
    escaped = False
    for b in raw:
        if in_str:
            if escaped:
                escaped = False
            elif b == _BACKSLASH:
                escaped = True
            elif b == _QUOTE:
                in_str = False
        elif b == _QUOTE:
            in_str = True
        elif b in _OPEN:
            depth += 1
            if depth > limit:
                raise JsonRejected(INVALID_REQUEST, "nesting too deep")
        elif b in _CLOSE:
            depth -= 1


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise JsonRejected(PARSE_ERROR, "duplicate key")
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise JsonRejected(PARSE_ERROR, f"{name} is not JSON")


def _long_string(value: Any, limit: int) -> bool:
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            if len(v) > limit:
                return True
        elif isinstance(v, dict):
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
    return False


def check_body(raw: bytes, *, max_depth: int = MAX_DEPTH, max_string: int = MAX_STRING) -> Any:
    """Parse ``raw`` strictly or raise :class:`JsonRejected`. Exposed for tests."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise JsonRejected(PARSE_ERROR, "invalid UTF-8") from None
    _depth(raw, max_depth)
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except JsonRejected:
        raise
    except (ValueError, RecursionError):
        raise JsonRejected(PARSE_ERROR, "malformed JSON") from None
    if _long_string(value, max_string):
        raise JsonRejected(INVALID_REQUEST, "string too long")
    return value


def _error_body(code: int) -> bytes:
    cid = correlation.new_correlation_id()
    msg = "Parse error" if code == PARSE_ERROR else "Invalid request"
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": code, "message": msg, "data": {"correlation_id": cid}},
        }
    ).encode()


class JsonLimits:
    def __init__(self, app: ASGIApp, *, path: str, max_body: int = MAX_BODY) -> None:
        self.app = app
        self.path = path
        self.max_body = max_body

    async def _reply(self, send: Send, status: int, code: int, reason: str) -> None:
        body = _error_body(code)
        log.info("request body refused: %s", reason)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                await self._reply(send, 413, INVALID_REQUEST, "body too large")
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        raw = b"".join(chunks)
        try:
            check_body(raw)
        except JsonRejected as exc:
            await self._reply(send, 400, exc.code, exc.reason)
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
