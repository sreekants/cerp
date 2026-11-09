"""The exception boundary around tools and requests (MPC001-006, MPC001-012, MPC001-086).

* :func:`guarded` wraps every tool function. Domain errors become ``isError: true`` results with structured
  content; a locked database becomes a retryable ``busy`` error; anything else becomes JSON-RPC ``-32603``
  carrying only a correlation id, with the traceback logged on the server.
* :class:`RequestMiddleware` runs around every inbound request: it binds a correlation id, rejects protocol
  revisions the deployment has not enabled (MPC001-013), adds the correlation id to every JSON-RPC error's
  ``data``, and logs one line per request without arguments or results (MPC001-111).

Nothing here patches the SDK (MPC001-161); both pieces use the SDK's public extension points.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Any, cast

import mcp_types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from cerp.protocol import correlation
from cerp.protocol.audit import AuditSink, build_record, close_call, log_sink, open_call
from cerp.protocol.errors import INTERNAL_ERROR, Busy, DomainError
from cerp.protocol.killswitch import NO_SWITCHES, KillSwitch

log = logging.getLogger("cerp.protocol")

_BUSY_MARKERS = ("database is locked", "database table is locked", "database is busy")


def is_busy(exc: BaseException) -> bool:
    """True for SQLite lock/busy errors, which are retryable rather than internal (MPC001-006)."""
    return isinstance(exc, sqlite3.OperationalError) and any(m in str(exc).lower() for m in _BUSY_MARKERS)


def domain_result(err: DomainError) -> CallToolResult:
    cid = correlation.ensure()
    payload = err.to_payload(cid)
    text = f"{err.code}: {err.message}"
    if err.field:
        text += f" (field: {err.field})"
    if err.allowed is not None:
        text += f"; allowed: {', '.join(map(str, err.allowed))}"
    text += f" [correlation id {cid}]"
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=payload, is_error=True
    )


def internal_error(exc: BaseException, where: str) -> MCPError:
    cid = correlation.ensure()
    log.error("internal error in %s", where, exc_info=exc, extra={"correlation_id": cid})
    return MCPError(INTERNAL_ERROR, "Internal error", {"correlation_id": cid})


def _translate(exc: Exception, where: str) -> CallToolResult:
    if isinstance(exc, DomainError):
        log.info("%s refused: %s", where, exc.code)
        return domain_result(exc)
    if is_busy(exc):
        log.warning("%s: database busy", where)
        return domain_result(Busy("the database is busy; retry shortly"))
    raise internal_error(exc, where) from None


def guarded[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap a tool function in the exception boundary. Keeps the signature the SDK introspects."""
    where = getattr(fn, "__name__", "tool")

    if inspect.iscoroutinefunction(fn):
        afn = cast(Callable[P, Awaitable[Any]], fn)

        @functools.wraps(fn)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
            try:
                return await afn(*args, **kwargs)
            except MCPError:
                raise
            except Exception as exc:
                return _translate(exc, where)

        return cast(Callable[P, R], async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except MCPError:
            raise
        except Exception as exc:
            return _translate(exc, where)

    return cast(Callable[P, R], wrapper)


def _with_correlation(err: MCPError, cid: str) -> MCPError:
    data = err.data
    if isinstance(data, dict) and "correlation_id" in data:
        return err
    if isinstance(data, dict):
        merged: dict[str, Any] = {**cast(dict[str, Any], data), "correlation_id": cid}
    elif data is None:
        merged = {"correlation_id": cid}
    else:
        merged = {"detail": data, "correlation_id": cid}
    return MCPError(err.code, err.message, merged)


def _with_tool_correlation(result: dict[str, Any], cid: str) -> dict[str, Any]:
    """Tool errors the SDK makes itself (e.g. unknown tool) carry no correlation id; add one (MPC001-012)."""
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        err = sc.get("error")
        if isinstance(err, dict) and err.get("correlation_id"):
            return result
        if isinstance(err, dict):
            return {**result, "structuredContent": {**sc, "error": {**err, "correlation_id": cid}}}
        return {**result, "structuredContent": {**sc, "error": {"code": "tool_error", "correlation_id": cid}}}
    return {**result, "structuredContent": {"error": {"code": "tool_error", "correlation_id": cid}}}


def _traceparent(ctx: ServerRequestContext[Any, Any]) -> str | None:
    """``traceparent`` from the request's ``_meta`` or, failing that, its HTTP headers (MPC001-112)."""
    meta = ctx.params.get("_meta") if isinstance(ctx.params, dict) else None
    if isinstance(meta, dict) and isinstance(meta.get("traceparent"), str):
        return str(meta["traceparent"])
    headers = getattr(ctx.request, "headers", None)
    if headers is not None:
        value = headers.get("traceparent")
        return value if isinstance(value, str) else None
    return None


class RequestMiddleware:
    """SDK server middleware: correlation and trace ids, version gate, kill switches, error decoration
    and one log line per request."""

    def __init__(
        self,
        protocol_versions: Collection[str],
        killswitch: KillSwitch = NO_SWITCHES,
        audit: AuditSink | None = log_sink,
    ) -> None:
        self.protocol_versions = tuple(protocol_versions)
        self.killswitch = killswitch
        self.audit = audit

    def _requested_version(self, ctx: ServerRequestContext[Any, Any]) -> str | None:
        if ctx.method == "initialize" and ctx.params is not None:
            v = ctx.params.get("protocolVersion")
            return v if isinstance(v, str) else None
        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
            return ctx.protocol_version
        return None  # handshake era: checked once, at initialize

    def _audit(
        self, ctx: ServerRequestContext[Any, Any], cid: str, outcome: str, result: Any, ms: float
    ) -> None:
        assert self.audit is not None  # noqa: S101 - checked by the caller
        try:
            self.audit(
                build_record(
                    correlation_id=cid,
                    trace_id=correlation.current_trace(),
                    params=ctx.params,
                    session=ctx.session,
                    outcome=outcome,
                    result=result,
                    latency_ms=ms,
                )
            )
        except Exception:
            log.exception("audit record could not be written")

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        cid = correlation.new_correlation_id()
        token = correlation.bind(cid)
        trace_token = correlation.bind_trace(correlation.parse_traceparent(_traceparent(ctx)))
        call_token = open_call()
        started = time.perf_counter()
        name = ctx.params.get("name") if ctx.params and ctx.method == "tools/call" else None
        outcome = "ok"
        result: HandlerResult = None
        try:
            requested = self._requested_version(ctx)
            if requested is not None and requested not in self.protocol_versions:
                outcome = "unsupported_version"
                raise MCPError(  # noqa: TRY301 - raised inside so the except clauses decorate and log it
                    mcp_types.UNSUPPORTED_PROTOCOL_VERSION,
                    "Unsupported protocol version",
                    {"supported": list(self.protocol_versions), "requested": requested},
                )
            result = await call_next(ctx)
        except MCPError as err:
            if outcome == "ok":
                outcome = f"error {err.code}"
            raise _with_correlation(err, cid) from None
        except Exception as exc:
            outcome = "internal"
            raise internal_error(exc, ctx.method) from None
        else:
            if isinstance(result, dict) and result.get("isError"):
                outcome = "tool_error"
                result = _with_tool_correlation(result, cid)
            if (
                ctx.method == "tools/list"
                and isinstance(result, dict)
                and isinstance(result.get("tools"), list)
            ):
                result = {**result, "tools": self.killswitch.filter_tools(result["tools"])}
            return result
        finally:
            if ctx.method == "tools/call" and self.audit is not None:
                self._audit(ctx, cid, outcome, result, (time.perf_counter() - started) * 1000)
            if ctx.request_id is not None:
                log.info(
                    "%s%s %s %.1fms",
                    ctx.method,
                    f" {name}" if isinstance(name, str) else "",
                    outcome,
                    (time.perf_counter() - started) * 1000,
                )
            close_call(call_token)
            correlation.reset_trace(trace_token)
            correlation.reset(token)
