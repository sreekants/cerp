"""A small synthetic tenant for the read-only tool packs (MPC001-208..276, MPC001-060/061).

Builds ``security.s3db`` from the webos schema (no real users, no passwords) and inserts invented records
into private copies of the template databases. It includes the data quirks the tools must survive: the
text ``'0'`` status in ``todo_list_items`` (pm.md §9.1) and ``opportunity_reminders`` (crm.md §9),
percentages in ``opportunities.probability``, soft-deleted rows (status -1) and personal lists of two users.

This stands in for the tenant seed script (MPC001-150) until it exists. The fixed clock is
Wednesday 2026-11-04 10:00, so "this week" is 2026-11-02..2026-11-08.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final

NOW: Final = dt.datetime(2026, 11, 4, 10, 0, 0)

SECURITY_DDL: Final = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, name VARCHAR(32) UNIQUE NOT NULL,
  password VARCHAR(32) NULL, first_name VARCHAR(32) NOT NULL, last_name VARCHAR(32) NULL,
  email VARCHAR(64) NULL, type INTEGER DEFAULT '0' NULL, enabled INTEGER NULL, image TEXT NULL,
  profile TEXT NULL, last_login_time TIMESTAMP NULL);
CREATE TABLE groups (id INTEGER PRIMARY KEY NOT NULL, name VARCHAR(64) UNIQUE NOT NULL,
  description VARCHAR(128) NULL);
CREATE TABLE memberships (id INTEGER PRIMARY KEY NOT NULL, user_id INTEGER NOT NULL,
  group_id INTEGER NOT NULL);
CREATE TABLE privileges (id INTEGER NOT NULL PRIMARY KEY, group_id INTEGER NOT NULL,
  right_id INTEGER NOT NULL);
CREATE TABLE rights (id INTEGER PRIMARY KEY NOT NULL, type INTEGER NOT NULL, name VARCHAR(64) UNIQUE NOT NULL,
  description VARCHAR(80) NULL, application VARCHAR(4) NULL);
"""

READER_RIGHTS: Final = (
    "Account.View", "Contact.View", "Opportunity.View", "Activity.View", "OpportunityReminder.View",
    "InventoryItem.View", "Ticket.View", "Project.View", "ProjectActivity.View", "Milestone.View",
    "TodoList.View", "TodoListItem.View", "TimeTrack.View", "Expense.View",
)  # fmt: skip

EDITOR_RIGHTS: Final = (
    "TodoList.Add", "TodoList.Edit", "TodoList.Delete", "TodoListItem.Add", "TodoListItem.Edit",
    # decision 0004
    "Account.Add", "Account.Edit", "Account.Delete", "Contact.Add", "Contact.Edit", "Contact.Delete",
    "Vendor.View", "Vendor.Add", "Vendor.Edit", "Vendor.Delete",
    "ProjectContact.Add", "ProjectContact.Delete",
)  # fmt: skip

# user id, login, first, last, enabled, groups
USERS: Final = (
    (1, "admin", "Ada", "Admin", 1, (1,)),
    (2, "alice", "Alice", "Andersen", 1, (10, 12)),  # 12: may write to-do lists (decision 0003)
    (3, "bob", "Bob", "Berg", "Y", (11,)),  # 'Y' as in the webos template: enabled
    (4, "carol", "Carol", "Carlsen", 0, (10,)),  # disabled
    (5, "nobody", "No", "Rights", 1, ()),
)
GROUPS: Final = {
    1: ("Administrators", ()),
    10: ("Readers", READER_RIGHTS),
    # Project.View only, plus a right whose name merely starts with Account.View (WOS-BUG-035 trap)
    11: ("Projects", ("Project.View", "Account.ViewAll", "TodoListItem.View", "TodoList.View")),
    12: ("Task editors", EDITOR_RIGHTS),
}


def _insert(
    con: sqlite3.Connection, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> None:
    # Test-only helper: table and column names are literals in this file, never input.
    cols = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    con.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(rows))  # noqa: S608


def build_security(dbroot: Path) -> None:
    con = sqlite3.connect(dbroot / "security.s3db")
    try:
        con.executescript(SECURITY_DDL)
        names = sorted({r for _, rights in GROUPS.values() for r in rights})
        right_id = {n: i for i, n in enumerate(names, start=1)}
        _insert(con, "rights", ("id", "type", "name"), [(i, 1, n) for n, i in right_id.items()])
        _insert(con, "groups", ("id", "name"), [(gid, g[0]) for gid, g in GROUPS.items()])
        _insert(
            con,
            "privileges",
            ("group_id", "right_id"),
            [(gid, right_id[r]) for gid, (_, rights) in GROUPS.items() for r in rights],
        )
        _insert(
            con,
            "users",
            ("id", "name", "first_name", "last_name", "enabled"),
            [(u[0], u[1], u[2], u[3], u[4]) for u in USERS],
        )
        _insert(con, "memberships", ("user_id", "group_id"), [(u[0], g) for u in USERS for g in u[5]])
        con.commit()
    finally:
        con.close()


def seed_business(dbroot: Path) -> None:
    pm = sqlite3.connect(dbroot / "pm.s3db")
    try:
        _insert(
            pm,
            "projects",
            ("id", "name", "stage", "owner_id", "owner", "parent_id", "account_id", "budget", "start_date",
             "end_date", "creation_date", "description"),
            [
                (1, "Website rebuild", 200000, 2, "Alice Andersen", 0, 1001, 10000.0, "2026-10-01 00:00:00",
                 "2026-12-31 00:00:00", "2026-09-20 09:00:00", "New public website"),
                (2, "Old intranet", 900000, 2, "Alice Andersen", 1, 0, None, "2025-01-01 00:00:00",
                 "2025-06-30 00:00:00", "2024-12-01 09:00:00", "Closed"),
                (3, "Riverside", 110000, 3, "Bob Berg", 0, 0, None, "2026-11-10 00:00:00", None,
                 "2026-10-25 09:00:00", "Data_migration 100% done"),
            ],
        )  # fmt: skip
        _insert(
            pm,
            "project_activities",
            ("id", "project_id", "owner", "description", "start_date", "end_date", "status"),
            [(1001, 1, "Alice Andersen", "Design", "2026-10-01", "2026-11-15", 20000)],
        )
        _insert(
            pm,
            "milestones",
            ("id", "project_id", "owner_id", "owner", "title", "due_date", "status"),
            [
                (1001, 1, 2, "Alice Andersen", "Beta", "2026-11-01 00:00:00", 20000),
                (1002, 1, 2, "Alice Andersen", "Launch", "2026-11-09 00:00:00", 10000),
                (1003, 1, 2, "Alice Andersen", "Kickoff", "2026-10-01 00:00:00", 70000),
                (1004, 3, 3, "Bob Berg", "Survey", "2026-11-12 00:00:00", 10000),
            ],
        )
        _insert(
            pm,
            "todo_lists",
            (
                "id",
                "project_id",
                "owner_id",
                "title",
                "status",
                "project_activity_id",
                "priority",
                "sort_order",
            ),
            [
                (1, 1, 2, "Build", 20000, 1001, 7, 1),
                (2, 0, 2, "Alice personal", 10000, 0, 5, 1),
                (3, 0, 3, "Bob personal", 10000, 0, 5, 1),
            ],
        )
        _insert(
            pm,
            "todo_list_items",
            ("id", "todo_list_id", "description", "status", "assigned_to", "due_date", "estimated",
             "budgeted", "billed", "sort_order"),
            [
                (1, 1, "Write spec", 70000, 2, "2026-10-20 00:00:00", 6, 0, 0, 1),
                (2, 1, "Build pages", 20000, 2, "2026-11-02 00:00:00", 8, 0, 0, 2),
                (3, 1, "Test pages", 10000, 2, "2026-11-06 00:00:00", 4, 0, 0, 3),
                (4, 1, "Dropped idea", 61000, 2, None, 0, 0, 0, 4),
                (5, 1, "Fix corrupt row", "'0'", 3, "2026-11-04 00:00:00", "'''0'''", 0, 0, 5),
                (6, 2, "Buy printer ink", 10000, 0, "2026-11-04 00:00:00", 1, 0, 0, 1),
                (7, 3, "Bob private errand", 10000, 0, "2026-11-04 00:00:00", 1, 0, 0, 1),
                (8, 1, "Deleted task", -1, 2, "2026-11-03 00:00:00", 9, 0, 0, 6),
                (9, 1, "Deploy", 10000, 2, "2026-11-11 00:00:00", 3, 0, 0, 7),
            ],
        )  # fmt: skip
        _insert(
            pm,
            "time_tracks",
            ("project_id", "owner_id", "owner", "hours", "logged_on"),
            [
                (1, 2, "Alice Andersen", 5.0, "2026-11-03 12:00:00"),
                (1, 2, "Alice Andersen", 3.0, "2026-10-15"),
            ],
        )
        _insert(
            pm,
            "expenses",
            ("project_id", "owner_id", "owner", "amount", "logged_on", "business_object_type",
             "business_object_id", "note"),
            [(1, 2, "Alice Andersen", 2500.0, "2026-10-10", 1001, 1, "licences")],
        )  # fmt: skip
        pm.commit()
    finally:
        pm.close()

    crm = sqlite3.connect(dbroot / "crm.s3db")
    try:
        _insert(
            crm,
            "accounts",
            ("id", "name", "owner_id", "owner", "type", "status", "website", "phone", "contact_id",
             "sales_team_id", "modification_date", "description"),
            [
                (1001, "Acme AS", 2, "Alice Andersen", 2, 1000, "https://www.acme.no", "+47 22 33 44 55",
                 1001, 1, "2026-10-01", "Machines"),
                (1002, "Globex Ltd", 2, "Alice Andersen", 1, 1000, "globex.com", None, 0, 0, "2026-09-01",
                 None),
                (1003, "Gone Corp", 2, "Alice Andersen", 1, -1, None, None, 0, 0, "2026-01-01", None),
            ],
        )  # fmt: skip
        _insert(
            crm,
            "contacts",
            ("id", "first_name", "last_name", "email", "mobile", "account_id", "status", "home_phone",
             "home_address"),
            [
                (1001, "Kari", "Nordmann", "kari@acme.no", "+47 912 34 567", 1001, 1000, "555-HOME",
                 "Secret 1"),
                (1002, "Ola", "Hansen", "ola@acme.no", None, 1001, 1000, None, None),
            ],
        )  # fmt: skip
        _insert(
            crm,
            "opportunities",
            ("id", "account_id", "owner_id", "owner", "title", "sales_stage", "amount", "probability",
             "expected_closure_date", "sales_team_id", "modification_time", "creation_date"),
            [
                (1001, 1001, 2, "Alice Andersen", "Big deal", 700000, 50000.0, 0.8, "2026-11-20", 1,
                 "2026-09-01 00:00:00", "2026-08-01 00:00:00"),
                (1002, 1001, 2, "Alice Andersen", "Small deal", 300000, 10000.0, 25, "2026-12-05", 1,
                 "2026-11-03 00:00:00", "2026-10-20 00:00:00"),
                (1003, 1002, 2, "Alice Andersen", "Won deal", 900000, 99999.0, 1.0, "2026-10-01", 0,
                 "2026-10-01 00:00:00", "2026-07-01 00:00:00"),
                (1004, 1002, 3, "Bob Berg", "Late deal", 400000, 5000.0, 0.5, "2026-10-15", 0,
                 "2026-11-01 00:00:00", "2026-09-15 00:00:00"),
            ],
        )  # fmt: skip
        _insert(
            crm,
            "opportunity_reminders",
            ("id", "opportunity_id", "description", "status", "assigned_to", "due_date"),
            [
                (1001, 1001, "Call the CFO", 10000, 2, "2026-11-01"),
                (1002, 1001, "Corrupt status row", "'0'", 2, "2026-11-01"),  # crm.md §9 default
            ],
        )
        _insert(
            crm,
            "activities",
            ("id", "account_id", "contact_id", "opportunity_id", "subject", "type", "status", "owner_id",
             "owner", "due_date", "creation_date"),
            [(1001, 1001, 1001, 1001, "Demo", 2, 10000, 2, "Alice Andersen", "2026-11-05", "2026-10-28")],
        )  # fmt: skip
        _insert(
            crm,
            "quotes",
            ("id", "opportunity_id", "title", "creation_date"),
            [(1001, 1001, "Q-1", "2026-10-30")],
        )
        _insert(
            crm, "quote_details", ("quote_id", "quantity", "unit_price"), [(1001, 2, 100.0), (1001, 1, 50.0)]
        )
        _insert(
            crm,
            "inventory_items",
            ("id", "account_id", "product_id", "quantity", "purchase_date", "support_end_date"),
            [(1001, 1001, 7, 3, "2025-01-15", "2027-01-15")],
        )
        _insert(
            crm,
            "vendors",
            ("id", "name", "owner_id", "owner", "status", "website", "phone", "contact_id"),
            [(1001, "Parts Supply AS", 2, "Alice Andersen", 1000, "parts.no", "+47 22 00 11 22", 0)],
        )
        _insert(crm, "sales_teams", ("id", "name", "owner"), [(1, "Nordic", "Alice Andersen")])
        _insert(
            crm,
            "sales_team_members",
            ("sales_team_id", "user_id", "first_name", "last_name"),
            [(1, 2, "Alice", "Andersen")],
        )
        crm.commit()
    finally:
        crm.close()

    im = sqlite3.connect(dbroot / "im.s3db")
    try:
        _insert(
            im,
            "tickets",
            (
                "id",
                "short_name",
                "status",
                "priority",
                "owner_id",
                "owner",
                "account_id",
                "creation_time",
                "due_date",
            ),
            [
                (
                    1001,
                    "Login fails",
                    1000,
                    9,
                    2,
                    "Alice Andersen",
                    1001,
                    "2026-10-29 08:00:00",
                    "2026-11-03",
                ),
                (1002, "Old closed", 5000, 5, 2, "Alice Andersen", 1001, "2026-09-01 08:00:00", None),
                (1003, "No due date", 2000, 5, 2, "Alice Andersen", 1002, "2026-10-30 08:00:00", None),
            ],
        )
        im.commit()
    finally:
        im.close()

    personal = sqlite3.connect(dbroot / "personal.s3db")
    try:
        _insert(
            personal,
            "personal_waitings",
            ("id", "owner_id", "owner", "title", "waiting_on", "due_date", "creation_time", "status"),
            [
                (1, 2, "Alice Andersen", "Signed contract", "Kari", "2026-11-05", "2026-10-01 09:00:00", 0),
                (2, 3, "Bob Berg", "Bob's private wait", "Bank", "2026-11-04", "2026-10-01 09:00:00", 0),
                (3, 2, "Alice Andersen", "Done already", "Ola", "2026-11-02", "2026-10-01 09:00:00", 70000),
            ],
        )
        _insert(
            personal,
            "personal_maybes",
            ("id", "owner_id", "owner", "title", "creation_time", "status"),
            [
                (1, 2, "Alice Andersen", "Learn Rust", "2026-09-01 09:00:00", 10000),
                (2, 3, "Bob Berg", "Bob's dream", "2026-09-01 09:00:00", 10000),
                (3, 2, "Alice Andersen", "Fresh idea", "2026-11-01 09:00:00", 10000),
            ],
        )
        personal.commit()
    finally:
        personal.close()
