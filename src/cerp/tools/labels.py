"""Code labels for the curated tools, from ``lang.en.s3db`` (the authority; MPC001-201, MPC001-249).

Copied from ``docs/webos/pm/pm.md`` §3, ``docs/webos/crm/crm.md`` §3 and ``docs/webos/im/im.md`` §3, which
record them from ``lang.en.s3db``. A code with no label is reported as ``"unknown"``, never guessed
(MPC001-248). Columns with no list (crm ``rating``, ``leads.status``, personal ``status``) are returned raw.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# OPTIONS.TASK.STATUS: activities, milestones, task lists and tasks (pm.md §3.1); crm activities and
# opportunity reminders use the same ladder (crm.md §3.3).
TASK_STATUS: Final = {
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
TASK_COMPLETED: Final = 70000
TASK_CANCELLED: Final = 61000
TASK_DONE: Final = (TASK_CANCELLED, TASK_COMPLETED)  # every other code is open
DELETED: Final = -1  # webos soft-delete marker on status columns (MPC001-163)

# OPTIONS.PROJECT.STATUS, stored in projects.stage (pm.md §3.2).
PROJECT_STAGE: Final = {
    100000: "New",
    110000: "Not Started",
    200000: "In Progress",
    500001: "Planned",
    600000: "Deferred",
    610000: "Cancelled",
    700000: "Completed",
    900000: "Closed",
}
PROJECT_FINISHED: Final = (610000, 700000, 900000)

# OPTIONS.PRIORITY: todo_lists.priority, tickets.priority, opportunities.priority (9 = most urgent).
PRIORITY: Final = {9: "Critical", 7: "High", 5: "Medium", 1: "Low"}

# OPTION.OPPORTUNITY.TYPE, stored in opportunities.sales_stage (crm.md §3.2). Open while < 900000.
SALES_STAGE: Final = {
    100000: "Prospecting",
    200000: "Lead Generation",
    300000: "Qualification",
    350000: "Pursuit",
    400000: "Needs Analysis",
    500000: "Request for Proposal (RFP)",
    600000: "Value Proposition",
    610000: "Identify Decision Maker",
    620000: "Perception Analysis",
    700000: "Proposal & Price Quote",
    800000: "Negotiation & Review",
    830000: "Paid",
    850000: "Shipped",
    860000: "Fulfilled",
    870000: "Delivered",
    900000: "Closed-Won",
    910000: "Closed-Lost",
}
OPEN_BELOW: Final = 900000

ACTIVE: Final = {1000: "Active", 5000: "Inactive"}  # accounts.status, contacts.status
BUSINESS_TYPE: Final = {
    0: "Unset",
    1: "Public Company",
    2: "Private Company",
    3: "Individual",
    4: "Govt.",
    5: "Small Business",
    6: "Non-Profit",
}
ACTIVITY_TYPE: Final = {
    1: "Task",
    2: "Meeting",
    3: "E-Mail",
    4: "Phone Call",
    5: "Web Chat",
    6: "Web Cast",
    7: "Letter",
}
ACTIVITY_PRIORITY: Final = {100: "Low", 200: "Normal", 300: "High", 400: "Urgent"}
SOURCE: Final = {
    100: "Cold Call",
    200: "Existing Customer",
    300: "Employee Referral",
    400: "Partner",
    500: "Direct Mail",
    600: "Conference",
    700: "Trade Show",
    800: "Website",
    900: "Campaign",
    1000: "Other",
}

# OPTIONS.RECEIVABLE.STATUS and OPTIONS.PAYABLE.STATUS (cfm.md §3.1, MPC001-283). Owed while Open or Approved.
OBLIGATION_STATUS: Final = {
    10000: "Open",
    15000: "Approved",
    20000: "Invoiced",
    30000: "Paid",
    80000: "Closed",
    90000: "Cancelled",
}
RECEIVABLE_STATUS: Final = OBLIGATION_STATUS
PAYABLE_STATUS: Final = OBLIGATION_STATUS
OWED: Final = (10000, 15000)

# OPTIONS.TICKET.STATUS (im.md §3.1). 5000 Closed is the only terminal state.
TICKET_STATUS: Final = {1000: "Open", 2000: "Fixed", 3000: "Reviewed", 4000: "Tested", 5000: "Closed"}
TICKET_CLOSED: Final = 5000


def label(codes: Mapping[int, str], value: Any) -> str | None:
    """The label for a stored code; ``"unknown"`` for a code with no label, ``None`` for no value."""
    if value is None:
        return None
    try:
        return codes.get(int(value), "unknown")
    except (TypeError, ValueError):  # e.g. todo_list_items.status stored as the text '0' (pm.md §9.1)
        return "unknown"


def with_labels(row: dict[str, Any], **columns: Mapping[int, str]) -> dict[str, Any]:
    """Add ``<column>_label`` beside each named code column present in ``row``."""
    for column, codes in columns.items():
        if column in row:
            row[f"{column}_label"] = label(codes, row[column])
    return row
