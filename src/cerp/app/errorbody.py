"""Correlation ids on errors the SDK transport returns before dispatch (MPC001-012).

The SDK answers some malformed requests itself (e.g. a routing-header mismatch, -32020, HTTP 400) before
cerp's request middleware runs, so those errors carry no correlation id. :class:`CorrelateErrors` buffers
JSON error responses (status 400 and above) from the MCP endpoint, adds ``error.data.correlation_id`` when
it is missing, and logs the code with that id, so every error a client sees can be found in the log.
Successful and streamed responses pass through untouched.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cerp.protocol import correlation

log = logging.getLogger("cerp.app")
_ERROR_STATUS = 400


def add_correlation(body: bytes) -> tuple[bytes, str | None, Any]:
    """The body with a correlation id added if it is a JSON-RPC error lacking one; the id; the code."""
    try:
        doc = json.loads(body)
    except ValueError:
        return body, None, None
    err = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(err, dict):
        return body, None, None
    data = err.get("data")
    if isinstance(data, dict) and data.get("correlation_id"):
        return body, str(data["correlation_id"]), err.get("code")
    cid = correlation.new_correlation_id()
    if isinstance(data, dict):
        err["data"] = {**data, "correlation_id": cid}
    elif data is None:
        err["data"] = {"correlation_id": cid}
    else:
        err["data"] = {"detail": data, "correlation_id": cid}
    return json.dumps(doc).encode(), cid, err.get("code")


class CorrelateErrors:
    def __init__(self, app: ASGIApp, *, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != self.path:
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        chunks: list[bytes] = []

        async def wrapped(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers", [])).get(b"content-type", b"")
                is_json = ctype.startswith(b"application/json")
                if message.get("status", 200) >= _ERROR_STATUS and is_json:
                    start = message  # hold until the whole body is here
                    return
                await send(message)
                return
            if start is None or message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body, cid, code = add_correlation(b"".join(chunks))
            if cid is not None:
                log.info("transport error %s (HTTP %s) [%s]", code, start.get("status"), cid)
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
