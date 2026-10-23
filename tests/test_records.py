"""MPC001-087 handles, MPC001-163 record conventions."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cerp.data.handles import Handle, HandleError
from cerp.data.records import clean_row, concurrency_token, deleted_rule, insert_values, visible_clause
from cerp.data.registry import load_registry
from cerp.data.sqlite import Databases


def test_handle_round_trip() -> None:
    h = Handle.parse("im:ticket:1002")
    assert h == Handle("im", "ticket", 1002)
    assert str(h) == "im:ticket:1002"
    assert h.expect("im", "ticket") is h


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "im:ticket",
        "im:ticket:0",
        "im:ticket:-1",
        "im:ticket:1;DROP",
        "IM:ticket:1",
        "im:ticket:99999999999999999999",
        "im::1",
    ],
)
def test_bad_handles(bad: str) -> None:
    with pytest.raises(HandleError):
        Handle.parse(bad)


def test_handle_for_other_model_is_refused() -> None:
    with pytest.raises(HandleError):
        Handle.parse("crm:account:1").expect("im", "ticket")


@given(st.integers(min_value=1, max_value=2**63 - 1))
def test_handle_ids(i: int) -> None:
    assert Handle.parse(str(Handle("im", "ticket", i))).id == i


def test_soft_delete_rules(registry_file: Path) -> None:
    reg = load_registry(registry_file)
    receipt = deleted_rule(reg.models["objects/cashflow/receipt"])
    assert receipt is not None
    assert (receipt.column, receipt.value, receipt.assumed) == ("status", -1, False)
    ticket = deleted_rule(reg.models["objects/support/ticket"])
    assert ticket is not None
    assert ticket.assumed is True
    assert visible_clause(reg.models["objects/support/ticket_link"]) == ("1=1", ())


def test_soft_deleted_rows_disappear_from_reads(registry_file: Path, dbroot: Path) -> None:
    reg = load_registry(registry_file)
    model = reg.models["objects/support/ticket"]
    dbs = Databases(reg.database_files(dbroot))
    with dbs.writer("im") as con:
        con.execute("UPDATE tickets SET status = -1 WHERE id = (SELECT MIN(id) FROM tickets)")
    clause, params = visible_clause(model)
    with dbs.reader("im") as con:
        total = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        visible = con.execute(f"SELECT COUNT(*) FROM tickets WHERE {clause}", params).fetchone()[0]  # noqa: S608
    assert visible == total - 1


def test_insert_values_apply_defaults_and_stamps(registry_file: Path) -> None:
    reg = load_registry(registry_file)
    model = reg.models["objects/support/ticket"]
    vals = insert_values(
        model, {"short_name": "Printer jam", "case_id": 7, "not_a_column": 1}, user=3, now=1000.0
    )
    assert vals["case_id"] == 7  # caller wins
    assert vals["account_id"] == 0  # ModelDefaults
    assert vals["map_url"] is None
    assert vals["creation_time"] == vals["modification_time"] == 1000
    assert "not_a_column" not in vals


def test_concurrency_token_tracks_values() -> None:
    a = {"id": 1, "status": 1000, "owner": "x"}
    assert concurrency_token(a) == concurrency_token(dict(reversed(a.items())))
    assert concurrency_token(a) != concurrency_token({**a, "status": 2000})


def test_null_strings_are_cleaned() -> None:
    assert clean_row({"a": "NULL", "b": "x", "c": None}) == {"a": None, "b": "x", "c": None}
