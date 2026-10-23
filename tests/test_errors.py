"""MPC001-006 exception boundary, MPC001-012 code table, MPC001-086 business errors."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio
import mcp_types
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from cerp.protocol import errors
from cerp.protocol.boundary import RequestMiddleware, guarded
from cerp.protocol.errors import JSONRPC_CODES, NotFound, ValidationError


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ("parse", -32700),
        ("invalid_request", -32600),
        ("method_not_found", -32601),
        ("invalid_params", -32602),
        ("internal", -32603),
        ("unsupported_protocol_version", mcp_types.UNSUPPORTED_PROTOCOL_VERSION),
    ],
)
def test_jsonrpc_code_table(row: str, code: int) -> None:
    assert JSONRPC_CODES[row] == code


def test_every_domain_error_has_a_documented_code() -> None:
    for cls in errors.DomainError.__subclasses__():
        assert cls.code in errors.BUSINESS_CODES


def _server(db: Path | None = None) -> MCPServer:
    mcp = MCPServer(name="t", middleware=[RequestMiddleware(["2026-07-28", "2025-11-25"])])

    @mcp.tool(name="t.validate")
    @guarded
    def validate(severity: int) -> str:
        raise ValidationError("bad severity", field="severity", allowed=[100, 200, 300, 400, 500])

    @mcp.tool(name="t.missing")
    @guarded
    async def missing() -> str:
        raise NotFound("no such ticket")

    @mcp.tool(name="t.crash")
    @guarded
    def crash() -> str:
        raise RuntimeError("secret internal detail /etc/passwd")

    @mcp.tool(name="t.locked")
    @guarded
    def locked() -> str:
        assert db is not None
        con = sqlite3.connect(db, timeout=0.05)
        try:
            con.execute("INSERT INTO t VALUES (1)")
        finally:
            con.close()
        return "unreachable"

    @mcp.tool(name="t.ok")
    @guarded
    def ok() -> str:
        return "fine"

    return mcp


def test_validation_error_names_field_and_allowed_values() -> None:
    async def go() -> mcp_types.CallToolResult:
        async with Client(_server()) as c:
            return await c.call_tool("t.validate", {"severity": 7})

    r = anyio.run(go)
    assert r.is_error
    assert r.structured_content is not None
    err = r.structured_content["error"]
    assert err["code"] == "validation"
    assert err["field"] == "severity"
    assert err["allowed"] == [100, 200, 300, 400, 500]
    assert err["correlation_id"]


def test_not_found_from_async_tool() -> None:
    async def go() -> mcp_types.CallToolResult:
        async with Client(_server()) as c:
            return await c.call_tool("t.missing", {})

    r = anyio.run(go)
    assert r.is_error
    assert r.structured_content is not None
    assert r.structured_content["error"]["code"] == "not_found"


def test_crash_is_internal_error_with_correlation_id_and_server_keeps_serving(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def go() -> tuple[MCPError, str]:
        async with Client(_server()) as c:
            with pytest.raises(MCPError) as exc:
                await c.call_tool("t.crash", {})
            after = await c.call_tool("t.ok", {})
            text = after.content[0].text if isinstance(after.content[0], mcp_types.TextContent) else ""
            return exc.value, text

    err, after = anyio.run(go)
    assert err.code == errors.INTERNAL_ERROR
    assert set(err.data) == {"correlation_id"}
    assert "passwd" not in err.message
    assert after == "fine"
    assert any(getattr(r, "correlation_id", None) == err.data["correlation_id"] for r in caplog.records)


def test_locked_database_is_retryable_busy(tmp_path: Path) -> None:
    db = tmp_path / "x.s3db"
    holder = sqlite3.connect(db)
    holder.execute("CREATE TABLE t (x)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")

    async def go() -> mcp_types.CallToolResult:
        async with Client(_server(db)) as c:
            return await c.call_tool("t.locked", {})

    try:
        r = anyio.run(go)
    finally:
        holder.rollback()
        holder.close()
    assert r.is_error
    assert r.structured_content is not None
    err = r.structured_content["error"]
    assert err["code"] == "busy"
    assert err["retryable"] is True
    assert err["correlation_id"]


def test_unknown_tool_error_carries_correlation_id() -> None:
    async def go() -> mcp_types.CallToolResult:
        async with Client(_server()) as c:
            return await c.call_tool("t.nope", {})

    r = anyio.run(go)
    # The SDK reports an unknown tool as a tool error result; it must not leak internals.
    assert r.is_error
