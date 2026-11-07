"""Correlation and trace ids: one per inbound request, carried in logs and errors (MPC001-012, -111, -112).

The trace id is taken from a W3C ``traceparent`` supplied by the client (in ``_meta`` or an HTTP header) so
a client's trace continues through cerp's logs.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar

_current: ContextVar[str | None] = ContextVar("cerp_correlation_id", default=None)
_trace: ContextVar[str | None] = ContextVar("cerp_trace_id", default=None)
_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def parse_traceparent(value: str | None) -> str | None:
    """The trace id of a valid W3C ``traceparent``, else None (all-zero ids are invalid)."""
    if not value:
        return None
    m = _TRACEPARENT.match(value.strip().lower())
    if not m or set(m.group(1)) == {"0"} or set(m.group(2)) == {"0"}:
        return None
    return m.group(1)


def current_trace() -> str | None:
    return _trace.get()


def bind_trace(trace_id: str | None) -> object:
    return _trace.set(trace_id)


def reset_trace(token: object) -> None:
    _trace.reset(token)  # type: ignore[arg-type]  # token is the Token returned by bind_trace()


def new_correlation_id() -> str:
    """A short random id, unique enough to find one request in the logs."""
    return secrets.token_hex(8)


def current() -> str | None:
    return _current.get()


def ensure() -> str:
    """The current request's id, creating one if code runs outside a request (e.g. a test)."""
    cid = _current.get()
    if cid is None:
        cid = new_correlation_id()
        _current.set(cid)
    return cid


def bind(cid: str) -> object:
    """Set the id for the current context; returns a token for :func:`reset`."""
    return _current.set(cid)


def reset(token: object) -> None:
    _current.reset(token)  # type: ignore[arg-type]  # token is the Token returned by bind()
