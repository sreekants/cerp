"""The crm write tools (decision 0004): contacts, customers (accounts), vendors and their links, end to end
through the MCP client.

Covers MPC001-254 (accounts, duplicate check first), MPC001-255 (contacts; personal fields never written
or returned), MPC001-259 (vendors) and the guardrails of MPC001-250: owner from the caller, soft delete
(status -1) on plain-PK tables, rights through the authorization port (MPC001-061), and the one allowed
DELETE on ``project_contacts``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from tests.test_args import _strings_without_bound
from tests.test_readonly_tools import ALL_PACKS, error_code, mvp_registry, ok, server, tenant, tools

__all__ = ["mvp_registry", "server", "tenant"]  # fixtures used by name

SCHEMA = "tool_error"  # rejected by the input schema before the tool runs (MPC001-065)
PACKS = [*ALL_PACKS, "crm_write"]
CRM_WRITE_TOOLS = {
    "crm.create_account", "crm.update_account", "crm.delete_account", "crm.create_vendor",
    "crm.update_vendor", "crm.delete_vendor", "crm.create_contact", "crm.update_contact",
    "crm.delete_contact", "crm.link_contact_account", "crm.unlink_contact_account",
    "crm.link_contact_vendor", "crm.unlink_contact_vendor", "crm.link_contact_project",
    "crm.unlink_contact_project",
}  # fmt: skip
PERSONAL = {"home_address", "home_phone", "home_fax", "birthday", "anniversary", "spouse_name", "nickname"}


@pytest.fixture
def crm(server: Callable[..., MCPServer]) -> Callable[..., MCPServer]:
    def make(user: str = "alice") -> MCPServer:
        return server(user, PACKS)

    return make


def db(dbroot: Path, statement: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"{(dbroot / 'crm.s3db').as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(statement, params).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- catalogue


def test_crm_write_tools_listed_only_with_their_pack(
    server: Callable[..., MCPServer], crm: Callable[..., MCPServer]
) -> None:
    assert not {t.name for t in tools(server())} & CRM_WRITE_TOOLS
    listed = {t.name: t for t in tools(crm())}
    assert set(listed) >= CRM_WRITE_TOOLS
    for name in CRM_WRITE_TOOLS:
        t = listed[name]
        assert t.annotations is not None
        assert t.annotations.read_only_hint is False, name
        assert list(_strings_without_bound(t.input_schema)) == [], name
        assert not PERSONAL & set(t.input_schema.get("properties", {})), name  # MPC001-255 req 2


# ---------------------------------------------------------------- accounts


def test_create_account_checks_duplicates_first(crm: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = crm()
    out = ok(mcp, "crm.create_account", {"name": "Acme", "website": "https://acme.no/about"})
    assert out["record"] is None
    assert out["created"] is False
    assert [c["record"]["id"] for c in out["duplicates"]] == [1001]
    assert len(db(tenant, "SELECT id FROM accounts")) == 3  # nothing written

    out = ok(mcp, "crm.create_account", {"name": "Acme", "website": "acme.no", "confirm": True})
    assert out["created"] is True
    assert out["record"]["id"] == 1004  # explicit id, MAX + 1 (crm.md §8 rule 2)
    out = ok(mcp, "crm.create_account", {"name": "Initech", "type": 2, "phone": "+47 99 88 77 66"})
    rec = out["record"]
    assert rec["handle"] == "crm:account:1005"
    assert rec["type_label"] == "Private Company"
    assert rec["status_label"] == "Active"
    row = db(tenant, "SELECT owner_id, owner, creation_date FROM accounts WHERE id = 1005")[0]
    assert (row["owner_id"], row["owner"]) == (2, "Alice Andersen")  # from the caller (MPC001-250 req 3)
    assert row["creation_date"] == "2026-11-04 10:00:00"


def test_update_and_delete_account(crm: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = crm()
    out = ok(
        mcp,
        "crm.update_account",
        {"account": "crm:account:1002", "phone": "+44 20 1234 5678", "status": 5000},
    )
    assert out["record"]["phone"] == "+44 20 1234 5678"
    assert out["record"]["status_label"] == "Inactive"
    assert out["record"]["name"] == "Globex Ltd"  # untouched
    assert db(tenant, "SELECT modification_date FROM accounts WHERE id = 1002")[0][0] == "2026-11-04 10:00:00"
    assert error_code(mcp, "crm.update_account", {"account": "crm:account:1002", "type": 9}) == SCHEMA

    assert ok(mcp, "crm.delete_account", {"account": "crm:account:1002"})["record"]["status"] == -1
    assert error_code(mcp, "crm.get_account", {"account": "crm:account:1002"}) == "not_found"
    assert error_code(mcp, "crm.update_account", {"account": "crm:account:1002", "name": "x"}) == "not_found"
    assert error_code(mcp, "crm.delete_account", {"account": "crm:account:1003"}) == "not_found"  # already -1
    assert db(tenant, "SELECT status FROM accounts WHERE id = 1002")[0][0] == -1  # still there


def test_account_rights(crm: Callable[..., MCPServer]) -> None:
    bob = crm("bob")
    assert error_code(bob, "crm.create_account", {"name": "x"}) == "not_permitted"
    assert (
        error_code(bob, "crm.update_account", {"account": "crm:account:1001", "name": "x"}) == "not_permitted"
    )
    assert error_code(bob, "crm.delete_account", {"account": "crm:account:1001"}) == "not_permitted"


# ---------------------------------------------------------------- vendors


def test_vendor_lifecycle(crm: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = crm()
    out = ok(mcp, "crm.create_vendor", {"name": "Parts Supply", "phone": "22 00 11 22"})
    assert out["record"] is None
    assert out["duplicates"][0]["record"]["handle"] == "crm:vendor:1001"
    rec = ok(mcp, "crm.create_vendor", {"name": "Bolt Works", "website": "bolts.example"})["record"]
    assert rec["handle"] == "crm:vendor:1002"
    assert rec["status"] == 1000
    rec = ok(mcp, "crm.update_vendor", {"vendor": rec["handle"], "description": "Fasteners", "status": 5000})[
        "record"
    ]
    assert rec["description"] == "Fasteners"
    assert rec["status_label"] == "Inactive"
    assert ok(mcp, "crm.delete_vendor", {"vendor": rec["handle"]})["record"]["status"] == -1
    assert error_code(mcp, "crm.update_vendor", {"vendor": rec["handle"], "name": "x"}) == "not_found"
    assert error_code(crm("bob"), "crm.create_vendor", {"name": "x"}) == "not_permitted"
    assert db(tenant, "SELECT owner_id FROM vendors WHERE id = 1002")[0][0] == 2


# ---------------------------------------------------------------- contacts


def test_contact_create_update_delete(crm: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = crm()
    dup = ok(mcp, "crm.create_contact", {"first_name": "K", "email": "KARI@acme.no"})
    assert dup["record"] is None
    assert dup["duplicates"][0]["record"]["id"] == 1001

    rec = ok(
        mcp, "crm.create_contact", {"first_name": "Per", "last_name": "Olsen", "email": "per@initech.no"}
    )
    rec = rec["record"]
    assert rec["handle"] == "crm:contact:1003"
    assert rec["account"] is None
    assert rec["status"] == 1000
    assert not PERSONAL & set(rec)
    h = rec["handle"]
    rec = ok(mcp, "crm.update_contact", {"contact": h, "description": "Buyer", "mobile": "+47 400 00 000"})[
        "record"
    ]
    assert (rec["description"], rec["mobile"], rec["email"]) == ("Buyer", "+47 400 00 000", "per@initech.no")
    rec = ok(
        mcp, "crm.update_contact", {"contact": h, "email": "per.olsen@initech.no", "business_phone": "22 11"}
    )
    assert rec["record"]["email"] == "per.olsen@initech.no"
    assert rec["record"]["business_phone"] == "22 11"
    assert error_code(mcp, "crm.update_contact", {"contact": h, "email": "not an email"}) == SCHEMA
    assert error_code(mcp, "crm.create_contact", {"first_name": ""}) == SCHEMA

    # the existing contact keeps its personal fields untouched and unseen
    rec = ok(mcp, "crm.update_contact", {"contact": "crm:contact:1001", "description": "CFO"})["record"]
    assert not PERSONAL & set(rec)
    assert db(tenant, "SELECT home_phone FROM contacts WHERE id = 1001")[0][0] == "555-HOME"

    # deleting the primary contact clears the account's pointer
    assert ok(mcp, "crm.delete_contact", {"contact": "crm:contact:1001"})["record"]["status"] == -1
    assert db(tenant, "SELECT contact_id FROM accounts WHERE id = 1001")[0][0] == 0
    assert (
        error_code(mcp, "crm.update_contact", {"contact": "crm:contact:1001", "description": "x"})
        == "not_found"
    )
    assert db(tenant, "SELECT status FROM contacts WHERE id = 1001")[0][0] == -1


def test_contact_rights(crm: Callable[..., MCPServer]) -> None:
    bob = crm("bob")
    assert error_code(bob, "crm.create_contact", {"first_name": "x"}) == "not_permitted"
    assert (
        error_code(bob, "crm.update_contact", {"contact": "crm:contact:1002", "email": ""}) == "not_permitted"
    )
    assert error_code(bob, "crm.delete_contact", {"contact": "crm:contact:1002"}) == "not_permitted"


# ---------------------------------------------------------------- links


def test_link_and_unlink_account_and_vendor(crm: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = crm()
    c = "crm:contact:1002"  # Ola, linked to Acme (1001), not primary
    out = ok(mcp, "crm.link_contact_account", {"contact": c, "account": "crm:account:1002", "primary": True})
    assert out["record"]["account"] == "crm:account:1002"
    assert db(tenant, "SELECT contact_id FROM accounts WHERE id = 1002")[0][0] == 1002
    # one account or one vendor
    assert (
        error_code(mcp, "crm.link_contact_vendor", {"contact": c, "vendor": "crm:vendor:1001"})
        == "validation"
    )
    assert ok(mcp, "crm.unlink_contact_account", {"contact": c})["record"]["account"] is None
    assert db(tenant, "SELECT contact_id FROM accounts WHERE id = 1002")[0][0] == 0

    out = ok(mcp, "crm.link_contact_vendor", {"contact": c, "vendor": "crm:vendor:1001", "primary": True})
    assert out["record"]["vendor"] == "crm:vendor:1001"
    assert db(tenant, "SELECT contact_id FROM vendors WHERE id = 1001")[0][0] == 1002
    assert error_code(mcp, "crm.link_contact_account", {"contact": c, "account": "crm:account:1001"}) == (
        "validation"
    )
    assert ok(mcp, "crm.unlink_contact_vendor", {"contact": c})["record"]["vendor"] is None
    assert db(tenant, "SELECT contact_id FROM vendors WHERE id = 1001")[0][0] == 0
    # deleted targets are not found
    assert (
        error_code(mcp, "crm.link_contact_account", {"contact": c, "account": "crm:account:1003"})
        == "not_found"
    )
    assert error_code(crm("bob"), "crm.unlink_contact_account", {"contact": c}) == "not_permitted"


def test_link_and_unlink_project(crm: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = crm()
    c = "crm:contact:1001"
    out = ok(mcp, "crm.link_contact_project", {"contact": c, "project": "pm:project:1", "role": "sponsor"})
    assert out["record"]["projects"] == [{"project": "pm:project:1", "role": "sponsor"}]
    out = ok(mcp, "crm.link_contact_project", {"contact": c, "project": "pm:project:1", "role": "owner"})
    assert out["record"]["projects"] == [{"project": "pm:project:1", "role": "owner"}]  # no second row
    ok(mcp, "crm.link_contact_project", {"contact": c, "project": "pm:project:3"})
    assert len(db(tenant, "SELECT id FROM project_contacts WHERE contact_id = 1001")) == 2
    assert (
        error_code(mcp, "crm.link_contact_project", {"contact": c, "project": "pm:project:99"}) == "not_found"
    )

    out = ok(mcp, "crm.unlink_contact_project", {"contact": c, "project": "pm:project:1"})
    assert out["record"]["projects"] == [{"project": "pm:project:3", "role": None}]
    assert (
        error_code(mcp, "crm.unlink_contact_project", {"contact": c, "project": "pm:project:1"})
        == "not_found"
    )
    assert db(tenant, "SELECT project_id FROM project_contacts")[0][0] == 3  # only the link row went
    assert db(tenant, "SELECT status FROM contacts WHERE id = 1001")[0][0] == 1000

    bob = crm("bob")
    assert (
        error_code(bob, "crm.link_contact_project", {"contact": c, "project": "pm:project:1"})
        == "not_permitted"
    )
    assert error_code(bob, "crm.unlink_contact_project", {"contact": c, "project": "pm:project:3"}) == (
        "not_permitted"
    )
