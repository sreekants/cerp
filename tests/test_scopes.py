"""MPC001-077 scopes, MPC001-036 protected-resource metadata, MPC001-058 no client token leaks."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp_types import ToolAnnotations
from starlette.testclient import TestClient

from cerp.app.asgi import build_app
from cerp.app.config import Settings
from cerp.app.logs import configure_audit, configure_logging
from cerp.data.registry import load_registry
from cerp.data.scopes import catalogue
from cerp.protocol.scopes import ScopePolicy
from cerp.protocol.tooling import register_tool
from tests.conftest import TICKET_EXPOSURE
from tests.test_oauth import AUDIENCE, ISSUER, JWKS_URL, Idp, idp  # noqa: F401 - fixture
from tests.test_protocol import ACCEPT, MODERN, _body, _meta

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


def _settings(registry_file: Path, exposure: Path, dbroot: Path) -> Settings:
    return Settings.model_validate(
        {
            "registry": {"path": str(registry_file)},
            "exposure": {"path": str(exposure)},
            "data": {"dbroot": str(dbroot)},
            "auth": {"oauth": {"enabled": True, "issuer": ISSUER, "jwks_url": JWKS_URL}},
        }
    )


def _call(c: TestClient, tool: str, token: str) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}, "_meta": _meta()},
    }
    hdr = {
        **ACCEPT,
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
        "Authorization": f"Bearer {token}",
    }
    r = c.post("/mcp", json=body, headers=hdr)
    assert r.status_code == 200, r.text
    return _body(r)["result"]


def test_catalogue_from_registry(registry_file: Path) -> None:
    names = [s.name for s in catalogue(load_registry(registry_file))]
    assert "webos.im.read" in names
    assert "webos.im.write" in names
    assert not any(n.endswith(".delete") for n in names)
    assert names == sorted(names)


@pytest.fixture
def app_and_mcp(
    idp: Idp,  # noqa: F811 - pytest fixture
    registry_file: Path,
    write_yaml: Callable[[str, Any], Path],
    dbroot: Path,
) -> tuple[Any, Any]:
    app, mcp, _ = build_app(_settings(registry_file, write_yaml("e.yaml", TICKET_EXPOSURE), dbroot), {})

    def write_something() -> dict[str, str]:
        return {"ok": "yes"}

    register_tool(
        mcp, write_something, name="im.write_something", title="Write", annotations=WRITE,
        scopes=ScopePolicy(enforce=True),
    )  # fmt: skip
    return app, mcp


def test_tools_need_the_module_scope(app_and_mcp: tuple[Any, Any], idp: Idp) -> None:  # noqa: F811
    app, _ = app_and_mcp
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        no_scope = _call(c, "im.describe_ticket", idp.token())
        assert no_scope["isError"]
        assert no_scope["structuredContent"]["error"]["code"] == "not_permitted"
        assert no_scope["structuredContent"]["error"]["allowed"] == ["webos.im.read"]
        assert not _call(c, "im.describe_ticket", idp.token(scope="webos.im.read")).get("isError")
        read_only = _call(c, "im.write_something", idp.token(scope="webos.im.read"))
        assert read_only["isError"]
        assert not _call(c, "im.write_something", idp.token(scope="webos.im.write")).get("isError")


def test_protected_resource_metadata(app_and_mcp: tuple[Any, Any], idp: Idp) -> None:  # noqa: F811
    app, _ = app_and_mcp
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            meta = c.get(path).json()
            assert meta["resource"] == AUDIENCE
            assert meta["authorization_servers"] == [ISSUER] or meta["authorization_servers"] == [
                ISSUER + "/"
            ]
            assert "webos.im.read" in meta["scopes_supported"]
            assert meta["bearer_methods_supported"] == ["header"]
        r = c.post("/mcp", json={}, headers=ACCEPT)
        assert r.status_code == 401
        assert 'resource_metadata="' in r.headers["www-authenticate"]


def test_client_tokens_never_reach_logs_or_audit(
    app_and_mcp: tuple[Any, Any],
    idp: Idp,  # noqa: F811
    tmp_path: Path,
) -> None:
    app, _ = app_and_mcp
    buf = io.StringIO()
    configure_logging("DEBUG", None, stream=buf)
    audit = tmp_path / "audit.jsonl"
    configure_audit(audit)
    token = idp.token(scope="webos.im.read")
    try:
        with TestClient(app, base_url="http://127.0.0.1:7411") as c:
            _call(c, "im.describe_ticket", token)
            _call(c, "im.write_something", token)
            c.post("/mcp", json={}, headers={**ACCEPT, "Authorization": "Bearer not-a-valid-token-value"})
    finally:
        configure_audit(None)
        configure_logging("INFO", None)
        logging.getLogger().setLevel(logging.WARNING)
    text = buf.getvalue() + audit.read_text(encoding="utf-8")
    assert token not in text
    assert token.split(".")[2] not in text  # not even the signature
    assert "not-a-valid-token-value" not in text
    assert len([json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]) == 2
