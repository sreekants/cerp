"""Reminder write tools: ``personal.*`` (MPC001-242, decision 0003).

A reminder is a ``personal_waitings`` row (decision 0003; there is no reminder table in webos). Procedure:
``docs/webos/personal/personal.INSTRUCTIONS.md`` §2.

* Owner only: every statement filters on ``owner_id`` = the caller; another user's reminder, including for
  an administrator, reads as not found (MPC001-243). No webos right is needed, as for reading (decision 0002).
* ``owner_id`` / ``owner`` come from the caller (MPC001-207 requirement 3).
* Statuses are ``OPTIONS.TASK.STATUS`` codes; "delete" sets ``status = -1`` and nothing issues ``DELETE``
  (MPC001-207 requirement 2). A deleted reminder cannot be changed.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field, StringConstraints

from cerp.data.records import clean_row
from cerp.protocol.args import HandleText
from cerp.protocol.errors import NotFound
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
from cerp.tools.labels import TASK_STATUS, with_labels

OWNER_MAX: Final = 80

ReminderTitle = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]
NextAction = Annotated[str, StringConstraints(max_length=256)]
WaitingOn = Annotated[str, StringConstraints(max_length=128)]
Notes = Annotated[str, StringConstraints(max_length=4096)]
Category = Literal["friends", "family", "work", "life"]  # OPTIONS.WAITING_LIST, personal.md §3.1
Status = Literal[10000, 11000, 20000, 40000, 41000, 50000, 60000, 61000, 70000]

READS: Final[Reads] = {
    ("personal", "personal_waitings"): (
        "id",
        "owner_id",
        "owner",
        "title",
        "description",
        "creation_time",
        "due_date",
        "sort_order",
        "category",
        "next_action",
        "waiting_on",
        "guid",
        "status",
    ),
}

_INSERT = """
INSERT INTO personal_waitings (
  owner_id, owner, title, description, creation_time, due_date, sort_order, category, next_action,
  waiting_on, guid, status
)
SELECT :owner_id, :owner, :title, :description, :now, :due_date,
  IFNULL((SELECT MAX(sort_order) FROM personal_waitings
          WHERE owner_id = :owner_id AND typeof(sort_order) = 'integer'), 0) + 1,
  :category, :next_action, :waiting_on, :guid, 10000
"""
_OUT = """
SELECT id, title, description, due_date, next_action, waiting_on, category, status, creation_time
FROM personal_waitings WHERE id = ? AND owner_id = ?
"""
_RENAME = """
UPDATE personal_waitings SET title = ?
WHERE id = ? AND owner_id = ? AND IFNULL(status, 0) <> -1
"""
_STATUS = """
UPDATE personal_waitings SET status = ?
WHERE id = ? AND owner_id = ? AND IFNULL(status, 0) <> -1
"""


class ReminderResult(BaseModel):
    reminder: dict[str, Any] = Field(description="The reminder as stored after the change")


def _out(con: sqlite3.Connection, rid: int, owner: int) -> dict[str, Any]:
    row = con.execute(_OUT, (rid, owner)).fetchone()
    out: dict[str, Any] = clean_row(dict(row)) if row is not None else {"id": rid}
    out["handle"] = handle("personal", "waiting", rid)
    return with_labels(out, status=TASK_STATUS)


def register(reg: Registrar, ctx: ToolContext) -> None:
    def _change(reminder: str, statement: str, value: Any) -> ReminderResult:
        caller = ctx.caller()
        rid = record_id(reminder, "personal", "waiting", "reminder")
        with ctx.writer("personal") as con:
            if con.execute(statement, (value, rid, caller.user_id)).rowcount != 1:
                raise NotFound(f"reminder {reminder} not found")
            return ReminderResult(reminder=_out(con, rid, caller.user_id))

    def add_reminder(
        title: ReminderTitle,
        *,
        due_date: DateText | None = None,
        next_action: NextAction | None = None,
        waiting_on: WaitingOn | None = None,
        category: Category | None = None,
        notes: Notes | None = None,
    ) -> ReminderResult:
        """Add a reminder for yourself (stored as a waiting-for, visible only to you): a title, an
        optional due date (YYYY-MM-DD), next action, who you are waiting on, and a category (friends,
        family, work, life). It starts as New (10000)."""
        caller = ctx.caller()
        with ctx.writer("personal") as con:
            cur = con.execute(
                _INSERT,
                {
                    "owner_id": caller.user_id,
                    "owner": (caller.display_name or caller.user_name)[:OWNER_MAX],
                    "title": title,
                    "description": notes,
                    "now": stamp(ctx.now()),
                    "due_date": due_date,
                    "category": category,
                    "next_action": next_action,
                    "waiting_on": waiting_on,
                    "guid": str(uuid.uuid4()).upper(),
                },
            )
            return ReminderResult(reminder=_out(con, int(cur.lastrowid or 0), caller.user_id))

    reg(add_reminder, "personal.add_reminder", "Add a reminder", WRITE)

    def rename_reminder(reminder: HandleText, title: ReminderTitle) -> ReminderResult:
        """Rename one of your reminders (handle personal:waiting:<id>)."""
        return _change(reminder, _RENAME, title)

    reg(rename_reminder, "personal.rename_reminder", "Rename a reminder", IDEMPOTENT_WRITE)

    def set_reminder_status(reminder: HandleText, status: Status) -> ReminderResult:
        """Set the status of one of your reminders (handle personal:waiting:<id>): 10000 New, 11000 Not
        Started, 20000 In Progress, 40000 Waiting for Client, 41000 Waiting for Information, 50000
        Unplanned, 60000 Deferred, 61000 Cancelled, 70000 Completed."""
        return _change(reminder, _STATUS, status)

    reg(set_reminder_status, "personal.set_reminder_status", "Set a reminder's status", IDEMPOTENT_WRITE)

    def delete_reminder(reminder: HandleText) -> ReminderResult:
        """Delete one of your reminders (handle personal:waiting:<id>). It is marked deleted (status -1)
        and hidden; nothing is erased."""
        return _change(reminder, _STATUS, -1)

    reg(delete_reminder, "personal.delete_reminder", "Delete a reminder", WRITE)
