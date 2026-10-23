"""MPC002-006 registry generator and loader."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from cerp.data.registry import (
    SCHEMA_VERSION,
    RegistryError,
    field_name,
    load_registry,
    pluralize,
    table_name_for,
)
from cerp.registry.generate import build, main, parse_wsidl, privilege_verb, render
from tests.conftest import DATASOURCES, PORTAL


def _codes(reg: dict[str, Any], model: str) -> set[str]:
    return {w["code"] for w in reg["warnings"] if model in w["model"]}


@pytest.fixture
def reg(dbroot: Path) -> dict[str, Any]:
    out: dict[str, Any] = yaml.safe_load(render(build(PORTAL, DATASOURCES, dbroot)))
    return out


def test_every_manifest_has_an_entry(reg: dict[str, Any]) -> None:
    assert len(reg["models"]) == len(list(PORTAL.rglob("*.wsm")))
    assert reg["schema_version"] == SCHEMA_VERSION


def test_table_model_entry(reg: dict[str, Any]) -> None:
    t = reg["models"]["objects/support/ticket"]
    assert t["identity"]["model"] == "Ticket"
    assert t["identity"]["class_id"] == "DDA807DE-9E64-4DC3-A794-62B55B524F76"
    assert t["storage"] == {
        "datasource": "im",
        "file": "$(DBROOT)/im.s3db",
        "table": "tickets",
        "table_source": "derived",
    }
    assert t["access"] == {"raw": "Privilege=$Model$", "privilege_model": "Ticket", "enforced": True}
    assert t["behaviour"]["model_defaults"]["case_id"] == "0"
    assert t["behaviour"]["model_defaults"]["map_url"] is None
    assert t["behaviour"]["delete"] == "hard"
    assert any(c["name"] == "short_name" for c in t["columns"])
    methods = {m["name"]: m["privilege"] for m in t["methods"]}
    assert methods["Add"] == "Ticket.Add"
    assert methods["Update"] == "Ticket.Edit"
    assert methods["GetShortName"] == "Ticket.View"
    assert methods["SetShortName"] == "Ticket.Edit"
    assert methods["Delete"] == "Ticket.Delete"
    assert methods["DoDelete"] == "unclassified"


def test_privilege_alias_and_soft_delete(reg: dict[str, Any]) -> None:
    link = reg["models"]["objects/support/ticket_link"]
    assert link["access"]["privilege_model"] == "Ticket"
    receipt = reg["models"]["objects/cashflow/receipt"]
    assert receipt["behaviour"]["deleted_state"] == {"field": "status", "value": -1}
    assert receipt["behaviour"]["delete"] == "soft"
    assert "hard_delete" not in _codes(reg, "objects/cashflow/receipt")
    assert "hard_delete" in _codes(reg, "objects/support/ticket")


def test_script_model_with_wsidl(reg: dict[str, Any]) -> None:
    asset = reg["models"]["objects/assets/asset"]
    assert asset["script"] == {"language": "JScript", "source": "controller.js", "wsidl": "controller.wsidl"}
    methods = {m["name"]: m for m in asset["methods"]}
    assert methods["Add"]["params"][0] == {"type": "string", "name": "Name"}
    assert methods["GetAge"]["returns"] == "double"
    assert "access_not_enforced" in _codes(reg, "objects/assets/asset")
    assert methods["SetLocation"]["privilege"] == "(not enforced).Edit"


def test_warnings_cover_each_case(reg: dict[str, Any]) -> None:
    assert _codes(reg, "objects/assets/sensor") >= {"datasource_unmapped"}
    assert reg["models"]["objects/assets/sensor"]["storage"]["table_source"] == "explicit"
    assert "table_not_found" in _codes(reg, "objects/inventories/product_property")
    assert "database_missing" in _codes(reg, "services/security/user")
    assert "access_not_enforced" in _codes(reg, "services/security/user")
    unclassified = {w["detail"].split()[0] for w in reg["warnings"] if w["code"] == "unclassified_method"}
    assert {"DoDelete", "UpdateAssetClassID"} <= unclassified


def test_output_is_deterministic(dbroot: Path) -> None:
    a = render(build(PORTAL, DATASOURCES, dbroot))
    b = render(build(PORTAL, DATASOURCES, dbroot))
    assert a == b
    assert b"\r\n" not in a


def test_check_mode(tmp_path: Path, dbroot: Path) -> None:
    portal = tmp_path / "portal"
    import shutil  # noqa: PLC0415

    shutil.copytree(PORTAL, portal)
    out = tmp_path / "reg.yaml"
    args = [
        "--portal",
        str(portal),
        "--datasources",
        str(DATASOURCES),
        "--dbroot",
        str(dbroot),
        "--out",
        str(out),
    ]
    assert main(args) == 0
    assert main([*args, "--check"]) == 0
    m = portal / "objects/modules/support/ticket/controller/controller.wsm"
    m.write_text(m.read_text().replace("Ticket Library", "Ticket Library v2"))
    assert main([*args, "--check"]) == 1


def test_generator_writes_nothing_under_inputs(tmp_path: Path, dbroot: Path) -> None:
    def snapshot(root: Path) -> dict[str, str]:
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}

    before = (snapshot(PORTAL), snapshot(dbroot))
    main(
        [
            "--portal",
            str(PORTAL),
            "--datasources",
            str(DATASOURCES),
            "--dbroot",
            str(dbroot),
            "--out",
            str(tmp_path / "r.yaml"),
        ]
    )
    assert (snapshot(PORTAL), snapshot(dbroot)) == before


def test_loader_reads_generated_file(registry_file: Path) -> None:
    reg = load_registry(registry_file)
    ticket = reg.models["objects/support/ticket"]
    assert ticket.is_table
    assert ticket.datasource == "im"
    assert ticket.column_names is not None
    assert "short_name" in ticket.column_names
    assert reg.models["objects/cashflow/receipt"].deleted_state == ("status", -1)
    assert len(reg.fingerprint) == 16


def test_loader_refuses_unknown_schema_version(tmp_path: Path) -> None:
    p = tmp_path / "r.yaml"
    p.write_text("schema_version: 99\nmodels: {}\n")
    with pytest.raises(RegistryError, match="schema version 99"):
        load_registry(p)


@pytest.mark.parametrize(
    ("cls", "table"),
    [
        ("Ticket", "tickets"),
        ("ProductCategory", "product_categories"),
        ("ProductStatistic", "product_statistics"),
        ("Address", "addresses"),
        ("Activity", "activities"),
        ("Bom", "boms"),
        ("ProductMatrixDef", "product_matrix_defs"),
    ],
)
def test_table_name_rule(cls: str, table: str) -> None:
    assert table_name_for(cls) == table


@given(st.from_regex(r"[A-Z][a-z]{1,8}([A-Z][a-z]{1,8}){0,3}", fullmatch=True))
def test_field_name_is_lower_snake(cls: str) -> None:
    name = field_name(cls)
    assert name == name.lower()
    assert name.replace("_", "").isalpha()
    assert pluralize(name).startswith(name[:-1])


def test_privilege_rule() -> None:
    assert privilege_verb("CreateFromXML") == "Add"
    assert privilege_verb("FromXML") == "Edit"
    assert privilege_verb("ToXML") == "View"
    assert privilege_verb("MoveTo") is None


def test_wsidl_parser_rejects_garbage() -> None:
    assert parse_wsidl("not an interface") is None
    assert parse_wsidl("interface X { void F( long a b c ); };") is None
    assert parse_wsidl("interface X { /* c */ long F(); // x\n };") == [
        {"name": "F", "returns": "long", "params": []}
    ]
