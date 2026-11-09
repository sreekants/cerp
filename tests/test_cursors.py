"""MPC001-023 signed cursors: bound to principal and query, expiring, tamper-evident."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.shared.exceptions import MCPError

from cerp.protocol.cursors import CursorSigner, query_digest
from cerp.protocol.errors import INVALID_PARAMS


def _signer(now: list[float]) -> CursorSigner:
    return CursorSigner(b"k" * 32, ttl=900, clock=lambda: now[0])


def _rejected(signer: CursorSigner, cursor: str, principal: str = "p", query: str = "q") -> None:
    with pytest.raises(MCPError) as exc:
        signer.decode(cursor, principal=principal, query=query)
    assert exc.value.error.code == INVALID_PARAMS
    assert set(exc.value.error.data) == {"correlation_id"}


def test_round_trip() -> None:
    s = _signer([1000.0])
    assert s.decode(s.encode(principal="p", query="q", offset=50), principal="p", query="q") == 50


def test_other_principal_or_query_is_rejected() -> None:
    s = _signer([1000.0])
    c = s.encode(principal="p", query="q", offset=25)
    _rejected(s, c, principal="other")
    _rejected(s, c, query="other")


def test_expired_cursor_is_rejected() -> None:
    now = [1000.0]
    s = _signer(now)
    c = s.encode(principal="p", query="q", offset=25)
    now[0] += 901
    _rejected(s, c)


def test_every_bit_flip_is_rejected() -> None:
    s = _signer([1000.0])
    c = s.encode(principal="p", query="q", offset=25)
    for i in range(len(c)):
        flipped = c[:i] + ("A" if c[i] != "A" else "B") + c[i + 1 :]
        _rejected(s, flipped)


def test_another_servers_cursor_is_rejected() -> None:
    c = CursorSigner(b"a" * 32).encode(principal="p", query="q", offset=1)
    _rejected(CursorSigner(b"b" * 32), c)


def test_query_digest_ignores_cursor_and_page_size_only() -> None:
    a = query_digest("t", {"text": "x", "limit": 10, "cursor": "abc"})
    assert a == query_digest("t", {"text": "x", "limit": 50})
    assert a != query_digest("t", {"text": "y"})
    assert a != query_digest("u", {"text": "x"})


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=600))
def test_garbage_is_always_invalid_params(garbage: str) -> None:
    _rejected(_signer([1000.0]), garbage)
