"""Prototype: a minimal read-only MCP server over the webos project-management database (pm.s3db).

Throwaway exploration code for cerp, not part of the package. Same approach as im_server.py. Follows
docs/webos/pm/pm.INSTRUCTIONS.md (read-only connections when looking, bound parameters) and the code
meanings in docs/webos/pm/pm.md section 3. It opens the database read-only (MPC001-162), builds SQL
only from fixed identifiers with bound values (MPC001-065), and never points at the templates in
data/template (it serves the copy next to this file by default).

Run from the cerp repository root:

    uv run python workspace/prototype/pm_server.py              # stdio (Claude Desktop / Claude Code)
    uv run python workspace/prototype/pm_server.py --http       # http://127.0.0.1:7413/mcp
    uv run python workspace/prototype/pm_server.py --db other.s3db

Add to Claude Code:

    claude mcp add pm-proto -- uv run --directory <cerp> python workspace/prototype/pm_server.py
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

DEFAULT_DB = Path(__file__).with_name("pm.s3db")
MAX_LIMIT = 100
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Code labels from lang.en.s3db, the authority (docs/webos/lang/lang.md, docs/webos/pm/pm.md section 3).
# OPTIONS.PROJECT.STATUS, stored in projects.stage.
STAGE = {
    100000: "New",
    110000: "Not Started",
    200000: "In Progress",
    500001: "Planned",
    600000: "Deferred",
    610000: "Cancelled",
    700000: "Completed",
    900000: "Closed",
}
# OPTIONS.TASK.STATUS, for activities, milestones, to-do lists and their items.
STATUS = {
    10000: "New",
    11000: "Not Started",
    20000: "In Progress",
    40000: "Waiting for Client",
    41000: "Waiting for Information",
    50000: "Unplanned",
    60000: "Deferred",
    61000: "Cancelled",
    70000: "Completed",
}
PRIORITY = {9: "Critical", 7: "High", 5: "Medium", 1: "Low"}
LABELS = {"stage": STAGE, "status": STATUS, "priority": PRIORITY}

# Columns returned in project search results; the full row comes from pm.get_project.
# "return" is a reserved word and must be quoted (pm.md section 9.5); it is not in this list.
SUMMARY_COLUMNS = (
    "id",
    "name",
    "stage",
    "owner",
    "parent_id",
    "budget",
    "available_funds",
    "creation_date",
    "start_date",
    "expected_end_date",
    "end_date",
    "next_action",
)


# ---------------------------------------------------------------- data access


class Database:
    """Read-only access to one pm.s3db file. One short-lived connection per call."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"database not found: {path}")
        self.path = path.resolve()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        return con

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [_clean(dict(r)) for r in con.execute(sql, params)]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        found = self.rows(sql, params)
        return found[0] if found else None


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    # webos stores some empty values as the literal string 'NULL' (MPC002-047).
    return {k: (None if v == "NULL" else v) for k, v in row.items()}


def _label(row: dict[str, Any]) -> dict[str, Any]:
    """Add *_label fields to a project, activity, milestone, to-do list or item row."""
    for column, names in LABELS.items():
        if column in row and row[column] is not None:
            try:
                row[f"{column}_label"] = names.get(int(row[column]), "unknown")
            except (TypeError, ValueError):
                # todo_list_items.status has a corrupt text default (pm.md section 9.1)
                row[f"{column}_label"] = "unknown"
    return row


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------- result models


class ProjectPage(BaseModel):
    total: int = Field(description="Number of projects matching the filters")
    offset: int
    limit: int
    projects: list[dict[str, Any]]


class Charges(BaseModel):
    hours: float = Field(description="Sum of time_tracks.hours")
    spent: float = Field(description="Sum of expenses.amount")
    remaining: float | None = Field(description="budget - spent, if the project has a budget")


class ProjectDetail(BaseModel):
    project: dict[str, Any]
    parent: dict[str, Any] | None
    subprojects: list[dict[str, Any]]
    activities: list[dict[str, Any]]
    milestones: list[dict[str, Any]]
    todo_lists: list[dict[str, Any]]
    charges: Charges


class TodoListDetail(BaseModel):
    todo_list: dict[str, Any]
    items: list[dict[str, Any]]


class Count(BaseModel):
    value: Any
    label: str | None = None
    count: int


class ProjectStats(BaseModel):
    projects: int
    by_stage: list[Count]
    tasks_by_status: list[Count] = Field(description="To-do list items by status")
    overdue_milestones: list[dict[str, Any]]
    budget_vs_spent: list[dict[str, Any]]


def _charges(db: Database, project_id: int, budget: float | None) -> Charges:
    """Hours and money charged to a project (neither table is indexed, pm.md section 9.4)."""
    hours = db.one("SELECT IFNULL(SUM(hours), 0) AS n FROM time_tracks WHERE project_id = ?", (project_id,))
    spent = db.one("SELECT IFNULL(SUM(amount), 0) AS n FROM expenses WHERE project_id = ?", (project_id,))
    spent_total = float(spent["n"]) if spent else 0.0
    return Charges(
        hours=float(hours["n"]) if hours else 0.0,
        spent=spent_total,
        remaining=(float(budget) - spent_total) if budget is not None else None,
    )


# ---------------------------------------------------------------- server


def build_server(db: Database) -> MCPServer:
    mcp = MCPServer(
        name="pm-prototype",
        title="webos projects (prototype)",
        instructions=(
            "Read-only access to a webos project-management database: projects (which can nest through "
            "parent_id), their activities, milestones, to-do lists and items, and the hours and expenses "
            "charged to them. Codes are returned with a *_label. projects.stage is the project status "
            "(100000 New, 110000 Not Started, 200000 In Progress, 500001 Planned, 600000 Deferred, "
            "610000 Cancelled, 700000 Completed, 900000 Closed). Activities, milestones, lists and items "
            "use the task status (10000 New, 11000 Not Started, 20000 In Progress, 40000 Waiting for "
            "Client, 41000 Waiting for Information, 50000 Unplanned, 60000 Deferred, 61000 Cancelled, "
            "70000 Completed). Codes without a label are labelled 'unknown'. Use pm.project_stats for an "
            "overview, pm.search_projects to find projects, pm.get_project for one project's work "
            "breakdown and pm.get_todo_list for a list's items."
        ),
    )

    @mcp.tool(name="pm.search_projects", title="Search projects", annotations=READ_ONLY)
    def search_projects(
        *,
        text: str | None = None,
        stage: int | None = None,
        owner: str | None = None,
        parent_id: int | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> ProjectPage:
        """Find projects, newest first, with counts of their activities, milestones and to-do lists.

        text: words to look for in the project name or description (case-insensitive substring).
        stage: 100000 New, 110000 Not Started, 200000 In Progress, 500001 Planned, 600000 Deferred,
            610000 Cancelled, 700000 Completed, 900000 Closed.
        owner: owner name, matched as a case-insensitive substring.
        parent_id: only sub-projects of this project; 0 for top-level projects only.
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        where: list[str] = []
        params: list[Any] = []
        if stage is not None:
            where.append("p.stage = ?")
            params.append(stage)
        if parent_id is not None:
            where.append("IFNULL(p.parent_id, 0) = ?")
            params.append(parent_id)
        if text:
            where.append("(p.name LIKE ? ESCAPE '\\' OR p.description LIKE ? ESCAPE '\\')")
            params += [_like(text), _like(text)]
        if owner:
            where.append("p.owner LIKE ? ESCAPE '\\'")
            params.append(_like(owner))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        total = db.one(f"SELECT COUNT(*) AS n FROM projects p{clause}", tuple(params))  # noqa: S608
        page = db.rows(
            f"SELECT {', '.join('p.' + c for c in SUMMARY_COLUMNS)}, "  # noqa: S608
            "(SELECT COUNT(*) FROM project_activities a WHERE a.project_id = p.id) AS activities, "
            "(SELECT COUNT(*) FROM milestones m WHERE m.project_id = p.id) AS milestones, "
            "(SELECT COUNT(*) FROM todo_lists l WHERE l.project_id = p.id) AS todo_lists "
            f"FROM projects p{clause} ORDER BY p.creation_date DESC, p.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return ProjectPage(
            total=total["n"] if total else 0, offset=offset, limit=limit, projects=[_label(p) for p in page]
        )

    @mcp.tool(name="pm.get_project", title="Get project", annotations=READ_ONLY)
    def get_project(project_id: int) -> ProjectDetail:
        """Return one project with its parent, sub-projects, activities, milestones, to-do lists and
        the hours and money charged to it. Items of a to-do list come from pm.get_todo_list."""
        project = db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if project is None:
            raise ToolError(f"project {project_id} not found")
        return ProjectDetail(
            project=_label(project),
            parent=(
                db.one("SELECT id, name, stage FROM projects WHERE id = ?", (project["parent_id"],))
                if project.get("parent_id")
                else None
            ),
            subprojects=[
                _label(p)
                for p in db.rows(
                    "SELECT id, name, stage, owner FROM projects WHERE parent_id = ? ORDER BY name",
                    (project_id,),
                )
            ],
            activities=[
                _label(a)
                for a in db.rows(
                    "SELECT id, description, owner, status, category, start_date, end_date, budgeted_hours "
                    "FROM project_activities WHERE project_id = ? ORDER BY start_date, id",
                    (project_id,),
                )
            ],
            milestones=[
                _label(m)
                for m in db.rows(
                    "SELECT id, title, owner, status, category, due_date, description "
                    "FROM milestones WHERE project_id = ? ORDER BY due_date, id",
                    (project_id,),
                )
            ],
            todo_lists=[
                _label(entry)
                for entry in db.rows(
                    "SELECT l.id, l.title, l.project_activity_id, l.owner, l.status, l.priority, "
                    "l.completion, l.start_date, l.end_date, COUNT(i.id) AS items "
                    "FROM todo_lists l LEFT JOIN todo_list_items i ON i.todo_list_id = l.id "
                    "WHERE l.project_id = ? GROUP BY l.id ORDER BY l.sort_order, l.id",
                    (project_id,),
                )
            ],
            charges=_charges(db, project_id, project.get("budget")),
        )

    @mcp.tool(name="pm.get_todo_list", title="Get to-do list", annotations=READ_ONLY)
    def get_todo_list(todo_list_id: int) -> TodoListDetail:
        """Return one to-do list with its items in order."""
        todo_list = db.one("SELECT * FROM todo_lists WHERE id = ?", (todo_list_id,))
        if todo_list is None:
            raise ToolError(f"to-do list {todo_list_id} not found")
        items = db.rows(
            "SELECT id, description, status, assigned_to, owner, due_date, category, comments, "
            "budgeted, estimated, billed, creation_time "
            "FROM todo_list_items WHERE todo_list_id = ? ORDER BY sort_order, id",
            (todo_list_id,),
        )
        return TodoListDetail(todo_list=_label(todo_list), items=[_label(i) for i in items])

    @mcp.tool(name="pm.project_stats", title="Project statistics", annotations=READ_ONLY)
    def project_stats() -> ProjectStats:
        """Count projects by stage and tasks by status, list open milestones past their due date, and
        compare each project's budget with what has been spent."""

        def by(table: str, column: str) -> list[Count]:
            # table and column are fixed names below, never caller input
            sql = f"SELECT {column} AS value, COUNT(*) AS n FROM {table} GROUP BY {column} ORDER BY n DESC"  # noqa: S608
            names: dict[int, str] = LABELS.get(column, {})
            counts = []
            for r in db.rows(sql):
                try:
                    label = names.get(int(r["value"])) if r["value"] is not None else None
                except (TypeError, ValueError):
                    label = None
                counts.append(Count(value=r["value"], label=label, count=r["n"]))
            return counts

        total = db.one("SELECT COUNT(*) AS n FROM projects")
        overdue = db.rows(
            "SELECT m.id, m.project_id, p.name AS project, m.title, m.due_date, m.status, "
            "CAST(julianday('now', 'localtime') - julianday(m.due_date) AS INTEGER) AS days_overdue "
            "FROM milestones m LEFT JOIN projects p ON p.id = m.project_id "
            "WHERE m.due_date IS NOT NULL AND julianday(m.due_date) < julianday('now', 'localtime') "
            "AND IFNULL(m.status, 0) NOT IN (61000, 70000) ORDER BY days_overdue DESC LIMIT ?",
            (MAX_LIMIT,),
        )
        budget = db.rows(
            "SELECT p.id, p.name, p.stage, p.budget, "
            "ROUND(IFNULL((SELECT SUM(hours) FROM time_tracks t WHERE t.project_id = p.id), 0), 2) AS hours, "
            "ROUND(IFNULL((SELECT SUM(amount) FROM expenses e WHERE e.project_id = p.id), 0), 2) AS spent "
            "FROM projects p ORDER BY spent DESC, p.id LIMIT ?",
            (MAX_LIMIT,),
        )
        return ProjectStats(
            projects=total["n"] if total else 0,
            by_stage=by("projects", "stage"),
            tasks_by_status=by("todo_list_items", "status"),
            overdue_milestones=[_label(m) for m in overdue],
            budget_vs_spent=[_label(b) for b in budget],
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal read-only MCP server over pm.s3db (prototype)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="database file (default: pm.s3db here)")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=7413)
    args = parser.parse_args()

    server = build_server(Database(args.db))
    if args.http:
        server.run("streamable-http", host="127.0.0.1", port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
