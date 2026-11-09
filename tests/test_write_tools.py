"""The first write tools (decision 0003): to-do lists and tasks (``pm_write``, MPC001-212) and reminders
(``personal_write``, MPC001-242), end to end through the MCP client.

Also covers the guardrails they rest on (MPC001-207): owner from the caller, typed ``todo_list_items``
columns (``pm.md`` §9.1), soft delete only, rights through the authorization port (MPC001-061) and personal
privacy with no administrator override (MPC001-243).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from cerp.app.config import Settings
from cerp.app.server import build_runtime
from tests.mvp_tenant import NOW
from tests.test_args import _strings_without_bound
from tests.test_readonly_tools import ALL_PACKS, call, error_code, mvp_registry, ok, server, tenant, tools

__all__ = ["mvp_registry", "server", "tenant"]  # fixtures used by name

SCHEMA = "tool_error"  # rejected by the input schema before the tool runs (MPC001-065)
WRITE_PACKS = [*ALL_PACKS, "pm_write", "personal_write"]
WRITE_TOOLS = {
    "pm.create_todo_list", "pm.update_todo_list", "pm.delete_todo_list", "pm.create_task", "pm.update_task",
    "pm.set_task_comment", "pm.check_task", "personal.add_reminder", "personal.rename_reminder",
    "personal.set_reminder_status", "personal.delete_reminder",
}  # fmt: skip


@pytest.fixture
def writer(server: Callable[..., MCPServer]) -> Callable[..., MCPServer]:
    def make(user: str = "alice") -> MCPServer:
        return server(user, WRITE_PACKS)

    return make


def row(dbroot: Path, db: str, statement: str, params: tuple[Any, ...]) -> sqlite3.Row:
    con = sqlite3.connect(f"{(dbroot / f'{db}.s3db').as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        found = con.execute(statement, params).fetchone()
    finally:
        con.close()
    assert found is not None
    return found


def _my_task_ids(mcp: MCPServer) -> set[int]:
    out = ok(mcp, "pm.my_tasks")
    return {
        int(t["id"]) for group in ("overdue", "today", "this_week", "later", "no_date") for t in out[group]
    }


# ---------------------------------------------------------------- catalogue


def test_write_tools_only_with_write_packs(
    server: Callable[..., MCPServer], writer: Callable[..., MCPServer]
) -> None:
    assert not {t.name for t in tools(server())} & WRITE_TOOLS
    listed = {t.name: t for t in tools(writer())}
    assert set(listed) >= WRITE_TOOLS
    for name in WRITE_TOOLS:
        t = listed[name]
        assert t.annotations is not None
        assert t.annotations.read_only_hint is False, name
        assert t.annotations.destructive_hint is False, name  # deletes are soft and reversible
        assert list(_strings_without_bound(t.input_schema)) == [], name


def test_writes_kill_switch_hides_them(mvp_registry: Path, tenant: Path) -> None:

    settings = Settings.model_validate(
        {
            "registry": {"path": str(mvp_registry)},
            "data": {"dbroot": str(tenant)},
            "auth": {"local_user": "alice"},
            "tools": {"packs": WRITE_PACKS},
            "kill_switches": {"writes": True},
        }
    )
    mcp, _ = build_runtime(settings, {}, clock=lambda: NOW)
    assert not {t.name for t in tools(mcp)} & WRITE_TOOLS
    assert error_code(mcp, "pm.check_task", {"task": "pm:todo_list_item:3"}) == "disabled"


# ---------------------------------------------------------------- tasks


def test_create_task_writes_typed_columns_and_owner(writer: Callable[..., MCPServer], tenant: Path) -> None:
    out = ok(
        writer(),
        "pm.create_task",
        {"todo_list": "pm:todo_list:1", "description": "Write release notes", "due_date": "2026-11-06"},
    )["task"]
    assert out["handle"].startswith("pm:todo_list_item:")
    assert out["status"] == 10000
    assert out["status_label"] == "New"
    assert out["todo_list"] == "pm:todo_list:1"
    r = row(
        tenant,
        "pm",
        "SELECT typeof(status) s, typeof(budgeted) b, typeof(estimated) e, typeof(billed) bi, owner_id,"
        " owner, sort_order, assigned_to FROM todo_list_items WHERE id = ?",
        (out["id"],),
    )
    assert (r["s"], r["b"], r["e"], r["bi"]) == ("integer",) * 4  # pm.md §9.1 detection query
    assert (r["owner_id"], r["owner"]) == (2, "Alice Andersen")  # from the caller (MPC001-207 req 3)
    assert r["sort_order"] == 8
    assert r["assigned_to"] == 0


def test_update_comment_and_check_off(writer: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = writer()
    task = "pm:todo_list_item:3"
    out = ok(mcp, "pm.update_task", {"task": task, "description": "Test all pages", "estimated": 5})["task"]
    assert (out["description"], out["estimated"], out["due_date"]) == (
        "Test all pages",
        5,
        "2026-11-06 00:00:00",  # untouched
    )
    assert ok(mcp, "pm.set_task_comment", {"task": task, "comment": "needs Safari"})["task"]["comments"] == (
        "needs Safari"
    )
    assert ok(mcp, "pm.set_task_comment", {"task": task, "comment": "Safari done"})["task"]["comments"] == (
        "Safari done"
    )
    assert error_code(mcp, "pm.set_task_comment", {"task": task, "comment": "x" * 129}) == SCHEMA
    assert 3 in _my_task_ids(mcp)
    assert ok(mcp, "pm.check_task", {"task": task})["task"]["status"] == 70000
    assert 3 not in _my_task_ids(mcp)
    assert ok(mcp, "pm.check_task", {"task": task, "done": False})["task"]["status"] == 10000
    assert 3 in _my_task_ids(mcp)
    # the corrupt text status '0' (pm.md §9.1) is overwritten with a real code
    assert ok(mcp, "pm.check_task", {"task": "pm:todo_list_item:5"})["task"]["status"] == 70000


def test_task_rights_and_missing_records(writer: Callable[..., MCPServer]) -> None:
    assert error_code(writer("bob"), "pm.check_task", {"task": "pm:todo_list_item:3"}) == "not_permitted"
    assert error_code(
        writer("nobody"), "pm.create_task", {"todo_list": "pm:todo_list:1", "description": "x"}
    ) == ("not_permitted")
    alice = writer()
    assert error_code(alice, "pm.check_task", {"task": "pm:todo_list_item:8"}) == "not_found"  # status -1
    assert error_code(alice, "pm.check_task", {"task": "pm:todo_list_item:999"}) == "not_found"
    assert error_code(alice, "pm.create_task", {"todo_list": "pm:todo_list:1", "description": ""}) == SCHEMA
    assert (
        error_code(
            alice, "pm.create_task", {"todo_list": "pm:todo_list:1", "description": "x", "assigned_to": 999}
        )
        == "validation"
    )
    assert error_code(alice, "pm.check_task", {"task": "pm:todo_list:3"}) == "validation"  # wrong model


def test_personal_lists_are_owner_only(writer: Callable[..., MCPServer]) -> None:
    alice, admin = writer(), writer("admin")
    for mcp in (alice, admin):  # no administrator override (MPC001-243)
        assert error_code(mcp, "pm.check_task", {"task": "pm:todo_list_item:7"}) == "not_found"
        assert error_code(mcp, "pm.create_task", {"todo_list": "pm:todo_list:3", "description": "x"}) == (
            "not_found"
        )
        assert error_code(mcp, "pm.delete_todo_list", {"todo_list": "pm:todo_list:3"}) == "not_found"
    # own personal list: fine, but only assignable to yourself
    assert ok(alice, "pm.check_task", {"task": "pm:todo_list_item:6"})["task"]["status"] == 70000
    assert (
        error_code(
            alice, "pm.create_task", {"todo_list": "pm:todo_list:2", "description": "x", "assigned_to": 3}
        )
        == "validation"
    )
    ok(alice, "pm.create_task", {"todo_list": "pm:todo_list:2", "description": "x", "assigned_to": 2})


# ---------------------------------------------------------------- lists


def test_create_update_delete_list(writer: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = writer()
    personal = ok(mcp, "pm.create_todo_list", {"title": "Errands", "priority": 5})["todo_list"]
    assert personal["personal"] is True
    assert personal["project"] is None
    assert personal["status"] == 10000
    assert personal["priority_label"] == "Medium"
    project = ok(mcp, "pm.create_todo_list", {"title": "QA", "project": "pm:project:1"})["todo_list"]
    assert project["project"] == "pm:project:1"
    assert project["sort_order"] == 2
    assert error_code(mcp, "pm.create_todo_list", {"title": "x", "project": "pm:project:99"}) == "not_found"

    lst = project["handle"]
    out = ok(mcp, "pm.update_todo_list", {"todo_list": lst, "description": "Release testing", "priority": 9})
    assert out["todo_list"]["description"] == "Release testing"
    assert out["todo_list"]["priority_label"] == "Critical"
    assert out["todo_list"]["title"] == "QA"
    assert ok(mcp, "pm.update_todo_list", {"todo_list": lst, "title": "QA round 1"})["todo_list"][
        "title"
    ] == ("QA round 1")
    assert error_code(mcp, "pm.update_todo_list", {"todo_list": lst, "priority": 8}) == SCHEMA

    task = ok(mcp, "pm.create_task", {"todo_list": lst, "description": "Smoke test", "assigned_to": 2})[
        "task"
    ]
    assert int(task["id"]) in _my_task_ids(mcp)
    assert ok(mcp, "pm.delete_todo_list", {"todo_list": lst})["todo_list"]["status"] == -1
    assert int(task["id"]) not in _my_task_ids(mcp)  # hidden with its list
    assert error_code(mcp, "pm.update_todo_list", {"todo_list": lst, "title": "back"}) == "not_found"
    assert error_code(mcp, "pm.check_task", {"task": task["handle"]}) == "not_found"
    # soft delete: both rows are still there, the item untouched (decision 0003)
    assert row(tenant, "pm", "SELECT status FROM todo_lists WHERE id = ?", (project["id"],))["status"] == -1
    assert (
        row(tenant, "pm", "SELECT status FROM todo_list_items WHERE id = ?", (task["id"],))["status"] == 10000
    )


def test_list_rights(writer: Callable[..., MCPServer]) -> None:
    bob = writer("bob")
    assert error_code(bob, "pm.create_todo_list", {"title": "x"}) == "not_permitted"
    assert error_code(bob, "pm.update_todo_list", {"todo_list": "pm:todo_list:1", "title": "x"}) == (
        "not_permitted"
    )
    assert error_code(bob, "pm.delete_todo_list", {"todo_list": "pm:todo_list:1"}) == "not_permitted"


# ---------------------------------------------------------------- reminders


def test_reminder_lifecycle(writer: Callable[..., MCPServer], tenant: Path) -> None:
    mcp = writer()
    r = ok(
        mcp,
        "personal.add_reminder",
        {"title": "Call the accountant", "due_date": "2026-11-05", "category": "work", "waiting_on": "Per"},
    )["reminder"]
    assert r["handle"].startswith("personal:waiting:")
    assert r["status"] == 10000
    stored = row(
        tenant,
        "personal",
        "SELECT owner_id, owner, guid, sort_order FROM personal_waitings WHERE id = ?",
        (r["id"],),
    )
    assert (stored["owner_id"], stored["owner"]) == (2, "Alice Andersen")
    assert stored["guid"]
    assert stored["sort_order"] == 1
    assert any(i.get("handle") == r["handle"] for i in ok(mcp, "personal.my_day")["this_week"])

    h = r["handle"]
    assert ok(mcp, "personal.rename_reminder", {"reminder": h, "title": "Call Per"})["reminder"]["title"] == (
        "Call Per"
    )
    out = ok(mcp, "personal.set_reminder_status", {"reminder": h, "status": 20000})["reminder"]
    assert out["status_label"] == "In Progress"
    assert error_code(mcp, "personal.set_reminder_status", {"reminder": h, "status": -1}) == SCHEMA
    assert error_code(mcp, "personal.set_reminder_status", {"reminder": h, "status": 12345}) == SCHEMA
    assert error_code(mcp, "personal.add_reminder", {"title": "x", "category": "tomorrow"}) == SCHEMA

    assert ok(mcp, "personal.delete_reminder", {"reminder": h})["reminder"]["status"] == -1
    assert not any(i.get("handle") == h for i in ok(mcp, "personal.my_day")["this_week"])
    assert error_code(mcp, "personal.rename_reminder", {"reminder": h, "title": "again"}) == "not_found"
    assert error_code(mcp, "personal.delete_reminder", {"reminder": h}) == "not_found"
    assert (
        row(tenant, "personal", "SELECT status FROM personal_waitings WHERE id = ?", (r["id"],))["status"]
        == -1
    )


def test_reminders_are_owner_only(writer: Callable[..., MCPServer], tenant: Path) -> None:
    for user in ("alice", "admin"):  # no administrator override (MPC001-243)
        mcp = writer(user)
        assert error_code(
            mcp, "personal.rename_reminder", {"reminder": "personal:waiting:2", "title": "x"}
        ) == ("not_found")
        assert error_code(mcp, "personal.delete_reminder", {"reminder": "personal:waiting:2"}) == "not_found"
    assert row(tenant, "personal", "SELECT title, status FROM personal_waitings WHERE id = 2", ())[0] == (
        "Bob's private wait"
    )
    # bob holds no pm write rights but owns his reminders: no webos right is needed (decision 0003)
    is_error, _ = call(
        writer("bob"), "personal.set_reminder_status", {"reminder": "personal:waiting:2", "status": 70000}
    )
    assert not is_error
