"""One audit record per ``tools/call`` (MPC001-110).

The request middleware builds an :class:`AuditRecord` for every ``tools/call`` and hands it to the
configured sink. The default sink writes one JSON line per record to the ``cerp.audit`` logger, which
:func:`cerp.app.logs.configure_audit` routes to its own file (never mixed with the diagnostic log).

A record holds: UTC time, correlation and trace ids, principal (token subject and client id), webos user,
client name and version, tool, SHA-256 of the canonical arguments, outcome, affected handles and latency.
Arguments themselves are never recorded. Write-tool rows in webos's ``log.s3db`` (requirements 2 and 3)
come with the write tools.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token

audit_log = logging.getLogger("cerp.audit")
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"

# The webos user a call was mapped to (MPC001-060). A mutable holder, so a tool running in a worker
# thread can fill it in for the middleware that opened it.
_MAPPED_USER: ContextVar[list[str] | None] = ContextVar("cerp_mapped_user", default=None)


def open_call() -> Token[list[str] | None]:
    """Start recording the mapped webos user for one request; pass the result to :func:`close_call`."""
    return _MAPPED_USER.set([])


def close_call(token: Token[list[str] | None]) -> None:
    _MAPPED_USER.reset(token)


def note_webos_user(name: str) -> None:
    """Record the webos user the current call acts as (called by principal mapping)."""
    holder = _MAPPED_USER.get()
    if holder is not None:
        holder[:] = [name]


@dataclass(frozen=True)
class AuditRecord:
    ts: str
    correlation_id: str
    trace_id: str | None
    principal: str | None
    client_id: str | None
    webos_user: str | None
    client_name: str | None
    client_version: str | None
    tool: str
    args_sha256: str
    outcome: str
    handles: list[str] = field(default_factory=list)
    latency_ms: float = 0.0


AuditSink = Callable[[AuditRecord], None]


def log_sink(record: AuditRecord) -> None:
    audit_log.info(json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":")))


def args_digest(arguments: Any) -> str:
    """SHA-256 of the canonical JSON form (sorted keys, no spaces, UTF-8) of the tool arguments."""
    canonical = json.dumps(
        arguments if arguments is not None else {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def client_info(params: Mapping[str, Any] | None, session: Any) -> tuple[str | None, str | None]:
    meta = params.get("_meta") if params else None
    info: Any = meta.get(CLIENT_INFO_KEY) if isinstance(meta, dict) else None
    if info is None:
        cp = getattr(session, "client_params", None)
        info = getattr(cp, "client_info", None) or getattr(cp, "clientInfo", None)
    if isinstance(info, dict):
        return info.get("name"), info.get("version")
    if info is not None:
        return getattr(info, "name", None), getattr(info, "version", None)
    return None, None


def handles_in(result: Any) -> list[str]:
    """Record handles a tool reported as affected (``structuredContent.handles``), if any."""
    if isinstance(result, dict):
        sc = result.get("structuredContent")
        if isinstance(sc, dict) and isinstance(sc.get("handles"), list):
            return [str(h) for h in sc["handles"]][:100]
    return []


def build_record(
    *,
    correlation_id: str,
    trace_id: str | None,
    params: Mapping[str, Any] | None,
    session: Any,
    outcome: str,
    result: Any,
    latency_ms: float,
) -> AuditRecord:
    token = get_access_token()
    name, version = client_info(params, session)
    mapped = _MAPPED_USER.get()
    return AuditRecord(
        ts=dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
        correlation_id=correlation_id,
        trace_id=trace_id,
        principal=token.subject if token else None,
        client_id=token.client_id if token else None,
        # the mapped webos user (MPC001-060) when a tool resolved one, else the token subject
        webos_user=mapped[0] if mapped else (token.subject if token else None),
        client_name=name,
        client_version=version,
        tool=str((params or {}).get("name", "")),
        args_sha256=args_digest((params or {}).get("arguments")),
        outcome=outcome,
        handles=handles_in(result),
        latency_ms=round(latency_ms, 1),
    )
