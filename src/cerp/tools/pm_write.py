"""To-do list and task write tools: ``pm.*`` (MPC001-212, decision 0003).

Procedures: ``docs/webos/pm/pm.INSTRUCTIONS.md`` §7 and §7.1 (MPC001-207 requirement 6).

* Lists and items in a project need the webos ``TodoList`` / ``TodoListItem`` rights (MPC001-061).
* Lists with no project (``project_id = 0``) are personal (MPC001-202 option a): only their owner may
  change them or their items, administrators included (MPC001-243), and the right is still needed. Items in a
  personal list may be assigned only to the owner, so they never show in someone else's views.
* ``owner_id`` / ``owner`` come from the caller (MPC001-207 requirement 3); every ``todo_list_items``
  insert names ``status``, ``budgeted``, ``estimated`` and ``billed`` (``pm.md`` §9.1).
* "Delete" sets ``status = -1``; nothing here issues ``DELETE`` (MPC001-207 requirement 2). A deleted list,
  or an item in one, reads as not found.
* Every write that can change a Portal plate rings the planners of the users concerned once it has
  committed (Portal PTL001-311, cerp update journal 17): the users whose plate the task was on before the
  write and is on after it (``assigned_to``, or ``owner_id`` when unassigned), and the caller. See
  :meth:`~cerp.tools.context.ToolContext.ring_plates`.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field, StringConstraints

from cerp.auth.authorize import Caller
from cerp.data.records import clean_row
from cerp.data.security import SecurityUnavailable
from cerp.protocol.args import HandleText
from cerp.protocol.errors import NotFound, ValidationError
from cerp.tools.context import (
    IDEMPOTENT_WRITE,
    WRITE,
    DateText,
    Reads,
    Registrar,
    ToolContext,
    handle,
    record_id,
    stamp,
)
from cerp.tools.labels import PRIORITY, TASK_COMPLETED, TASK_STATUS, with_labels
from cerp.tools.pm import PROJECT, TASK, TASK_LIST

TASK_NEW: Final = 10000
DELETED: Final = -1
OWNER_MAX: Final = 80

TaskText = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
"""``todo_list_items.description``: the task's only label, VARCHAR(128)."""
CommentText = Annotated[str, StringConstraints(max_length=128)]
"""``todo_list_items.comments``: VARCHAR(128); empty clears it."""
CategoryText = Annotated[str, StringConstraints(max_length=128)]
TitleText = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
ListDescription = Annotated[str, StringConstraints(max_length=4096)]
Priority = Literal[9, 7, 5, 1]

READS: Final[Reads] = {
    ("pm", "projects"): ("id",),
    ("pm", "todo_lists"): (
        "id",
        "project_id",
        "project_activity_id",
        "owner_id",
        "owner",
        "creation_time",
        "title",
        "description",
        "status",
        "sort_order",
        "priority",
        "start_date",
        "end_date",
        "is_new",
        "is_late",
        "is_risk",
        "icon_flags",
    ),
    ("pm", "todo_list_items"): (
        "id",
        "todo_list_id",
        "description",
        "creation_time",
        "status",
        "assigned_to",
        "due_date",
        "sort_order",
        "owner_id",
        "owner",
        "category",
        "comments",
        "budgeted",
        "estimated",
        "billed",
    ),
}
GUARDS: Final = (PROJECT, TASK_LIST, TASK)

# ---------------------------------------------------------------- SQL (constant; values bound)

_LIST = "SELECT id, project_id, owner_id, status FROM todo_lists WHERE id = ?"
_ITEM = "SELECT id, todo_list_id, status FROM todo_list_items WHERE id = ?"
_PROJECT_EXISTS = "SELECT 1 FROM projects WHERE id = ?"
_LIST_OUT = """
SELECT id, project_id, title, description, status, priority, sort_order, start_date, end_date, owner,
       creation_time
FROM todo_lists WHERE id = ?
"""
_ITEM_OUT = """
SELECT id, todo_list_id, description, status, assigned_to, due_date, estimated, category, comments, owner,
       creation_time, sort_order
FROM todo_list_items WHERE id = ?
"""
_INSERT_LIST = """
INSERT INTO todo_lists (
  project_id, project_activity_id, owner_id, owner, creation_time, title, description, status, sort_order,
  priority, start_date, end_date, is_new, is_late, is_risk, icon_flags
)
SELECT :project_id, 0, :owner_id, :owner, :now, :title, :description, 10000,
  IFNULL((SELECT MAX(sort_order) FROM todo_lists
          WHERE IFNULL(project_id, 0) = :project_id
            AND (:project_id <> 0 OR owner_id = :owner_id)), 0) + 1,
  :priority, :start_date, :end_date, 0, 0, 0, 0
"""
_UPDATE_LIST = """
UPDATE todo_lists SET
  title = COALESCE(:title, title),
  description = COALESCE(:description, description),
  priority = COALESCE(:priority, priority),
  start_date = COALESCE(:start_date, start_date),
  end_date = COALESCE(:end_date, end_date)
WHERE id = :id AND IFNULL(status, 0) <> -1
"""
_DELETE_LIST = "UPDATE todo_lists SET status = -1 WHERE id = ? AND IFNULL(status, 0) <> -1"
_INSERT_ITEM = """
INSERT INTO todo_list_items (
  todo_list_id, description, creation_time, status, assigned_to, due_date, sort_order, owner_id, owner,
  category, comments, budgeted, estimated, billed
)
SELECT :list_id, :description, :now, 10000, :assigned_to, :due_date,
  IFNULL((SELECT MAX(sort_order) FROM todo_list_items
          WHERE todo_list_id = :list_id AND typeof(sort_order) = 'integer'), 0) + 1,
  :owner_id, :owner, :category, :comments, 0, :estimated, 0
"""
_UPDATE_ITEM = """
UPDATE todo_list_items SET
  description = COALESCE(:description, description),
  due_date = COALESCE(:due_date, due_date),
  assigned_to = COALESCE(:assigned_to, assigned_to),
  estimated = COALESCE(:estimated, estimated),
  category = COALESCE(:category, category)
WHERE id = :id AND IFNULL(status, 0) <> -1
"""
_COMMENT_ITEM = "UPDATE todo_list_items SET comments = ? WHERE id = ? AND IFNULL(status, 0) <> -1"
_STATUS_ITEM = "UPDATE todo_list_items SET status = ? WHERE id = ? AND IFNULL(status, 0) <> -1"
# The Portal's plate rule (PTL001-301): a task is on its assignee's plate, or its owner's when unassigned.
_PLATE_ITEM = """
SELECT CASE WHEN IFNULL(assigned_to, 0) <> 0 THEN assigned_to ELSE IFNULL(owner_id, 0) END
FROM todo_list_items WHERE id = ?
"""
_PLATE_LIST = """
SELECT DISTINCT CASE WHEN IFNULL(assigned_to, 0) <> 0 THEN assigned_to ELSE IFNULL(owner_id, 0) END
FROM todo_list_items WHERE todo_list_id = ? AND IFNULL(status, 0) <> -1
"""


class TodoListResult(BaseModel):
    todo_list: dict[str, Any] = Field(description="The list as stored after the change")


class TaskResult(BaseModel):
    task: dict[str, Any] = Field(description="The task as stored after the change")


# ---------------------------------------------------------------- helpers


def _owner_name(caller: Caller) -> str:
    return (caller.display_name or caller.user_name)[:OWNER_MAX]


def _one(con: sqlite3.Connection, statement: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = con.execute(statement, params).fetchone()
    return clean_row(dict(row)) if row is not None else None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _open_list(con: sqlite3.Connection, caller: Caller, list_id: int, what: str) -> dict[str, Any]:
    """The list, if it exists, is not deleted and (when personal) belongs to the caller."""
    row = _one(con, _LIST, (list_id,))
    if row is None or _as_int(row["status"]) == DELETED:
        raise NotFound(f"{what} not found")
    if _as_int(row["project_id"]) == 0 and _as_int(row["owner_id"]) != caller.user_id:
        raise NotFound(f"{what} not found")  # someone else's personal list (MPC001-243)
    return row


def _open_item(
    con: sqlite3.Connection, caller: Caller, item_id: int, what: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    item = _one(con, _ITEM, (item_id,))
    if item is None or _as_int(item["status"]) == DELETED:
        raise NotFound(f"{what} not found")
    return item, _open_list(con, caller, _as_int(item["todo_list_id"]), what)


def _plate(con: sqlite3.Connection, statement: str, key: int) -> set[int]:
    """The users whose Portal plate the task (or the list's tasks) is on (PTL001-301 plate rule)."""
    return {u for u in (_as_int(r[0]) for r in con.execute(statement, (key,)).fetchall()) if u > 0}


def _list_out(con: sqlite3.Connection, list_id: int) -> dict[str, Any]:
    row = _one(con, _LIST_OUT, (list_id,)) or {"id": list_id}
    row["handle"] = handle("pm", "todo_list", list_id)
    row["project"] = handle("pm", "project", row.pop("project_id", None))
    row["personal"] = row["project"] is None
    return with_labels(row, status=TASK_STATUS, priority=PRIORITY)


def _item_out(con: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    row = _one(con, _ITEM_OUT, (item_id,)) or {"id": item_id}
    row["handle"] = handle("pm", "todo_list_item", item_id)
    row["todo_list"] = handle("pm", "todo_list", row.pop("todo_list_id", None))
    return with_labels(row, status=TASK_STATUS)


def _check_assignee(ctx: ToolContext, caller: Caller, user_id: int | None, personal: bool) -> None:
    if user_id is None or user_id == 0:
        return
    if personal and user_id != caller.user_id:
        raise ValidationError(
            "a task in a personal list can be assigned only to its owner", field="assigned_to"
        )
    try:
        user = ctx.users().user_by_id(user_id)
    except SecurityUnavailable:
        raise ValidationError("the user directory is unavailable", field="assigned_to") from None
    if user is None:
        raise ValidationError(f"no webos user with id {user_id}", field="assigned_to")


# ---------------------------------------------------------------- tools


def register(reg: Registrar, ctx: ToolContext) -> None:  # noqa: PLR0915 - one closure per tool
    def create_todo_list(
        title: TitleText,
        *,
        project: HandleText | None = None,
        description: ListDescription | None = None,
        priority: Priority | None = None,
        start_date: DateText | None = None,
        end_date: DateText | None = None,
    ) -> TodoListResult:
        """Create a to-do list in a project (handle pm:project:<id>) or, with no project, a personal list
        only you can see. Priority: 9 Critical, 7 High, 5 Medium, 1 Low. Dates are YYYY-MM-DD."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK_LIST, "Add", "create to-do lists")
        pid = record_id(project, "pm", "project", "project") if project else 0
        with ctx.writer("pm") as con:
            if pid and con.execute(_PROJECT_EXISTS, (pid,)).fetchone() is None:
                raise NotFound(f"project {project} not found")
            cur = con.execute(
                _INSERT_LIST,
                {
                    "project_id": pid,
                    "owner_id": caller.user_id,
                    "owner": _owner_name(caller),
                    "now": stamp(ctx.now()),
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
            return TodoListResult(todo_list=_list_out(con, int(cur.lastrowid or 0)))

    reg(create_todo_list, "pm.create_todo_list", "Create a to-do list", WRITE)

    def update_todo_list(
        todo_list: HandleText,
        *,
        title: TitleText | None = None,
        description: ListDescription | None = None,
        priority: Priority | None = None,
        start_date: DateText | None = None,
        end_date: DateText | None = None,
    ) -> TodoListResult:
        """Change a to-do list (handle pm:todo_list:<id>): its title, description, priority (9 Critical,
        7 High, 5 Medium, 1 Low) or dates (YYYY-MM-DD). Fields you leave out are unchanged."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK_LIST, "Edit", "change to-do lists")
        lid = record_id(todo_list, "pm", "todo_list", "todo_list")
        with ctx.writer("pm") as con:
            _open_list(con, caller, lid, f"to-do list {todo_list}")
            users = _plate(con, _PLATE_LIST, lid)  # the list's title and priority show on its tasks' cards
            con.execute(
                _UPDATE_LIST,
                {
                    "id": lid,
                    "title": title,
                    "description": description,
                    "priority": priority,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
            out = TodoListResult(todo_list=_list_out(con, lid))
        ctx.ring_plates(users, caller)
        return out

    reg(update_todo_list, "pm.update_todo_list", "Change a to-do list", IDEMPOTENT_WRITE)

    def delete_todo_list(todo_list: HandleText) -> TodoListResult:
        """Delete a to-do list (handle pm:todo_list:<id>). The list is marked deleted (status -1) and
        hidden with its tasks; nothing is erased, so an administrator can restore it."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK_LIST, "Delete", "delete to-do lists")
        lid = record_id(todo_list, "pm", "todo_list", "todo_list")
        with ctx.writer("pm") as con:
            _open_list(con, caller, lid, f"to-do list {todo_list}")
            users = _plate(con, _PLATE_LIST, lid)  # its tasks leave their plates with it
            con.execute(_DELETE_LIST, (lid,))
            out = TodoListResult(todo_list=_list_out(con, lid))
        ctx.ring_plates(users, caller)
        return out

    reg(delete_todo_list, "pm.delete_todo_list", "Delete a to-do list", WRITE)

    def create_task(
        todo_list: HandleText,
        description: TaskText,
        *,
        due_date: DateText | None = None,
        assigned_to: int | None = None,
        estimated: Annotated[int, Field(ge=0, le=100000)] | None = None,
        category: CategoryText | None = None,
        comment: CommentText | None = None,
    ) -> TaskResult:
        """Add a task to a to-do list (handle pm:todo_list:<id>). `description` is the task's label (at
        most 128 characters); `assigned_to` is a webos user id (in a personal list, only yourself);
        `estimated` is a whole number of hours. The task starts as New (10000)."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK, "Add", "create tasks")
        lid = record_id(todo_list, "pm", "todo_list", "todo_list")
        with ctx.writer("pm") as con:
            row = _open_list(con, caller, lid, f"to-do list {todo_list}")
            _check_assignee(ctx, caller, assigned_to, personal=_as_int(row["project_id"]) == 0)
            cur = con.execute(
                _INSERT_ITEM,
                {
                    "list_id": lid,
                    "description": description,
                    "now": stamp(ctx.now()),
                    "assigned_to": assigned_to or 0,
                    "due_date": due_date,
                    "owner_id": caller.user_id,
                    "owner": _owner_name(caller),
                    "category": category,
                    "comments": comment,
                    "estimated": estimated or 0,
                },
            )
            iid = int(cur.lastrowid or 0)
            users = _plate(con, _PLATE_ITEM, iid)
            out = TaskResult(task=_item_out(con, iid))
        ctx.ring_plates(users, caller)
        return out

    reg(create_task, "pm.create_task", "Add a task", WRITE)

    def update_task(
        task: HandleText,
        *,
        description: TaskText | None = None,
        due_date: DateText | None = None,
        assigned_to: int | None = None,
        estimated: Annotated[int, Field(ge=0, le=100000)] | None = None,
        category: CategoryText | None = None,
    ) -> TaskResult:
        """Change a task (handle pm:todo_list_item:<id>): description (at most 128 characters), due date
        (YYYY-MM-DD), assignee (webos user id), estimate in hours or category. Fields you leave out are
        unchanged. Use pm.check_task to complete it and pm.set_task_comment for its comment."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK, "Edit", "change tasks")
        iid = record_id(task, "pm", "todo_list_item", "task")
        with ctx.writer("pm") as con:
            _, lst = _open_item(con, caller, iid, f"task {task}")
            _check_assignee(ctx, caller, assigned_to, personal=_as_int(lst["project_id"]) == 0)
            users = _plate(con, _PLATE_ITEM, iid)  # before: a reassignment rings the user it leaves
            con.execute(
                _UPDATE_ITEM,
                {
                    "id": iid,
                    "description": description,
                    "due_date": due_date,
                    "assigned_to": assigned_to,
                    "estimated": estimated,
                    "category": category,
                },
            )
            users |= _plate(con, _PLATE_ITEM, iid)  # after: and the user it reaches
            out = TaskResult(task=_item_out(con, iid))
        ctx.ring_plates(users, caller)
        return out

    reg(update_task, "pm.update_task", "Change a task", IDEMPOTENT_WRITE)

    def set_task_comment(task: HandleText, comment: CommentText) -> TaskResult:
        """Add or replace the comment on a task (handle pm:todo_list_item:<id>). A task holds one comment
        of at most 128 characters; an empty comment clears it."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK, "Edit", "change tasks")
        iid = record_id(task, "pm", "todo_list_item", "task")
        with ctx.writer("pm") as con:
            _open_item(con, caller, iid, f"task {task}")
            con.execute(_COMMENT_ITEM, (comment or None, iid))
            return TaskResult(task=_item_out(con, iid))

    reg(set_task_comment, "pm.set_task_comment", "Comment on a task", IDEMPOTENT_WRITE)

    def check_task(task: HandleText, done: bool = True) -> TaskResult:
        """Check off a task (handle pm:todo_list_item:<id>): status Completed (70000). With done=false it
        is unchecked and becomes New (10000) again. The list's own status is not changed."""
        caller = ctx.caller()
        ctx.require_to(caller, TASK, "Edit", "change tasks")
        iid = record_id(task, "pm", "todo_list_item", "task")
        with ctx.writer("pm") as con:
            _open_item(con, caller, iid, f"task {task}")
            users = _plate(con, _PLATE_ITEM, iid)  # a completed task leaves the plate; unchecked, it returns
            con.execute(_STATUS_ITEM, (TASK_COMPLETED if done else TASK_NEW, iid))
            out = TaskResult(task=_item_out(con, iid))
        ctx.ring_plates(users, caller)
        return out

    reg(check_task, "pm.check_task", "Check off a task", IDEMPOTENT_WRITE)
