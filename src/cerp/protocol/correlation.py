"""Correlation ids: one per inbound request, carried in logs and in every error (MPC001-012, MPC001-111)."""

from __future__ import annotations

import secrets
from contextvars import ContextVar

_current: ContextVar[str | None] = ContextVar("cerp_correlation_id", default=None)


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
    _current.reset(token)  # type: ignore[arg-type]
