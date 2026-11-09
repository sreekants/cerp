"""Opaque, signed pagination cursors (MPC001-023).

A cursor carries the next row offset of one query. It is authenticated with HMAC-SHA256, bound to the
principal and to a digest of the tool name and its filter arguments, and expires (default 15 minutes).
A tampered, expired, foreign or malformed cursor is a protocol error, ``-32602``.

Key: until key management (MPC001-056) supplies a server key, each process generates a random key at
start, kept only in memory. Cursors therefore do not survive a restart; a client simply searches again.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from mcp.shared.exceptions import MCPError

from cerp.protocol.correlation import ensure
from cerp.protocol.errors import INVALID_PARAMS

MAX_CURSOR_CHARS: Final = 512
_SIG_BYTES: Final = 16


_NOT_QUERY: Final = frozenset({"cursor", "limit"})  # the page size may change between pages


def query_digest(tool: str, arguments: Mapping[str, Any]) -> str:
    """A stable digest of a tool name and the arguments that select rows (not cursor or page size)."""
    blob = json.dumps(
        {"tool": tool, "args": {k: v for k, v in sorted(arguments.items()) if k not in _NOT_QUERY}},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CursorSigner:
    def __init__(
        self, key: bytes | None = None, *, ttl: float = 900.0, clock: Callable[[], float] = time.time
    ) -> None:
        self._key = key or secrets.token_bytes(32)
        self._ttl = ttl
        self._clock = clock

    def _sign(self, body: bytes) -> bytes:
        return hmac.new(self._key, body, hashlib.sha256).digest()[:_SIG_BYTES]

    def encode(self, *, principal: str, query: str, offset: int) -> str:
        body = json.dumps(
            {"p": principal, "q": query, "o": offset, "e": int(self._clock() + self._ttl)},
            separators=(",", ":"),
        ).encode()
        return f"{_b64(body)}.{_b64(self._sign(body))}"

    def _read(self, cursor: str, principal: str, query: str) -> int:
        """The offset in a valid cursor; ValueError (or a decoding error) for anything else."""
        if len(cursor) > MAX_CURSOR_CHARS:
            raise ValueError("too long")
        body_text, sig_text = cursor.split(".", 1)
        body, sig = _unb64(body_text), _unb64(sig_text)
        if _b64(body) != body_text or _b64(sig) != sig_text:
            raise ValueError("not canonical")  # unused base64 bits would let one cursor have many spellings
        if not hmac.compare_digest(self._sign(body), sig):
            raise ValueError("bad signature")
        data = json.loads(body)
        offset = data["o"]
        valid = (
            isinstance(offset, int)
            and not isinstance(offset, bool)
            and offset >= 0
            and data["p"] == principal
            and data["q"] == query
            and int(data["e"]) >= self._clock()
        )
        if not valid:
            raise ValueError("foreign, expired or malformed cursor")
        return int(offset)

    def decode(self, cursor: str, *, principal: str, query: str) -> int:
        """The offset a cursor stands for, or raise ``-32602``."""
        try:
            return self._read(cursor, principal, query)
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):  # JSONDecodeError is a ValueError
            raise MCPError(
                INVALID_PARAMS,
                "Invalid cursor: it is malformed, expired or belongs to another query; search again",
                {"correlation_id": ensure()},
            ) from None
