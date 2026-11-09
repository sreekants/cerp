"""Read-only personal views: ``personal.my_day`` and ``personal.weekly_review`` (MPC001-240, MPC001-245).

Both are for the caller only. Personal records (waiting-fors, maybes) are filtered on ``owner_id`` = the
caller and are never readable by anyone else, administrators included (MPC001-243); they need no webos
right, because webos defines none for them. Project tasks, tickets, milestones and CRM follow-ups come
from their own modules and are shown only where the caller holds that module's rights (MPC001-061).

Decisions recorded for the read-only MVP (``docs/webos/personal/personal.md``, MPC001-202):

* personal tasks are task lists with no project in ``pm.todo_lists`` (option a; no schema change);
* a waiting-for or maybe is open unless its status is Completed (70000), Cancelled (61000) or deleted
  (-1); live data holds 0, NULL or 10000, which are all open;
* "maybes not touched in a month" uses ``creation_time``: the table has no modification time.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from pydantic import BaseModel, Field

from cerp.tools.context import Reads, Registrar, ToolContext, handle, parse_day, stamp
from cerp.tools.crm import ACTIVITY, REMINDER, TICKET, activity_row
from cerp.tools.labels import PRIORITY, TASK_STATUS, TICKET_STATUS, with_labels
from cerp.tools.pm import MILESTONE, TASK, open_tasks_for

READS: Final[Reads] = {
    ("personal", "personal_waitings"): (
        "id",
        "owner_id",
        "title",
        "waiting_on",
        "next_action",
        "due_date",
        "creation_time",
        "status",
        "category",
    ),
    ("personal", "personal_maybes"): (
        "id",
        "owner_id",
        "title",
        "next_action",
        "creation_time",
        "status",
        "category",
    ),
    ("im", "tickets"): ("id", "short_name", "status", "priority", "owner_id", "due_date", "creation_time"),
    ("crm", "opportunity_reminders"): (
        "id",
        "description",
        "opportunity_id",
        "status",
        "assigned_to",
        "due_date",
    ),
    ("crm", "activities"): ("id", "subject", "type", "status", "priority", "owner_id", "due_date"),
    ("pm", "milestones"): ("id", "project_id", "owner_id", "title", "due_date", "status"),
    ("pm", "projects"): ("id", "name", "owner_id"),
}

_WAITINGS = """
SELECT id, title, waiting_on, next_action, due_date, creation_time, status, category
FROM personal_waitings
WHERE owner_id = :user AND IFNULL(status, 0) NOT IN (61000, 70000, -1)
ORDER BY due_date, id LIMIT 500
"""
_MAYBES = """
SELECT id, title, next_action, creation_time, status, category FROM personal_maybes
WHERE owner_id = :user AND IFNULL(status, 0) NOT IN (61000, 70000, -1) AND creation_time < :before
ORDER BY creation_time, id LIMIT 200
"""
_MY_TICKETS = """
SELECT id, short_name, status, priority, due_date, creation_time FROM tickets
WHERE owner_id = :user AND IFNULL(status, 0) NOT IN (5000, -1)
ORDER BY due_date, priority DESC, id LIMIT 200
"""
_MY_REMINDERS = """
SELECT id, description, opportunity_id, status, due_date FROM opportunity_reminders
WHERE assigned_to = :user AND typeof(status) = 'integer' AND status NOT IN (61000, 70000, -1)
  AND substr(due_date, 1, 10) <= :until
ORDER BY due_date, id LIMIT 200
"""
_MY_ACTIVITIES = """
SELECT id, subject, type, status, priority, due_date FROM activities
WHERE owner_id = :user AND IFNULL(status, 0) NOT IN (61000, 70000, -1) AND substr(due_date, 1, 10) <= :until
ORDER BY due_date, id LIMIT 200
"""
_MY_MILESTONES = """
SELECT m.id, m.title, m.due_date, m.status, m.project_id, p.name AS project_name
FROM milestones m LEFT JOIN projects p ON p.id = m.project_id
WHERE (m.owner_id = :user OR p.owner_id = :user) AND IFNULL(m.status, 0) NOT IN (61000, 70000, -1)
  AND substr(m.due_date, 1, 10) BETWEEN :start AND :end
ORDER BY m.due_date, m.id LIMIT 200
"""


class MyDay(BaseModel):
    as_of: str
    overdue: list[dict[str, Any]]
    today: list[dict[str, Any]]
    this_week: list[dict[str, Any]]
    open_tickets_without_due_date: list[dict[str, Any]] | None = None
    not_shown: list[str] = Field(description="Sources left out because you cannot read them")


class WeeklyReview(BaseModel):
    as_of: str
    next_week: str = Field(description="Monday to Sunday of next week")
    overdue_tasks: list[dict[str, Any]] | None
    stale_waiting_fors: list[dict[str, Any]] = Field(description="Open waiting-fors older than stale_days")
    old_maybes: list[dict[str, Any]] = Field(description="Open maybes created more than 30 days ago")
    milestones_next_week: list[dict[str, Any]] | None
    tasks_due_next_week: list[dict[str, Any]] | None
    not_shown: list[str]


def _waiting(row: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "waiting for", "handle": handle("personal", "waiting", row["id"]), **row}


def register(reg: Registrar, ctx: ToolContext) -> None:  # noqa: PLR0915 - one closure per tool
    def my_day() -> MyDay:  # noqa: PLR0912 - one branch per guarded section
        """Your day, for you only: your open tasks, waiting-fors, tickets assigned to you and CRM
        follow-ups and activities, grouped as overdue, today and this week (to Sunday)."""
        caller = ctx.caller()
        today = ctx.now().date()
        week_end = today + dt.timedelta(days=6 - today.weekday())
        items: list[dict[str, Any]] = []
        not_shown: list[str] = []
        if ctx.allowed(caller, TASK):
            items += [{"kind": "task", **t} for t in open_tasks_for(ctx, caller.user_id)]
        else:
            not_shown.append("tasks")
        items += [_waiting(r) for r in ctx.rows("personal", _WAITINGS, {"user": caller.user_id})]
        undated: list[dict[str, Any]] | None = None
        if ctx.allowed(caller, TICKET):
            tickets = [
                with_labels(
                    {"kind": "ticket", "handle": handle("im", "ticket", r["id"]), **r},
                    status=TICKET_STATUS,
                    priority=PRIORITY,
                )
                for r in ctx.rows("im", _MY_TICKETS, {"user": caller.user_id})
            ]
            items += [t for t in tickets if t.get("due_date")]
            undated = [t for t in tickets if not t.get("due_date")]
        else:
            not_shown.append("tickets")
        until = {"user": caller.user_id, "until": week_end.isoformat()}
        if ctx.allowed(caller, REMINDER):
            items += [
                with_labels(
                    {
                        "kind": "follow-up",
                        "handle": handle("crm", "opportunity_reminder", r["id"]),
                        "opportunity": handle("crm", "opportunity", r.pop("opportunity_id")),
                        **r,
                    },
                    status=TASK_STATUS,
                )
                for r in ctx.rows("crm", _MY_REMINDERS, until)
            ]
        else:
            not_shown.append("CRM follow-ups")
        if ctx.allowed(caller, ACTIVITY):
            items += [
                {"kind": "sales activity", **activity_row(r)} for r in ctx.rows("crm", _MY_ACTIVITIES, until)
            ]
        else:
            not_shown.append("sales activities")
        out = MyDay(
            as_of=today.isoformat(),
            overdue=[],
            today=[],
            this_week=[],
            open_tickets_without_due_date=undated,
            not_shown=not_shown,
        )
        for item in sorted(items, key=lambda i: (str(i.get("due_date") or ""), i["kind"])):
            day = parse_day(item.get("due_date"))
            if day is None:
                continue
            if day < today:
                out.overdue.append(item)
            elif day == today:
                out.today.append(item)
            elif day <= week_end:
                out.this_week.append(item)
        return out

    reg(my_day, "personal.my_day", "My day")

    def weekly_review(stale_days: int = 14) -> WeeklyReview:
        """Your weekly review, for you only: overdue tasks, open waiting-fors older than `stale_days`
        (default 14), maybes older than a month, and next week's milestones and due tasks."""
        caller = ctx.caller()
        now = ctx.now()
        today = now.date()
        monday = today + dt.timedelta(days=7 - today.weekday())
        sunday = monday + dt.timedelta(days=6)
        not_shown: list[str] = []
        tasks: list[dict[str, Any]] | None = None
        if ctx.allowed(caller, TASK):
            tasks = open_tasks_for(ctx, caller.user_id)
        else:
            not_shown.append("tasks")
        stale_before = stamp(now - dt.timedelta(days=max(1, stale_days)))
        waitings = [
            _waiting(r)
            for r in ctx.rows("personal", _WAITINGS, {"user": caller.user_id})
            if (r.get("creation_time") or "") < stale_before
        ]
        maybes = [
            {"handle": handle("personal", "maybe", r["id"]), **r}
            for r in ctx.rows(
                "personal", _MAYBES, {"user": caller.user_id, "before": stamp(now - dt.timedelta(days=30))}
            )
        ]
        milestones = None
        if ctx.allowed(caller, MILESTONE):
            milestones = [
                with_labels(
                    {
                        "handle": handle("pm", "milestone", r["id"]),
                        "project": handle("pm", "project", r.pop("project_id")),
                        **r,
                    },
                    status=TASK_STATUS,
                )
                for r in ctx.rows(
                    "pm",
                    _MY_MILESTONES,
                    {"user": caller.user_id, "start": monday.isoformat(), "end": sunday.isoformat()},
                )
            ]
        else:
            not_shown.append("milestones")
        return WeeklyReview(
            as_of=today.isoformat(),
            next_week=f"{monday.isoformat()}..{sunday.isoformat()}",
            overdue_tasks=None
            if tasks is None
            else [t for t in tasks if (d := parse_day(t["due_date"])) and d < today],
            stale_waiting_fors=waitings,
            old_maybes=maybes,
            milestones_next_week=milestones,
            tasks_due_next_week=None
            if tasks is None
            else [t for t in tasks if (d := parse_day(t["due_date"])) and monday <= d <= sunday],
            not_shown=not_shown,
        )

    reg(weekly_review, "personal.weekly_review", "Weekly review")
