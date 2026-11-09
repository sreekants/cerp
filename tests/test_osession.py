"""webos sessions (OSESSION) as bearer tokens (MPC001-286, decision 0005).

Covers the session parser and seal (a port of ``Session::FromString``), the domain directory
(``nodedb.s3db`` and the ``<G0>/<G1>/<GUID>/db`` layout), the verifier and dispatcher, and end to end: an
OSESSION caller is mapped to the user of its own domain and every tool reads and writes that domain's
databases, not the default ones.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio
import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from starlette.testclient import TestClient

from cerp.app.asgi import build_app
from cerp.app.config import Settings
from cerp.app.server import build_runtime
from cerp.auth.osession import (
    OSESSION_CLIENT_ID,
    PREFIX,
    DispatchingVerifier,
    OSessionVerifier,
    parse,
    seal,
)
from cerp.data.domains import DomainDirectory, DomainUnavailable
from tests.mvp_tenant import NOW
from tests.test_protocol import ACCEPT, MODERN, _meta
from tests.test_readonly_tools import ALL_PACKS, mvp_registry, tenant

__all__ = ["mvp_registry", "tenant"]  # fixtures used by name

GUID_A = "4df72d07-b9ad-45e8-96a7-f45a9a1b86ec"  # stored lower-case, as in nodedb.s3db
GUID_B = "bffc4d78-0fb7-4fad-a6bc-834648643f93"
GUID_OFF = "0a0a0a0a-0000-4000-8000-000000000000"


def session(domain: str, user_id: int, guid: str, salt: str = "6F1C2B9E-1111-4222-8333-944455556666") -> str:
    text = f"{domain},{user_id},{salt}"
    raw = hashlib.md5((guid + text).encode(), usedforsecurity=False).digest()
    digest = "".join(f"{b:02x}"[::-1] for b in raw)  # webos prints each byte low nibble first
    return f"{text},{digest}"


def build_nodedb(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "nodedb.s3db")
    con.executescript(
        "CREATE TABLE domains (id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, name VARCHAR(64),"
        " guid VARCHAR(80), creation_time TIMESTAMP, disk_quota_MB INTEGER,"
        " disk_utilization_MB INTEGER, enabled INTEGER, notification TEXT, owner_email VARCHAR(80));"
    )
    con.executemany(
        "INSERT INTO domains (name, guid, enabled) VALUES (?, ?, ?)",
        [("exocortex", GUID_A, 1), ("in.bial", GUID_B, 1), ("closed", GUID_OFF, 0)],
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------- parser and seal


def test_parse_plain_and_url_encoded() -> None:
    raw = session("exocortex", 2, GUID_A)
    s = parse(raw)
    assert s is not None
    assert (s.domain, s.user_id) == ("exocortex", "2")
    assert parse(quote(raw, safe="")) == s  # cookies often arrive with commas as %2C
    assert seal(GUID_A, s) == s.digest


def test_seal_matches_webos_known_vector() -> None:
    # Worked by hand from empact's CMD5::GetDigest (stream PrintDigest: low nibble, then high nibble).
    # Plain MD5 hex of the same input is c20c77c058ccbc13a3a0b30ac01576bc; webos swaps each pair.
    s = parse("acme,7,0f0e0d0c-0b0a-0908-0706-050403020100,2cc0770c85cccb313a0a3ba00c5167cb")
    assert s is not None
    assert seal("00000000-1111-2222-3333-444444444444", s) == s.digest
    assert seal("00000000-1111-2222-3333-444444444444", s) != "c20c77c058ccbc13a3a0b30ac01576bc"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "exocortex,2,salt",  # three parts
        "exocortex,2,salt,abc,extra",
        "exocortex,0,salt," + "a" * 32,  # user id 0
        "exocortex,two,salt," + "a" * 32,
        "exocortex,2,," + "a" * 32,  # empty salt
        "exocortex,2,salt," + "g" * 32,  # not hex
        "exocortex,2,salt," + "a" * 31,
        "Exo Cortex,2,salt," + "a" * 32,  # not a domain name
        "exocortex,2," + "s" * 65 + "," + "a" * 32,
        "x" * 2000,
    ],
)
def test_parse_refuses_malformed(bad: str) -> None:
    assert parse(bad) is None


@given(st.text(max_size=300))
def test_parse_never_raises(text: str) -> None:
    parse(text)


# ---------------------------------------------------------------- domain directory


def test_domain_directory(tmp_path: Path) -> None:
    build_nodedb(tmp_path)
    d = DomainDirectory(tmp_path)
    assert d.guid("exocortex") == GUID_A
    assert d.guid("ExoCortex") == GUID_A  # webos lower-cases names
    assert d.guid("closed") is None  # disabled
    assert d.guid("nobody") is None
    assert d.guid("../etc") is None
    assert d.database_dir(GUID_A) == tmp_path / "4" / "D" / GUID_A.upper() / "db"
    with pytest.raises(DomainUnavailable):
        DomainDirectory(tmp_path / "missing").guid("exocortex")


def test_domain_directory_caches(tmp_path: Path) -> None:
    build_nodedb(tmp_path)
    now = [0.0]
    d = DomainDirectory(tmp_path, ttl=10, clock=lambda: now[0])
    assert d.guid("exocortex") == GUID_A
    con = sqlite3.connect(tmp_path / "nodedb.s3db")
    con.execute("UPDATE domains SET enabled = 0 WHERE name = 'exocortex'")
    con.commit()
    con.close()
    assert d.guid("exocortex") == GUID_A  # cached
    now[0] = 11
    assert d.guid("exocortex") is None  # disabling takes effect after the cache time


def test_domain_directory_loads_one_map(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_nodedb(tmp_path)
    d = DomainDirectory(tmp_path, ttl=10, clock=lambda: 0.0)
    loads = [0]
    real = d._load

    def counting(path: Path) -> dict[str, str]:
        loads[0] += 1
        return real(path)

    monkeypatch.setattr(d, "_load", counting)
    for _ in range(3):
        assert d.guid("exocortex") == GUID_A
        assert d.guid("nobody") is None  # unchanged file: a miss does not reload
    assert loads[0] == 1


def test_domain_directory_finds_new_domain_at_once(tmp_path: Path) -> None:
    build_nodedb(tmp_path)
    d = DomainDirectory(tmp_path, ttl=1000, clock=lambda: 0.0)
    assert d.guid("fresh") is None
    con = sqlite3.connect(tmp_path / "nodedb.s3db")
    con.execute(
        "INSERT INTO domains (name, guid, enabled) VALUES ('fresh', ?, 1)",
        ("0a1b2c3d-0000-4000-8000-000000000001",),
    )
    con.commit()
    con.close()
    assert d.guid("fresh") == "0a1b2c3d-0000-4000-8000-000000000001"  # the file changed: reloaded on the miss


# ---------------------------------------------------------------- verifier


class Fallback:
    async def verify_token(self, token: str) -> AccessToken | None:
        return AccessToken(token=token, client_id="other", scopes=[]) if token == "static-token" else None  # noqa: S105


def verify(verifier: Any, token: str) -> AccessToken | None:
    async def go() -> AccessToken | None:
        result: AccessToken | None = await verifier.verify_token(token)
        return result

    return anyio.run(go)


def test_verifier(tmp_path: Path) -> None:
    build_nodedb(tmp_path)
    v = OSessionVerifier(DomainDirectory(tmp_path))
    tok = verify(v, PREFIX + session("exocortex", 2, GUID_A))
    assert tok is not None
    assert tok.client_id == OSESSION_CLIENT_ID
    assert tok.subject == "2"
    assert tok.claims == {"domain": "exocortex"}
    upper = session("exocortex", 2, GUID_A)
    text, digest = upper.rsplit(",", 1)
    assert verify(v, f"{PREFIX}{text},{digest.upper()}") is not None  # hex case does not matter
    assert verify(v, PREFIX + session("exocortex", 2, GUID_B)) is None  # sealed with another domain's GUID
    assert verify(v, PREFIX + session("exocortex", 3, GUID_A).replace(",3,", ",2,", 1)) is None  # tampered
    assert verify(v, PREFIX + session("closed", 2, GUID_OFF)) is None  # disabled domain
    assert verify(v, PREFIX + session("nobody", 2, GUID_A)) is None
    assert verify(v, session("exocortex", 2, GUID_A)) is None  # no OSESSION/ prefix
    assert verify(DomainVerifierMissing(), PREFIX + session("exocortex", 2, GUID_A)) is None


class DomainVerifierMissing(OSessionVerifier):
    def __init__(self) -> None:
        super().__init__(DomainDirectory(Path("/nonexistent-cerp-domain-root")))


def test_dispatching_verifier(tmp_path: Path) -> None:
    build_nodedb(tmp_path)
    d = DispatchingVerifier(Fallback(), OSessionVerifier(DomainDirectory(tmp_path)))
    assert verify(d, "static-token").client_id == "other"  # type: ignore[union-attr]
    assert verify(d, "wrong") is None
    tok = verify(d, PREFIX + session("in.bial", 5, GUID_B))
    assert tok is not None
    assert tok.client_id == OSESSION_CLIENT_ID
    assert (
        verify(DispatchingVerifier(None, OSessionVerifier(DomainDirectory(tmp_path))), "static-token") is None
    )


# ---------------------------------------------------------------- end to end


@pytest.fixture
def domains(tmp_path: Path, tenant: Path) -> Path:
    """Two domains, each a copy of the test tenant; domain B renames project 1 to tell them apart."""
    root = tmp_path / "domains"
    build_nodedb(root)
    for guid, name in ((GUID_A, None), (GUID_B, "Domain B website")):
        folder = DomainDirectory(root).database_dir(guid)
        folder.mkdir(parents=True)
        for f in tenant.glob("*.s3db"):
            shutil.copy(f, folder / f.name)
        if name:
            con = sqlite3.connect(folder / "pm.s3db")
            con.execute("UPDATE projects SET name = ? WHERE id = 1", (name,))
            con.commit()
            con.close()
    return root


@pytest.fixture
def server(mvp_registry: Path, tenant: Path, domains: Path) -> MCPServer:
    settings = Settings.model_validate(
        {
            "registry": {"path": str(mvp_registry)},
            "data": {"dbroot": str(tenant)},
            "auth": {"osession": {"enabled": True, "domain_root": str(domains)}},
            "tools": {"packs": [*ALL_PACKS, "pm_write"]},
        }
    )
    mcp, _ = build_runtime(settings, {}, clock=lambda: NOW)
    return mcp


@pytest.fixture
def as_session(domains: Path) -> Callable[[str, int, str], Iterator[None]]:
    def use(domain: str, user_id: int, guid: str) -> Any:
        tok = verify(OSessionVerifier(DomainDirectory(domains)), PREFIX + session(domain, user_id, guid))
        assert tok is not None
        return AuthenticatedUser(tok)

    return use  # type: ignore[return-value]


def call(mcp: MCPServer, user: AuthenticatedUser | None, tool: str, args: dict[str, Any]) -> tuple[bool, Any]:
    async def go() -> tuple[bool, Any]:
        reset = auth_context_var.set(user)
        try:
            async with Client(mcp) as c:
                r = await c.call_tool(tool, args)
                return bool(r.is_error), r.structured_content
        finally:
            auth_context_var.reset(reset)

    return anyio.run(go)


def test_session_caller_uses_its_domain(
    server: MCPServer, as_session: Any, domains: Path, tenant: Path
) -> None:
    a = as_session("exocortex", 2, GUID_A)  # alice in domain A
    b = as_session("in.bial", 2, GUID_B)  # alice in domain B
    err, out = call(server, a, "pm.get_project", {"project": "pm:project:1"})
    assert not err, out
    assert out["project"]["name"] == "Website rebuild"
    err, out = call(server, b, "pm.get_project", {"project": "pm:project:1"})
    assert not err, out
    assert out["project"]["name"] == "Domain B website"

    # a write lands in the caller's domain only
    err, out = call(server, b, "pm.check_task", {"task": "pm:todo_list_item:3"})
    assert not err, out
    status = "SELECT status FROM todo_list_items WHERE id = 3"
    folder_b = DomainDirectory(domains).database_dir(GUID_B)
    folder_a = DomainDirectory(domains).database_dir(GUID_A)
    assert sqlite3.connect(folder_b / "pm.s3db").execute(status).fetchone()[0] == 70000
    assert sqlite3.connect(folder_a / "pm.s3db").execute(status).fetchone()[0] == 10000
    assert sqlite3.connect(tenant / "pm.s3db").execute(status).fetchone()[0] == 10000  # default untouched


def test_session_user_comes_from_the_domain(server: MCPServer, as_session: Any, domains: Path) -> None:
    folder_b = DomainDirectory(domains).database_dir(GUID_B)
    con = sqlite3.connect(folder_b / "security.s3db")
    con.execute("UPDATE users SET enabled = 0 WHERE id = 2")  # alice disabled in domain B only
    con.commit()
    con.close()
    err, out = call(server, as_session("in.bial", 2, GUID_B), "pm.my_tasks", {})
    assert err
    assert out["error"]["code"] == "not_permitted"
    err, out = call(server, as_session("exocortex", 2, GUID_A), "pm.my_tasks", {})
    assert not err, out
    err, out = call(server, as_session("exocortex", 99, GUID_A), "pm.my_tasks", {})  # no such user
    assert err


def test_domain_without_databases_is_refused(server: MCPServer, as_session: Any, domains: Path) -> None:
    shutil.rmtree(DomainDirectory(domains).database_dir(GUID_B))
    err, out = call(server, as_session("in.bial", 2, GUID_B), "pm.my_tasks", {})
    assert err
    assert out["error"]["code"] == "not_permitted"


def test_config_requires_domain_root() -> None:
    with pytest.raises(ValueError, match="domain_root"):
        Settings.model_validate({"auth": {"osession": {"enabled": True}}})
    with pytest.raises(ValueError, match="local_user"):
        Settings.model_validate(
            {"auth": {"local_user": "alice", "osession": {"enabled": True, "domain_root": "/x"}}}
        )


def test_http_bearer_osession(mvp_registry: Path, tenant: Path, domains: Path) -> None:
    """Through the real HTTP stack: the SDK's bearer middleware hands ``OSESSION/...`` to the dispatcher."""
    settings = Settings.model_validate(
        {
            "registry": {"path": str(mvp_registry)},
            "data": {"dbroot": str(tenant)},
            "auth": {"osession": {"enabled": True, "domain_root": str(domains)}},
            "tools": {"packs": ["pm"]},
        }
    )
    app, _, _ = build_app(settings, {})

    def tools_list(c: TestClient, bearer: str | None) -> Any:
        hdr = {**ACCEPT, "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}
        if bearer:
            hdr["Authorization"] = f"Bearer {bearer}"
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta()}}
        return c.post("/mcp", json=body, headers=hdr)

    raw = session("exocortex", 2, GUID_A)
    with TestClient(app, base_url="http://127.0.0.1:7411") as c:
        assert tools_list(c, PREFIX + raw).status_code == 200
        assert tools_list(c, PREFIX + quote(raw, safe="")).status_code == 200
        assert tools_list(c, PREFIX + session("exocortex", 2, GUID_B)).status_code == 401
        assert tools_list(c, raw).status_code == 401  # no OSESSION/ prefix, and no other verifier
        assert tools_list(c, None).status_code == 401
