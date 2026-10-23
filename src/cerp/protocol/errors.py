"""Error model: domain errors and the one table that maps failures to the wire (MPC001-006, -012, -086).

Two kinds of failure reach a client:

* **Business failures** (a bad field value, a missing record, a rule the server enforces) are returned as a
  successful ``tools/call`` result with ``isError: true`` and structured content
  ``{"error": {"code", "message", "field", "allowed", "retryable", "correlation_id"}}`` so the model can
  correct itself. They are raised in tool code as one of the :class:`DomainError` subclasses below.
* **Protocol and internal failures** are JSON-RPC errors. Internal errors carry only a correlation id; the
  details stay in the server log (MPC001-067).

:data:`JSONRPC_CODES` is the documented table; ``tests/test_errors.py`` asserts every row.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Final

import mcp_types

# ---------------------------------------------------------------- JSON-RPC table (MPC001-012)

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

JSONRPC_CODES: Final[dict[str, int]] = {
    "parse": PARSE_ERROR,
    "invalid_request": INVALID_REQUEST,
    "method_not_found": METHOD_NOT_FOUND,
    "invalid_params": INVALID_PARAMS,  # also: unknown resource / missing argument
    "internal": INTERNAL_ERROR,
    "unsupported_protocol_version": mcp_types.UNSUPPORTED_PROTOCOL_VERSION,
}

# ---------------------------------------------------------------- business error codes (MPC001-086)

BUSINESS_CODES: Final = ("validation", "conflict", "not_permitted", "not_found", "rule", "disabled", "busy")


class DomainError(Exception):
    """A failure the caller can act on. Returned as ``isError: true``, never as a JSON-RPC error."""

    code: ClassVar[str] = "rule"
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        allowed: Sequence[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.allowed = list(allowed) if allowed is not None else None

    def to_payload(self, correlation_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": correlation_id,
        }
        if self.field is not None:
            body["field"] = self.field
        if self.allowed is not None:
            body["allowed"] = self.allowed
        return {"error": body}


class ValidationError(DomainError):
    """An argument is malformed or out of range. ``field`` names it; ``allowed`` lists valid values."""

    code = "validation"


class ConflictError(DomainError):
    """The record changed since it was read (stale concurrency token) or would duplicate another."""

    code = "conflict"


class NotPermitted(DomainError):
    """The caller may not do this. Never used for reads: an unreadable record is :class:`NotFound`."""

    code = "not_permitted"


class NotFound(DomainError):
    """No such record, or one the caller cannot read (the two are indistinguishable, MPC001-087)."""

    code = "not_found"


class RuleViolation(DomainError):
    """A business rule the server enforces (workflow, non-negative stock, balanced journal)."""

    code = "rule"


class Disabled(DomainError):
    """The tool or model is switched off by configuration or a kill switch."""

    code = "disabled"


class Busy(DomainError):
    """The database is locked by another writer (usually the web UI). Safe to retry shortly."""

    code = "busy"
    retryable = True
