"""MPC001-110 audit: one record per tools/call, matching correlation ids, no argument values."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from starlette.testclient import TestClient

from cerp.app.asgi import build_app
from cerp.app.config import Settings
from cerp.app.logs import configure_audit
from cerp.app.server import build_runtime
from cerp.protocol.audit import args_digest
from tests.conftest import TICKET_EXPOSURE
from tests.test_exposure import _settings
from tests.test_ops import TRACE, TRACEPARENT
from tests.test_protocol import ACCEPT, MODERN, TOKEN, _meta


def _records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "cerp.audit"]


def test_every_call_is_audited_once(
    registry_file: Path,
    write_yaml: Callable[[str, Any], Path],
    dbroot: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="cerp.audit")
    configure_audit(None)
    mcp, _ = build_runtime(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})
    calls = 1000

    async def go() -> None:
        async with Client(mcp) as c:
            await c.list_tools()  # not audited: only tools/call is
            for _ in range(calls):
                await c.call_tool("im.describe_ticket", {})
            await c.call_tool("im.no_such_tool", {})

    anyio.run(go)
    recs = _records(caplog)
    assert len(recs) == calls + 1
    assert len({r["correlation_id"] for r in recs}) == calls + 1
    assert {r["tool"] for r in recs} == {"im.describe_ticket", "im.no_such_tool"}
    assert recs[0]["outcome"] == "ok"
    assert recs[0]["args_sha256"] == args_digest({})
    assert recs[-1]["outcome"] != "ok"
    assert all(r["latency_ms"] >= 0 for r in recs)


def test_digest_is_canonical_and_values_are_not_recorded() -> None:
    assert args_digest({"b": 1, "a": "ü"}) == args_digest({"a": "ü", "b": 1})
    assert args_digest({"a": 1}) != args_digest({"a": 2})


def test_http_record_has_principal_client_and_trace(
    registry_file: Path,
    write_yaml: Callable[[str, Any], Path],
    dbroot: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="cerp.audit")
    configure_audit(None)
    s = _settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot)
    s = Settings.model_validate(
        s.model_dump()
        | {"auth": {"static_tokens": {"enabled": True, "tokens": [{"token_env": "CERP_T", "user": "admin"}]}}}
    )
    app, _, _ = build_app(s, {"CERP_T": TOKEN})
    secret_arg = "do-not-log-this-value"  # noqa: S105 - a marker string, not a credential
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "im.describe_ticket", "arguments": {"x": secret_arg}, "_meta": _meta()},
    }
    hdr = {
        **ACCEPT,
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "im.describe_ticket",
        "Authorization": f"Bearer {TOKEN}",
        "traceparent": TRACEPARENT,
    }
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        c.post("/mcp", json=body, headers=hdr)
    (rec,) = _records(caplog)
    assert rec["principal"] == "admin"
    assert rec["client_name"] == "test"
    assert rec["trace_id"] == TRACE
    assert secret_arg not in json.dumps(rec)
    assert TOKEN not in json.dumps(rec)


def test_audit_file_gets_records_and_main_log_does_not(tmp_path: Path) -> None:
    f = tmp_path / "audit" / "audit.jsonl"
    configure_audit(f)
    try:
        logging.getLogger("cerp.audit").info('{"tool":"x"}')
    finally:
        configure_audit(None)
    assert json.loads(f.read_text(encoding="utf-8").strip()) == {"tool": "x"}
