"""Read-only project tools: ``pm.*`` (MPC001-208, MPC001-213, MPC001-217, MPC001-219).

Projects live in ``pm.projects`` (the ``Project`` model is a JScript model, so it is read through these
curated queries, not the generic exposure, MPC001-164). Tasks are ``todo_list_items`` in ``todo_lists``;
phases are ``project_activities``. Codes are labelled from ``lang.en.s3db`` (``pm.md`` §3, MPC001-201).

Rules applied everywhere (``pm.md`` §4, §9; MPC001-207):

* a task is open unless its status is Completed (70000) or Cancelled (61000); status ``-1`` (deleted,
  MPC001-163) is never shown; ``todo_list_items.status`` may hold the corrupt text ``'0'`` (§9.1) and is
  then labelled ``unknown`` and treated as open;
* overdue and at-risk are computed from dates and statuses; the stored ``is_late`` / ``is_risk`` flags are
  not trusted (MPC001-208 requirement 3);
* task lists with no project (``project_id = 0``) are personal (MPC001-202 option a) and appear only in
  their owner's own views;
* every section is guarded by its own model's rights (MPC001-061): a caller without ``TimeTrack.View``
  sees no hours, and so on.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from cerp.protocol.args import HandleText, ShortText
from cerp.protocol.errors import NotFound
from cerp.tools.context import (
    CursorText,
    DateText,
    Guard,
    Reads,
    Registrar,
    ToolContext,
    handle,
    like,
    parse_day,
    record_id,
)
from cerp.tools.labels import (
    PRIORITY,
    PROJECT_STAGE,
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_DONE,
    TASK_STATUS,
    label,
    with_labels,
)

PROJECT: Final = Guard("projects", model_key="objects/projects/project")
PHASE: Final = Guard("project phases", model_key="objects/projects/project_activity")
MILESTONE: Final = Guard("milestones", model_key="objects/projects/milestone")
TASK_LIST: Final = Guard("task lists", model_key="objects/projects/todo_list")
TASK: Final = Guard("tasks", model_key="objects/projects/todo_list_item")
TIME: Final = Guard("time entries", model_key="objects/projects/time_track")
EXPENSE: Final = Guard("expenses", model_key="objects/projects/expense")
GUARDS: Final = (PROJECT, PHASE, MILESTONE, TASK_LIST, TASK, TIME, EXPENSE)

READS: Final[Reads] = {
    ("pm", "projects"): (
        "id",
        "name",
        "stage",
        "owner_id",
        "owner",
        "parent_id",
        "account_id",
        "team_id",
        "budget",
        "start_date",
        "expected_end_date",
        "end_date",
        "creation_date",
        "next_action",
        "description",
    ),
    ("pm", "project_activities"): (
        "id",
        "project_id",
        "owner",
        "description",
        "start_date",
        "end_date",
        "status",
        "budgeted_hours",
    ),
    ("pm", "milestones"): ("id", "project_id", "owner_id", "owner", "title", "due_date", "status"),
    ("pm", "todo_lists"): (
        "id",
        "project_id",
        "owner_id",
        "title",
        "status",
        "project_activity_id",
        "priority",
        "start_date",
        "end_date",
        "sort_order",
    ),
    ("pm", "todo_list_items"): (
        "id",
        "description",
        "todo_list_id",
        "status",
        "assigned_to",
        "due_date",
        "estimated",
        "sort_order",
    ),
    ("pm", "time_tracks"): ("id", "project_id", "owner_id", "owner", "hours", "logged_on"),
    ("pm", "expenses"): ("id", "project_id", "amount"),
}

LIMIT_SECTION: Final = 200  # rows per section of an overview

# ---------------------------------------------------------------- SQL (constant; values bound)

_SEARCH = """
SELECT p.id, p.name, p.stage, p.owner, p.parent_id, p.account_id, p.team_id, p.budget,
       p.start_date, p.expected_end_date, p.end_date, p.creation_date, p.next_action
FROM projects p
WHERE (:stage IS NULL OR p.stage = :stage)
  AND (:open_only = 0 OR IFNULL(p.stage, 0) NOT IN (610000, 700000, 900000))
  AND (:text IS NULL OR p.name LIKE :text ESCAPE '\\' OR p.description LIKE :text ESCAPE '\\')
  AND (:owned_by IS NULL OR p.owner LIKE :owned_by ESCAPE '\\')
  AND (:account_id IS NULL OR p.account_id = :account_id)
  AND (:parent_id IS NULL OR p.parent_id = :parent_id)
  AND (:top_level = 0 OR IFNULL(p.parent_id, 0) = 0)
  AND (:team_id IS NULL OR p.team_id = :team_id)
  AND (:starts_from IS NULL OR substr(p.start_date, 1, 10) >= :starts_from)
  AND (:starts_to IS NULL OR substr(p.start_date, 1, 10) <= :starts_to)
  AND (:ends_from IS NULL OR substr(IFNULL(p.end_date, p.expected_end_date), 1, 10) >= :ends_from)
  AND (:ends_to IS NULL OR substr(IFNULL(p.end_date, p.expected_end_date), 1, 10) <= :ends_to)
ORDER BY
  CASE WHEN :sort = 'name' THEN p.name END COLLATE NOCASE,
  CASE WHEN :sort = 'start' THEN p.start_date END,
  CASE WHEN :sort = 'end' THEN IFNULL(p.end_date, p.expected_end_date) END,
  CASE WHEN :sort = 'stage' THEN p.stage END,
  CASE WHEN :sort = 'created' THEN p.creation_date END DESC,
  p.id DESC
LIMIT :limit OFFSET :offset
"""
_PROJECT = """
SELECT id, name, stage, owner, owner_id, parent_id, account_id, team_id, budget, description,
       start_date, expected_end_date, end_date, creation_date, next_action
FROM projects WHERE id = ?
"""
_SUBPROJECTS = (
    "SELECT id, name, stage, owner, parent_id FROM projects WHERE parent_id = ? ORDER BY name, id LIMIT ?"
)
_PHASES = """
SELECT id, description, owner, status, start_date, end_date, budgeted_hours
FROM project_activities WHERE project_id = ? AND IFNULL(status, 0) <> -1
ORDER BY start_date, id LIMIT ?
"""
_MILESTONES = """
SELECT id, title, owner, status, due_date FROM milestones
WHERE project_id = ? AND IFNULL(status, 0) <> -1 ORDER BY due_date, id LIMIT ?
"""
_LISTS = """
SELECT id, title, status, priority, project_activity_id, start_date, end_date FROM todo_lists
WHERE project_id = ? AND IFNULL(status, 0) <> -1 ORDER BY sort_order, id LIMIT ?
"""
_PROJECT_TASKS = """
SELECT i.id, i.description, i.status, i.assigned_to, i.due_date, i.estimated, i.todo_list_id,
       l.title AS list_title, l.project_activity_id
FROM todo_list_items i JOIN todo_lists l ON l.id = i.todo_list_id
WHERE l.project_id = ? AND IFNULL(i.status, 0) <> -1 AND IFNULL(l.status, 0) <> -1
ORDER BY i.due_date, i.sort_order, i.id LIMIT ?
"""
_HOURS = "SELECT IFNULL(SUM(hours), 0) AS hours FROM time_tracks WHERE project_id = ?"
_SPENT = "SELECT IFNULL(SUM(amount), 0) AS spent FROM expenses WHERE project_id = ?"
_MY_TASKS = """
SELECT i.id, i.description, i.status, i.due_date, i.todo_list_id, l.title AS list_title, l.priority,
       l.project_id, p.name AS project_name
FROM todo_list_items i
JOIN todo_lists l ON l.id = i.todo_list_id
LEFT JOIN projects p ON p.id = l.project_id
WHERE IFNULL(i.status, 0) NOT IN (61000, 70000, -1) AND IFNULL(l.status, 0) <> -1
  AND (i.assigned_to = :user
       OR (IFNULL(l.project_id, 0) = 0 AND l.owner_id = :user AND IFNULL(i.assigned_to, 0) = 0))
ORDER BY i.due_date, i.id LIMIT 500
"""
_WORKLOAD_TASKS = """
SELECT i.assigned_to AS user_id, i.due_date,
       CASE WHEN typeof(i.estimated) IN ('integer', 'real') THEN i.estimated ELSE 0 END AS estimated
FROM todo_list_items i JOIN todo_lists l ON l.id = i.todo_list_id
WHERE IFNULL(i.status, 0) NOT IN (61000, 70000, -1) AND IFNULL(i.assigned_to, 0) > 0
  AND IFNULL(l.project_id, 0) > 0
  AND substr(i.due_date, 1, 10) BETWEEN :start AND :end
"""
_WORKLOAD_HOURS = """
SELECT owner_id AS user_id, owner, logged_on, hours FROM time_tracks
WHERE IFNULL(owner_id, 0) > 0 AND substr(logged_on, 1, 10) BETWEEN :start AND :end
"""


# ---------------------------------------------------------------- result models


class ProjectPage(BaseModel):
    projects: list[dict[str, Any]]
    next_cursor: str | None = Field(description="Pass to the same call for the next page; null at the end")


class TaskSummary(BaseModel):
    total: int
    open: int
    completed: int
    cancelled: int
    percent_complete: float | None = Field(description="Completed / (total - cancelled), in percent")
    overdue: list[dict[str, Any]] = Field(description="Open tasks past their due date")
    due_this_week: list[dict[str, Any]]


class MilestoneSummary(BaseModel):
    milestones: list[dict[str, Any]]
    next_due: dict[str, Any] | None
    missed: list[dict[str, Any]] = Field(description="Open milestones past their due date")
    at_risk: list[dict[str, Any]] = Field(description="Open milestones due in the next 7 days")


class Charges(BaseModel):
    hours_logged: float | None = None
    spent: float | None = None
    budget_remaining: float | None = None


class ProjectOverview(BaseModel):
    project: dict[str, Any]
    parent: dict[str, Any] | None
    subprojects: list[dict[str, Any]]
    phases: list[dict[str, Any]] | None = Field(default=None, description="Null if you cannot read phases")
    milestones: MilestoneSummary | None = None
    tasks: TaskSummary | None = None
    charges: Charges = Charges()
    as_of: str


class MyTasks(BaseModel):
    overdue: list[dict[str, Any]]
    today: list[dict[str, Any]]
    this_week: list[dict[str, Any]]
    later: list[dict[str, Any]]
    no_date: list[dict[str, Any]]
    as_of: str


class PersonWeek(BaseModel):
    week_start: str
    estimated_due: float = Field(description="Sum of `estimated` on open tasks due that week, as stored")
    hours_logged: float | None = Field(description="Hours logged that week; null if time is not visible")


class PersonLoad(BaseModel):
    user: str | None
    user_id: int
    weeks: list[PersonWeek]


class Workload(BaseModel):
    weeks: list[str]
    people: list[PersonLoad]
    capacity_known: bool = Field(default=False, description="False: no capacity data; this is load only")


class BoardColumn(BaseModel):
    key: str
    label: str | None
    tasks: list[dict[str, Any]]


class Board(BaseModel):
    project: str
    group_by: str
    columns: list[BoardColumn]


class Timeline(BaseModel):
    project: str
    start: str | None
    end: str | None
    phases: list[dict[str, Any]] | None
    milestones: list[dict[str, Any]] | None
    task_lists: list[dict[str, Any]] | None
    dated_tasks: list[dict[str, Any]] | None
    undated_tasks: list[dict[str, Any]] | None = Field(
        default=None, description="Tasks with no due date, so every task appears exactly once"
    )


# ---------------------------------------------------------------- helpers


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def _task(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("pm", "todo_list_item", row["id"])
    with_labels(row, status=TASK_STATUS)
    if "priority" in row:
        with_labels(row, priority=PRIORITY)
    return row


def _is_done(status: Any) -> bool:
    try:
        return int(status) in TASK_DONE
    except (TypeError, ValueError):
        return False


def _project_row(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("pm", "project", row["id"])
    row["parent"] = handle("pm", "project", row.pop("parent_id", None))
    row["account"] = handle("crm", "account", row.pop("account_id", None))
    return with_labels(row, stage=PROJECT_STAGE)


def open_tasks_for(ctx: ToolContext, user_id: int) -> list[dict[str, Any]]:
    """A user's open tasks: assigned to them, plus unassigned tasks in their personal lists (MPC001-202)."""
    out = []
    for r in ctx.rows("pm", _MY_TASKS, {"user": user_id}):
        t = _task(r)
        t["project"] = handle("pm", "project", t.pop("project_id", None))
        out.append(t)
    return out


def _load_project(ctx: ToolContext, project: str) -> tuple[int, dict[str, Any]]:
    caller = ctx.caller()
    ctx.require_record(caller, PROJECT, f"project {project}")
    pid = record_id(project, "pm", "project", "project")
    row = ctx.one("pm", _PROJECT, (pid,))
    if row is None:
        raise NotFound(f"project {project} not found")
    return pid, row


# ---------------------------------------------------------------- tools


def register(reg: Registrar, ctx: ToolContext) -> None:  # noqa: PLR0915 - one closure per tool
    def search_projects(
        *,
        text: ShortText | None = None,
        stage: int | None = None,
        open_only: bool = False,
        owned_by: ShortText | None = None,
        account: HandleText | None = None,
        parent: HandleText | None = None,
        top_level_only: bool = False,
        team_id: int | None = None,
        starts_from: DateText | None = None,
        starts_to: DateText | None = None,
        ends_from: DateText | None = None,
        ends_to: DateText | None = None,
        sort: Literal["created", "name", "start", "end", "stage"] = "created",
        limit: int = 25,
        cursor: CursorText | None = None,
    ) -> ProjectPage:
        """Find projects by words in the name or description, stage, owner name, customer account,
        parent project, team or start/end date range (YYYY-MM-DD). Stage codes: 100000 New, 110000 Not
        Started, 200000 In Progress, 500001 Planned, 600000 Deferred, 610000 Cancelled, 700000 Completed,
        900000 Closed; open_only excludes the last three. Page with `cursor` (limit 1-100)."""
        caller = ctx.caller()
        ctx.require(caller, PROJECT)
        args = {
            "text": text,
            "stage": stage,
            "open_only": open_only,
            "owned_by": owned_by,
            "account": account,
            "parent": parent,
            "top_level_only": top_level_only,
            "team_id": team_id,
            "starts_from": starts_from,
            "starts_to": starts_to,
            "ends_from": ends_from,
            "ends_to": ends_to,
            "sort": sort,
            "limit": limit,
        }
        size = ctx.page_size(limit)
        offset = ctx.offset("pm.search_projects", args, caller, cursor)
        rows = ctx.rows(
            "pm",
            _SEARCH,
            {
                "stage": stage,
                "open_only": int(open_only),
                "text": like(text) if text else None,
                "owned_by": like(owned_by) if owned_by else None,
                "account_id": record_id(account, "crm", "account", "account") if account else None,
                "parent_id": record_id(parent, "pm", "project", "parent") if parent else None,
                "top_level": int(top_level_only),
                "team_id": team_id,
                "starts_from": starts_from,
                "starts_to": starts_to,
                "ends_from": ends_from,
                "ends_to": ends_to,
                "sort": sort,
                "limit": size + 1,
                "offset": offset,
            },
        )
        return ProjectPage(
            projects=[_project_row(r) for r in rows[:size]],
            next_cursor=ctx.next_cursor(
                "pm.search_projects", args, caller, offset=offset, limit=size, fetched=len(rows)
            ),
        )

    reg(search_projects, "pm.search_projects", "Search projects")

    def get_project(project: HandleText) -> ProjectOverview:
        """One project (handle pm:project:<id>) with its parent and sub-projects, phases, milestones
        (next due, missed, at risk), task counts and % complete, overdue and due-this-week tasks, hours
        logged and money spent against the budget. Overdue and at-risk come from dates and statuses."""
        caller = ctx.caller()
        pid, row = _load_project(ctx, project)
        today = ctx.now().date()
        week_end = today + dt.timedelta(days=6 - today.weekday())
        parent = ctx.one("pm", _PROJECT, (row["parent_id"],)) if row.get("parent_id") else None
        out = ProjectOverview(
            project=_project_row(dict(row)),
            parent=_project_row(parent) if parent else None,
            subprojects=[_project_row(r) for r in ctx.rows("pm", _SUBPROJECTS, (pid, LIMIT_SECTION))],
            as_of=today.isoformat(),
        )
        if ctx.allowed(caller, PHASE):
            out.phases = [
                with_labels({**r, "handle": handle("pm", "project_activity", r["id"])}, status=TASK_STATUS)
                for r in ctx.rows("pm", _PHASES, (pid, LIMIT_SECTION))
            ]
        if ctx.allowed(caller, MILESTONE):
            ms = [
                with_labels({**r, "handle": handle("pm", "milestone", r["id"])}, status=TASK_STATUS)
                for r in ctx.rows("pm", _MILESTONES, (pid, LIMIT_SECTION))
            ]
            open_ms = [(parse_day(m["due_date"]), m) for m in ms if not _is_done(m["status"])]
            upcoming = sorted((d, m["id"], m) for d, m in open_ms if d is not None and d >= today)
            out.milestones = MilestoneSummary(
                milestones=ms,
                next_due=upcoming[0][2] if upcoming else None,
                missed=[m for d, m in open_ms if d is not None and d < today],
                at_risk=[m for d, m in open_ms if d is not None and today <= d <= today + dt.timedelta(7)],
            )
        if ctx.allowed(caller, TASK) and ctx.allowed(caller, TASK_LIST):
            tasks = [_task(r) for r in ctx.rows("pm", _PROJECT_TASKS, (pid, 5000))]
            done = sum(1 for t in tasks if _is_done(t["status"]) and int(t["status"]) == TASK_COMPLETED)
            cancelled = sum(1 for t in tasks if _is_done(t["status"]) and int(t["status"]) == TASK_CANCELLED)
            open_tasks = [t for t in tasks if not _is_done(t["status"])]
            countable = len(tasks) - cancelled
            out.tasks = TaskSummary(
                total=len(tasks),
                open=len(open_tasks),
                completed=done,
                cancelled=cancelled,
                percent_complete=round(100.0 * done / countable, 1) if countable else None,
                overdue=[t for t in open_tasks if (d := parse_day(t["due_date"])) and d < today][:50],
                due_this_week=[
                    t for t in open_tasks if (d := parse_day(t["due_date"])) and today <= d <= week_end
                ][:50],
            )
        charges = Charges()
        if ctx.allowed(caller, TIME):
            found = ctx.one("pm", _HOURS, (pid,))
            charges.hours_logged = float(found["hours"]) if found else 0.0
        if ctx.allowed(caller, EXPENSE):
            found = ctx.one("pm", _SPENT, (pid,))
            charges.spent = float(found["spent"]) if found else 0.0
            if row.get("budget") is not None:
                charges.budget_remaining = round(float(row["budget"]) - charges.spent, 2)
        out.charges = charges
        return out

    reg(get_project, "pm.get_project", "Project overview")

    def my_tasks() -> MyTasks:
        """Your own open tasks across all projects and your personal task lists, grouped as overdue,
        today, this week (to Sunday), later and no date, with project and list names."""
        caller = ctx.caller()
        ctx.require(caller, TASK)
        today = ctx.now().date()
        week_end = today + dt.timedelta(days=6 - today.weekday())
        out = MyTasks(overdue=[], today=[], this_week=[], later=[], no_date=[], as_of=today.isoformat())
        for t in open_tasks_for(ctx, caller.user_id):
            day = parse_day(t["due_date"])
            if day is None:
                out.no_date.append(t)
            elif day < today:
                out.overdue.append(t)
            elif day == today:
                out.today.append(t)
            elif day <= week_end:
                out.this_week.append(t)
            else:
                out.later.append(t)
        return out

    reg(my_tasks, "pm.my_tasks", "My tasks")

    def workload(weeks: int = 4, start: DateText | None = None) -> Workload:
        """Load per person and week across projects: the `estimated` value of open tasks due that week
        and the hours logged. Starts on the Monday of `start` (default this week) for 1-12 weeks. No
        capacity data exists yet, so this shows load only."""
        caller = ctx.caller()
        ctx.require(caller, TASK)
        n = max(1, min(weeks, 12))
        first = _week_start(dt.date.fromisoformat(start) if start else ctx.now().date())
        last = first + dt.timedelta(days=7 * n - 1)
        week_keys = [(first + dt.timedelta(days=7 * i)).isoformat() for i in range(n)]
        bounds = {"start": first.isoformat(), "end": last.isoformat()}
        est: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        hours: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for r in ctx.rows("pm", _WORKLOAD_TASKS, bounds):
            day = parse_day(r["due_date"])
            if day is not None:
                est[int(r["user_id"])][_week_start(day).isoformat()] += float(r["estimated"] or 0)
        see_time = ctx.allowed(caller, TIME)
        names: dict[int, str] = {}
        if see_time:
            for r in ctx.rows("pm", _WORKLOAD_HOURS, bounds):
                day = parse_day(r["logged_on"])
                if day is not None:
                    hours[int(r["user_id"])][_week_start(day).isoformat()] += float(r["hours"] or 0)
                    if r.get("owner"):
                        names[int(r["user_id"])] = str(r["owner"])
        people = sorted(set(est) | set(hours))
        names = {**names, **ctx.user_names(people)}
        return Workload(
            weeks=week_keys,
            people=[
                PersonLoad(
                    user=names.get(uid),
                    user_id=uid,
                    weeks=[
                        PersonWeek(
                            week_start=w,
                            estimated_due=round(est[uid][w], 2),
                            hours_logged=round(hours[uid][w], 2) if see_time else None,
                        )
                        for w in week_keys
                    ],
                )
                for uid in people
            ],
        )

    reg(workload, "pm.workload", "Workload by person")

    def board(project: HandleText, group_by: Literal["status", "phase"] = "status") -> Board:
        """A project's tasks as board columns, grouped by task status or by phase (project activity)."""
        caller = ctx.caller()
        pid, _ = _load_project(ctx, project)
        ctx.require(caller, TASK)
        ctx.require(caller, TASK_LIST)
        tasks = [_task(r) for r in ctx.rows("pm", _PROJECT_TASKS, (pid, 5000))]
        columns: dict[str, BoardColumn] = {}
        if group_by == "status":
            for code, status_name in TASK_STATUS.items():
                columns[str(code)] = BoardColumn(key=str(code), label=status_name, tasks=[])
            for t in tasks:
                key = str(t["status"])
                columns.setdefault(key, BoardColumn(key=key, label=label(TASK_STATUS, t["status"]), tasks=[]))
                columns[key].tasks.append(t)
        else:
            phases = (
                {r["id"]: r["description"] for r in ctx.rows("pm", _PHASES, (pid, LIMIT_SECTION))}
                if ctx.allowed(caller, PHASE)
                else {}
            )
            for t in tasks:
                aid = t.get("project_activity_id") or 0
                key = f"pm:project_activity:{aid}" if aid else "none"
                name: str | None = phases.get(aid) if aid else "No phase"
                columns.setdefault(key, BoardColumn(key=key, label=name, tasks=[]))
                columns[key].tasks.append(t)
        return Board(project=project, group_by=group_by, columns=list(columns.values()))

    reg(board, "pm.board", "Project board")

    def timeline(project: HandleText) -> Timeline:
        """A project's phases, milestones, task lists and tasks with their dates, in date order, for
        drawing a timeline; tasks with no due date are listed separately. Sections you cannot read are
        null."""
        caller = ctx.caller()
        pid, row = _load_project(ctx, project)
        phases = (
            [with_labels(dict(r), status=TASK_STATUS) for r in ctx.rows("pm", _PHASES, (pid, LIMIT_SECTION))]
            if ctx.allowed(caller, PHASE)
            else None
        )
        milestones = (
            [
                with_labels(dict(r), status=TASK_STATUS)
                for r in ctx.rows("pm", _MILESTONES, (pid, LIMIT_SECTION))
            ]
            if ctx.allowed(caller, MILESTONE)
            else None
        )
        lists = (
            [with_labels(dict(r), status=TASK_STATUS) for r in ctx.rows("pm", _LISTS, (pid, LIMIT_SECTION))]
            if ctx.allowed(caller, TASK_LIST)
            else None
        )
        dated: list[dict[str, Any]] | None = None
        undated: list[dict[str, Any]] | None = None
        if ctx.allowed(caller, TASK) and ctx.allowed(caller, TASK_LIST):
            tasks = [_task(r) for r in ctx.rows("pm", _PROJECT_TASKS, (pid, 5000))]
            dated = [t for t in tasks if t.get("due_date")]
            undated = [t for t in tasks if not t.get("due_date")]
        return Timeline(
            project=project,
            start=row.get("start_date"),
            end=row.get("end_date") or row.get("expected_end_date"),
            phases=phases,
            milestones=milestones,
            task_lists=lists,
            dated_tasks=dated,
            undated_tasks=undated,
        )

    reg(timeline, "pm.timeline", "Project timeline")
