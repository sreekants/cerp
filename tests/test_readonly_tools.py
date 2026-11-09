"""The read-only decision-support tools, end to end through the MCP client.

Covers MPC001-208, -213, -217, -219 (pm), -240, -245 (personal), -252, -253, -256, -268, -269, -272, -276
(crm), and the security they rest on: principal mapping (MPC001-060), the authorization port (MPC001-061),
personal privacy (MPC001-243) and signed cursors (MPC001-023). Runs against the synthetic tenant in
``tests/mvp_tenant.py`` with the clock fixed at Wednesday 2026-11-04 10:00.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError

from cerp.app.asgi import build_app
from cerp.app.config import ConfigError, Settings
from cerp.app.server import StartupError, build_runtime
from cerp.registry.generate import build, render
from tests.mvp_tenant import NOW, build_security, seed_business
from tests.test_args import _strings_without_bound

MVP = Path(__file__).resolve().parent / "fixtures" / "webos-mvp"
ALL_PACKS = ["pm", "crm", "personal"]


@pytest.fixture
def tenant(dbroot: Path) -> Path:
    build_security(dbroot)
    seed_business(dbroot)
    return dbroot


@pytest.fixture
def mvp_registry(tmp_path: Path, tenant: Path) -> Path:
    path = tmp_path / "mvp-registry.yaml"
    path.write_bytes(render(build(MVP / "portal", MVP / "datasources.xml", tenant)))
    return path


def _settings(registry: Path, dbroot: Path, user: str | None, packs: list[str]) -> Settings:
    auth: dict[str, Any] = {"local_user": user} if user else {}
    return Settings.model_validate(
        {
            "registry": {"path": str(registry)},
            "data": {"dbroot": str(dbroot)},
            "auth": auth,
            "tools": {"packs": packs},
        }
    )


@pytest.fixture
def server(mvp_registry: Path, tenant: Path) -> Callable[..., MCPServer]:
    def make(user: str | None = "alice", packs: list[str] | None = None) -> MCPServer:
        mcp, _ = build_runtime(
            _settings(mvp_registry, tenant, user, packs or ALL_PACKS), {}, clock=lambda: NOW
        )
        return mcp

    return make


def call(mcp: MCPServer, tool: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
    async def go() -> tuple[bool, Any]:
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args or {})
            return bool(r.is_error), r.structured_content

    return anyio.run(go)


def ok(mcp: MCPServer, tool: str, args: dict[str, Any] | None = None) -> Any:
    is_error, content = call(mcp, tool, args)
    assert not is_error, content
    return content


def error_code(mcp: MCPServer, tool: str, args: dict[str, Any] | None = None) -> str:
    is_error, content = call(mcp, tool, args)
    assert is_error, content
    return str(content["error"]["code"])


def tools(mcp: MCPServer) -> list[Any]:
    async def go() -> list[Any]:
        async with Client(mcp) as c:
            return list((await c.list_tools()).tools)

    return anyio.run(go)


def _ids(rows: list[dict[str, Any]]) -> list[int]:
    return [int(r["id"]) for r in rows]


# ---------------------------------------------------------------- catalogue and startup


EXPECTED = {
    "pm.search_projects", "pm.get_project", "pm.my_tasks", "pm.workload", "pm.board", "pm.timeline",
    "crm.search_accounts", "crm.get_account", "crm.find_contact", "crm.find_account", "crm.pipeline",
    "crm.forecast", "crm.list_sales_teams", "crm.timeline", "personal.my_day", "personal.weekly_review",
}  # fmt: skip


def test_every_tool_is_read_only_with_bounded_strings(server: Callable[..., MCPServer]) -> None:
    listed = tools(server())
    assert {t.name for t in listed} == EXPECTED
    for t in listed:
        assert t.annotations is not None
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name
        assert list(_strings_without_bound(t.input_schema)) == [], t.name
        assert t.description, t.name


def test_packs_need_a_registry(tenant: Path) -> None:
    s = Settings.model_validate({"data": {"dbroot": str(tenant)}, "tools": {"packs": ["pm"]}})
    with pytest.raises(ConfigError, match=r"tools\.packs"):
        build_runtime(s, {})


def test_packs_need_the_security_database(mvp_registry: Path, tenant: Path) -> None:
    (tenant / "security.s3db").unlink()
    with pytest.raises(StartupError, match="security"):
        build_runtime(_settings(mvp_registry, tenant, "alice", ["pm"]), {})


def test_packs_refuse_schema_drift(mvp_registry: Path, tenant: Path) -> None:
    con = sqlite3.connect(tenant / "crm.s3db")
    con.execute("ALTER TABLE sales_teams RENAME COLUMN name TO team_name")
    con.commit()
    con.close()
    with pytest.raises(StartupError, match=r"crm\.sales_teams: missing columns name"):
        build_runtime(_settings(mvp_registry, tenant, "alice", ["crm"]), {})


def test_local_user_is_refused_over_http(mvp_registry: Path, tenant: Path) -> None:
    with pytest.raises(ConfigError, match="local_user"):
        build_app(_settings(mvp_registry, tenant, "alice", ["pm"]), {})


def test_local_user_cannot_be_combined_with_http_auth() -> None:
    with pytest.raises(Exception, match="local_user"):
        Settings.model_validate(
            {
                "auth": {
                    "local_user": "alice",
                    "static_tokens": {"enabled": True, "tokens": [{"token_env": "T", "user": "alice"}]},
                }
            }
        )


# ---------------------------------------------------------------- who may call (MPC001-060, -061)


def test_no_caller_is_refused(server: Callable[..., MCPServer]) -> None:
    assert error_code(server(user=None), "pm.my_tasks") == "not_permitted"


@pytest.mark.parametrize("user", ["carol", "ghost"])
def test_disabled_or_unknown_users_are_refused(server: Callable[..., MCPServer], user: str) -> None:
    assert error_code(server(user=user), "pm.search_projects") == "not_permitted"


def test_a_user_without_rights_is_refused_and_records_look_missing(server: Callable[..., MCPServer]) -> None:
    mcp = server(user="nobody")
    assert error_code(mcp, "pm.search_projects") == "not_permitted"
    assert error_code(mcp, "pm.get_project", {"project": "pm:project:1"}) == "not_found"
    assert error_code(mcp, "crm.get_account", {"account": "crm:account:1001"}) == "not_found"


def test_substring_rights_do_not_count(server: Callable[..., MCPServer]) -> None:
    # bob holds "Account.ViewAll", which webos's substring lookup would accept as Account.View (WOS-BUG-035)
    assert error_code(server(user="bob"), "crm.search_accounts") == "not_permitted"


def test_administrator_passes_like_webos(
    server: Callable[..., MCPServer], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING", logger="cerp.audit")
    page = ok(server(user="admin"), "crm.search_accounts")
    assert _ids(page["rows"]) == [1001, 1002]
    assert any("administrator bypass" in r.getMessage() for r in caplog.records)


def test_sections_without_rights_are_left_out(server: Callable[..., MCPServer]) -> None:
    # bob reads projects and tasks but not milestones, phases, time or expenses
    view = ok(server(user="bob"), "pm.get_project", {"project": "pm:project:1"})
    assert view["tasks"] is not None
    assert view["milestones"] is None
    assert view["phases"] is None
    assert view["charges"]["hours_logged"] is None
    assert view["charges"]["spent"] is None


# ---------------------------------------------------------------- pm


def test_search_projects_filters_and_labels(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    page = ok(mcp, "pm.search_projects", {"open_only": True, "sort": "name"})
    assert [p["name"] for p in page["projects"]] == ["Riverside", "Website rebuild"]
    assert page["projects"][1]["stage_label"] == "In Progress"
    assert page["projects"][1]["account"] == "crm:account:1001"
    assert _ids(ok(mcp, "pm.search_projects", {"account": "crm:account:1001"})["projects"]) == [1]
    assert _ids(ok(mcp, "pm.search_projects", {"parent": "pm:project:1"})["projects"]) == [2]
    assert _ids(ok(mcp, "pm.search_projects", {"starts_from": "2026-11-01"})["projects"]) == [3]
    # LIKE wildcards in the search text are literal: "_" and "%" do not match anything else
    assert _ids(ok(mcp, "pm.search_projects", {"text": "Data_migration 100%"})["projects"]) == [3]
    assert ok(mcp, "pm.search_projects", {"text": "Data%"})["projects"] == []


def test_search_projects_pages_with_signed_cursors(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    first = ok(mcp, "pm.search_projects", {"limit": 2})
    assert len(first["projects"]) == 2
    second = ok(mcp, "pm.search_projects", {"limit": 2, "cursor": first["next_cursor"]})
    assert second["next_cursor"] is None
    assert {p["id"] for p in first["projects"]} | {p["id"] for p in second["projects"]} == {1, 2, 3}


def test_cursor_from_another_query_or_tampered_is_rejected(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    cursor = ok(mcp, "pm.search_projects", {"limit": 1})["next_cursor"]
    tampered = cursor[:-2] + ("A" if cursor[-2] != "A" else "B") + cursor[-1]

    async def replay() -> list[int]:
        codes = []
        async with Client(mcp) as c:
            for args in ({"limit": 1, "stage": 200000, "cursor": cursor}, {"limit": 1, "cursor": tampered}):
                with pytest.raises(MCPError) as exc:
                    await c.call_tool("pm.search_projects", args)
                codes.append(exc.value.error.code)
        return codes

    assert anyio.run(replay) == [-32602, -32602]


def test_get_project_overview(server: Callable[..., MCPServer]) -> None:
    view = ok(server(), "pm.get_project", {"project": "pm:project:1"})
    assert view["as_of"] == "2026-11-04"
    assert [p["name"] for p in view["subprojects"]] == ["Old intranet"]
    tasks = view["tasks"]
    # 6 visible tasks (the deleted one hidden): 1 completed, 1 cancelled, 4 open (incl. the text '0' status)
    assert (tasks["total"], tasks["completed"], tasks["cancelled"], tasks["open"]) == (6, 1, 1, 4)
    assert tasks["percent_complete"] == pytest.approx(20.0)
    assert _ids(tasks["overdue"]) == [2]
    assert _ids(tasks["due_this_week"]) == [5, 3]
    ms = view["milestones"]
    assert [m["title"] for m in ms["missed"]] == ["Beta"]
    assert ms["next_due"]["title"] == "Launch"
    assert [m["title"] for m in ms["at_risk"]] == ["Launch"]
    assert view["charges"] == {"hours_logged": 8.0, "spent": 2500.0, "budget_remaining": 7500.0}
    corrupt = next(t for t in ms["milestones"] if t["title"] == "Beta")
    assert corrupt["status_label"] == "In Progress"


def test_bad_handle_is_a_validation_error(server: Callable[..., MCPServer]) -> None:
    assert error_code(server(), "pm.get_project", {"project": "crm:account:1"}) == "validation"
    assert error_code(server(), "pm.get_project", {"project": "pm:project:999"}) == "not_found"


def test_my_tasks_groups_and_privacy(server: Callable[..., MCPServer]) -> None:
    mine = ok(server(), "pm.my_tasks")
    assert _ids(mine["overdue"]) == [2]
    assert _ids(mine["today"]) == [6]  # alice's personal list
    assert _ids(mine["this_week"]) == [3]
    assert _ids(mine["later"]) == [9]
    everything = [t["id"] for k in ("overdue", "today", "this_week", "later", "no_date") for t in mine[k]]
    assert 7 not in everything  # bob's personal task never shows up for alice
    assert 8 not in everything  # deleted
    bobs = ok(server(user="bob"), "pm.my_tasks")
    assert _ids(bobs["today"]) == [5, 7]
    assert bobs["today"][0]["status_label"] == "unknown"  # the corrupt text '0' status


def test_workload_by_person_and_week(server: Callable[..., MCPServer]) -> None:
    load = ok(server(), "pm.workload", {"weeks": 2})
    assert load["weeks"] == ["2026-11-02", "2026-11-09"]
    assert load["capacity_known"] is False
    alice = next(p for p in load["people"] if p["user_id"] == 2)
    assert alice["user"] == "Alice Andersen"
    assert [(w["estimated_due"], w["hours_logged"]) for w in alice["weeks"]] == [(12.0, 5.0), (3.0, 0.0)]
    bob = next(p for p in load["people"] if p["user_id"] == 3)
    assert bob["weeks"][0]["estimated_due"] == 0.0  # corrupt text estimate counts as 0
    assert all(p["user_id"] != 0 for p in load["people"])  # personal lists are not workload


def test_board_and_timeline(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    board = ok(mcp, "pm.board", {"project": "pm:project:1"})
    columns = {c["key"]: c for c in board["columns"]}
    assert _ids(columns["20000"]["tasks"]) == [2]
    assert columns["'0'"]["label"] == "unknown"  # the corrupt text status gets its own column
    by_phase = ok(mcp, "pm.board", {"project": "pm:project:1", "group_by": "phase"})
    assert by_phase["columns"][0]["label"] == "Design"
    tl = ok(mcp, "pm.timeline", {"project": "pm:project:1"})
    assert [m["title"] for m in tl["milestones"]] == ["Kickoff", "Beta", "Launch"]
    assert tl["start"].startswith("2026-10-01")
    assert all(t["due_date"] for t in tl["dated_tasks"])


# ---------------------------------------------------------------- crm


def test_search_accounts(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    page = ok(mcp, "crm.search_accounts", {"text": "acme"})
    assert _ids(page["rows"]) == [1001]
    assert page["rows"][0]["type_label"] == "Private Company"
    assert page["rows"][0]["status_label"] == "Active"
    assert _ids(ok(mcp, "crm.search_accounts", {"type": 1})["rows"]) == [1002]  # 1003 is soft-deleted


def test_customer_360(server: Callable[..., MCPServer]) -> None:
    view = ok(server(), "crm.get_account", {"account": "crm:account:1001"})
    assert view["primary_contact"]["first_name"] == "Kari"
    for c in view["contacts"]:
        assert "home_phone" not in c
        assert "home_address" not in c
    assert _ids(view["open_opportunities"]) == [1001, 1002]
    assert view["open_opportunities"][1]["probability"] == 0.25  # stored as a percentage
    assert view["quotes"][0]["computed_total"] == 250.0
    assert _ids(view["open_tickets"]) == [1001]
    assert [p["name"] for p in view["projects"]] == ["Website rebuild"]
    assert view["products_owned"][0]["handle"] == "crm:inventory_item:1001"
    assert any("receivables" in n for n in view["notes"])
    assert "555-HOME" not in str(view)


def test_find_contact_and_account(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    by_mail = ok(mcp, "crm.find_contact", {"email": "KARI@acme.no"})["candidates"]
    assert by_mail[0]["record"]["id"] == 1001
    assert by_mail[0]["score"] == 1.0
    assert "same email domain" in by_mail[1]["reasons"]  # ola@acme.no
    by_phone = ok(mcp, "crm.find_contact", {"phone": "0047 91234567"})["candidates"]
    assert by_phone[0]["record"]["id"] == 1001
    assert by_phone[0]["reasons"] == ["same phone number"]
    acct = ok(mcp, "crm.find_account", {"name": "ACME", "website": "acme.no/contact"})["candidates"]
    assert acct[0]["record"]["id"] == 1001
    assert set(acct[0]["reasons"]) == {"same name", "same website domain"}
    assert error_code(mcp, "crm.find_contact", {}) == "validation"


def test_pipeline(server: Callable[..., MCPServer]) -> None:
    pipe = ok(server(), "crm.pipeline", {"group_by": "stage"})
    assert pipe["open_count"] == 3
    assert pipe["open_amount"] == 65000.0
    assert pipe["weighted_amount"] == pytest.approx(50000 * 0.8 + 10000 * 0.25 + 5000 * 0.5)
    assert {g["label"] for g in pipe["groups"]} == {
        "Proposal & Price Quote",
        "Qualification",
        "Needs Analysis",
    }
    assert _ids(pipe["stale"]) == [1001]
    assert _ids(pipe["past_expected_close"]) == [1004]
    assert _ids(pipe["overdue_follow_ups"]) == [1001]  # the corrupt text status row is not "open"
    teams = ok(server(), "crm.pipeline", {"group_by": "team", "sales_team_id": 1})
    assert [(g["label"], g["count"]) for g in teams["groups"]] == [("Nordic", 2)]


def test_forecast(server: Callable[..., MCPServer]) -> None:
    fc = ok(server(), "crm.forecast", {"months": 2})
    periods = {p["period"]: p for p in fc["periods"]}
    assert list(periods) == ["2026-11", "2026-12"]
    assert periods["2026-11"]["commit"] == 50000.0
    assert periods["2026-11"]["weighted"] == 40000.0
    assert periods["2026-12"]["best_case"] == 10000.0
    assert periods["2026-12"]["commit"] == 0.0
    assert "currency" in fc["note"]


def test_sales_teams(server: Callable[..., MCPServer]) -> None:
    teams = ok(server(), "crm.list_sales_teams")["teams"]
    assert teams[0]["name"] == "Nordic"
    assert teams[0]["members"] == [{"user_id": 2, "name": "Alice Andersen"}]


def test_timeline_pages_newest_first(server: Callable[..., MCPServer]) -> None:
    mcp = server()
    first = ok(mcp, "crm.timeline", {"account": "crm:account:1001", "limit": 3})
    whens = [e["when"] for e in first["events"]]
    assert whens == sorted(whens, reverse=True)
    assert first["next_cursor"]
    rest = ok(
        mcp, "crm.timeline", {"account": "crm:account:1001", "limit": 50, "cursor": first["next_cursor"]}
    )
    kinds = {e["kind"] for e in first["events"] + rest["events"]}
    assert kinds == {
        "activity",
        "opportunity created",
        "opportunity expected close",
        "quote",
        "ticket",
        "product bought",
    }
    contact = ok(mcp, "crm.timeline", {"contact": "crm:contact:1001"})
    assert [e["kind"] for e in contact["events"]] == ["activity"]
    assert error_code(mcp, "crm.timeline", {}) == "validation"


# ---------------------------------------------------------------- personal (MPC001-243)


def test_my_day_for_the_caller_only(server: Callable[..., MCPServer]) -> None:
    day = ok(server(), "personal.my_day")

    def kinds(bucket: str) -> list[tuple[str, int]]:
        return [(i["kind"], int(i["id"])) for i in day[bucket]]

    assert ("task", 2) in kinds("overdue")
    assert ("ticket", 1001) in kinds("overdue")
    assert ("follow-up", 1001) in kinds("overdue")
    assert ("task", 6) in kinds("today")
    assert ("waiting for", 1) in kinds("this_week")
    assert ("sales activity", 1001) in kinds("this_week")
    assert _ids(day["open_tickets_without_due_date"]) == [1003]
    assert "Bob" not in str(day)  # no other user's personal records
    assert day["not_shown"] == []


def test_my_day_leaves_out_what_the_caller_cannot_read(server: Callable[..., MCPServer]) -> None:
    day = ok(server(user="bob"), "personal.my_day")
    assert set(day["not_shown"]) == {"tickets", "CRM follow-ups", "sales activities"}
    assert [i["title"] for i in day["today"] if i["kind"] == "waiting for"] == ["Bob's private wait"]


def test_weekly_review(server: Callable[..., MCPServer]) -> None:
    review = ok(server(), "personal.weekly_review", {"stale_days": 14})
    assert review["next_week"] == "2026-11-09..2026-11-15"
    assert _ids(review["overdue_tasks"]) == [2]
    assert _ids(review["tasks_due_next_week"]) == [9]
    assert [m["title"] for m in review["milestones_next_week"]] == ["Launch"]
    assert [w["title"] for w in review["stale_waiting_fors"]] == ["Signed contract"]
    assert [m["title"] for m in review["old_maybes"]] == ["Learn Rust"]


def test_personal_records_are_never_shared_even_with_admin(server: Callable[..., MCPServer]) -> None:
    review = ok(server(user="admin"), "personal.weekly_review")
    assert review["stale_waiting_fors"] == []
    assert review["old_maybes"] == []


def test_kill_switch_hides_a_pack(mvp_registry: Path, tenant: Path) -> None:
    s = _settings(mvp_registry, tenant, "alice", ALL_PACKS).model_copy(
        update={"kill_switches": {"module:crm": True}}
    )
    mcp, _ = build_runtime(s, {}, clock=lambda: NOW)
    assert not [t for t in tools(mcp) if t.name.startswith("crm.")]


def test_audit_record_names_the_mapped_webos_user(
    server: Callable[..., MCPServer], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="cerp.audit")
    ok(server(), "pm.my_tasks")
    records = [
        r.getMessage() for r in caplog.records if r.name == "cerp.audit" and "pm.my_tasks" in r.getMessage()
    ]
    assert records
    assert '"webos_user":"alice"' in records[-1]


def test_board_and_timeline_show_every_open_task_exactly_once(server: Callable[..., MCPServer]) -> None:
    # MPC001-219 acceptance: both views return every open task of the seeded project exactly once
    mcp = server()
    open_ids = sorted(
        t["id"]
        for column in ok(mcp, "pm.board", {"project": "pm:project:1"})["columns"]
        for t in column["tasks"]
        if t["status_label"] not in ("Completed", "Cancelled")
    )
    assert open_ids == [2, 3, 5, 9]
    tl = ok(mcp, "pm.timeline", {"project": "pm:project:1"})
    timeline_open = sorted(
        t["id"]
        for t in tl["dated_tasks"] + tl["undated_tasks"]
        if t["status_label"] not in ("Completed", "Cancelled")
    )
    assert timeline_open == open_ids


def test_subprojects_name_their_parent(server: Callable[..., MCPServer]) -> None:
    view = ok(server(), "pm.get_project", {"project": "pm:project:1"})
    assert view["subprojects"][0]["parent"] == "pm:project:1"
