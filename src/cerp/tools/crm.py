"""Read-only CRM tools: ``crm.*`` (MPC001-252, -253, -256, -268, -269, -272, -276).

Codes are labelled from ``lang.en.s3db`` (``crm.md`` §3, MPC001-249): sales stages, account and contact
status, business type, activity type and priority, opportunity priority and source. An opportunity is open
while ``sales_stage < 900000``. ``rating`` and ``leads.status`` have no list and are returned raw.

Decisions recorded for the read-only MVP:

* **Rights.** Each section is guarded by its model's rights (MPC001-061). Quotes and sales teams have no
  model or rights of their own in webos, so they are guarded by ``Opportunity`` rights.
* **Personal contact fields** (home address, home phone, home fax, deceased flag) are never selected
  (MPC001-255, MPC002-043).
* **Probability** is stored inconsistently as a fraction or a percentage; values above 1 are read as
  percentages. Amounts carry no currency; every total says so.
* **Customer 360** leaves out money owed (cfm receivables) and orders (scm): their status codes have no
  decided meaning, so "owed" and "open" cannot be computed truthfully yet (MPC001-253).
* **Timeline** has no stage history to draw on (``crm.md``: none is kept); it shows when each
  opportunity was created, its expected close and its current stage.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from cerp.protocol.args import HandleText, ShortText
from cerp.protocol.errors import NotFound, ValidationError
from cerp.tools.context import (
    CursorText,
    Guard,
    Reads,
    Registrar,
    ToolContext,
    handle,
    like,
    parse_day,
    record_id,
    stamp,
)
from cerp.tools.labels import (
    ACTIVE,
    ACTIVITY_PRIORITY,
    ACTIVITY_TYPE,
    BUSINESS_TYPE,
    OPEN_BELOW,
    PRIORITY,
    PROJECT_STAGE,
    SALES_STAGE,
    SOURCE,
    TASK_STATUS,
    TICKET_STATUS,
    with_labels,
)
from cerp.tools.pm import PROJECT

ACCOUNT: Final = Guard("accounts", model_key="objects/sales/account")
CONTACT: Final = Guard("contacts", model_key="objects/sales/contact")
OPPORTUNITY: Final = Guard("opportunities", model_key="objects/sales/opportunity")
ACTIVITY: Final = Guard("sales activities", model_key="objects/sales/activity")
REMINDER: Final = Guard("opportunity follow-ups", model_key="objects/sales/opportunity_reminder")
INSTALLED: Final = Guard("installed base", model_key="objects/inventories/inventory_item")
TICKET: Final = Guard("tickets", model_key="objects/support/ticket")
QUOTE: Final = Guard("quotes", privilege="Opportunity")
TEAM: Final = Guard("sales teams", privilege="Opportunity")
GUARDS: Final = (ACCOUNT, CONTACT, OPPORTUNITY, ACTIVITY, REMINDER, INSTALLED, TICKET, QUOTE, TEAM, PROJECT)

READS: Final[Reads] = {
    ("crm", "accounts"): (
        "id",
        "name",
        "description",
        "owner_id",
        "owner",
        "type",
        "status",
        "rating",
        "phone",
        "website",
        "contact_id",
        "sales_team_id",
        "creation_date",
        "modification_date",
        "address",
        "code",
    ),
    ("crm", "contacts"): (
        "id",
        "first_name",
        "last_name",
        "title",
        "company",
        "email",
        "mobile",
        "business_phone",
        "business_address",
        "account_id",
        "vendor_id",
        "status",
    ),
    ("crm", "opportunities"): (
        "id",
        "title",
        "account_id",
        "owner_id",
        "owner",
        "sales_stage",
        "amount",
        "probability",
        "expected_closure_date",
        "priority",
        "source",
        "sales_team_id",
        "region_id",
        "industry_id",
        "next_action",
        "creation_date",
        "modification_time",
        "project_id",
    ),
    ("crm", "activities"): (
        "id",
        "subject",
        "type",
        "status",
        "priority",
        "owner_id",
        "owner",
        "account_id",
        "contact_id",
        "opportunity_id",
        "start_date",
        "due_date",
        "creation_date",
    ),
    ("crm", "opportunity_reminders"): (
        "id",
        "description",
        "opportunity_id",
        "status",
        "assigned_to",
        "due_date",
    ),
    ("crm", "quotes"): ("id", "opportunity_id", "title", "status", "creation_date", "expiry_date"),
    ("crm", "quote_details"): ("quote_id", "quantity", "unit_price"),
    ("crm", "inventory_items"): (
        "id",
        "account_id",
        "product_id",
        "quantity",
        "status",
        "purchase_date",
        "support_start_date",
        "support_end_date",
    ),
    ("crm", "sales_teams"): ("id", "name", "description", "owner", "status"),
    ("crm", "sales_team_members"): ("sales_team_id", "user_id", "first_name", "last_name"),
    ("im", "tickets"): (
        "id",
        "short_name",
        "status",
        "priority",
        "owner",
        "account_id",
        "creation_time",
        "due_date",
    ),
    ("pm", "projects"): ("id", "name", "stage", "account_id", "start_date", "end_date"),
}

SECTION: Final = 50
NO_CURRENCY: Final = "Amounts are in the business's single currency; no currency is recorded."

# ---------------------------------------------------------------- SQL (constant; values bound)

_SEARCH_ACCOUNTS = """
SELECT id, name, owner, type, status, rating, phone, website, sales_team_id, modification_date
FROM accounts
WHERE IFNULL(status, 0) <> -1
  AND (:text IS NULL OR name LIKE :text ESCAPE '\\' OR description LIKE :text ESCAPE '\\')
  AND (:type IS NULL OR type = :type)
  AND (:status IS NULL OR status = :status)
  AND (:rating IS NULL OR rating = :rating)
  AND (:owned_by IS NULL OR owner LIKE :owned_by ESCAPE '\\')
  AND (:team IS NULL OR sales_team_id = :team)
ORDER BY CASE WHEN :sort = 'name' THEN name END COLLATE NOCASE,
         CASE WHEN :sort = 'modified' THEN modification_date END DESC, id
LIMIT :limit OFFSET :offset
"""
_ACCOUNT = """
SELECT id, name, description, owner, type, status, rating, phone, website, address, code, contact_id,
       sales_team_id, creation_date, modification_date
FROM accounts WHERE id = ? AND IFNULL(status, 0) <> -1
"""
_CONTACTS_OF = """
SELECT id, first_name, last_name, title, company, email, mobile, business_phone, status
FROM contacts WHERE account_id = ? AND IFNULL(status, 0) <> -1 ORDER BY last_name, first_name, id LIMIT ?
"""
_OPEN_OPPS_OF = """
SELECT id, title, sales_stage, amount, probability, expected_closure_date, owner, next_action
FROM opportunities WHERE account_id = ? AND sales_stage < 900000
ORDER BY expected_closure_date, id LIMIT ?
"""
_ACTIVITIES_OF = """
SELECT id, subject, type, status, priority, owner, due_date, creation_date, contact_id, opportunity_id
FROM activities WHERE account_id = ? AND IFNULL(status, 0) <> -1
ORDER BY IFNULL(due_date, creation_date) DESC, id DESC LIMIT ?
"""
_QUOTES_OF = """
SELECT q.id, q.title, q.status, q.creation_date, q.expiry_date, q.opportunity_id,
       (SELECT ROUND(IFNULL(SUM(d.quantity * d.unit_price), 0), 2) FROM quote_details d
        WHERE d.quote_id = q.id) AS computed_total
FROM quotes q JOIN opportunities o ON o.id = q.opportunity_id
WHERE o.account_id = ? ORDER BY q.creation_date DESC, q.id DESC LIMIT ?
"""
_INSTALLED_OF = """
SELECT id, product_id, quantity, status, purchase_date, support_start_date, support_end_date
FROM inventory_items WHERE account_id = ? ORDER BY support_end_date, id LIMIT ?
"""
_TICKETS_OF = """
SELECT id, short_name, status, priority, owner, creation_time, due_date FROM tickets
WHERE account_id = ? AND IFNULL(status, 0) NOT IN (5000, -1) ORDER BY creation_time DESC, id DESC LIMIT ?
"""
_PROJECTS_OF = """
SELECT id, name, stage, start_date, end_date FROM projects WHERE account_id = ?
ORDER BY start_date DESC, id DESC LIMIT ?
"""
_FIND_CONTACT = """
SELECT id, first_name, last_name, company, email, mobile, business_phone, account_id, status
FROM contacts
WHERE IFNULL(status, 0) <> -1 AND (
     lower(email) = :email
  OR (:email_domain IS NOT NULL AND lower(email) LIKE :email_domain ESCAPE '\\')
  OR (:last IS NOT NULL AND lower(last_name) = :last)
  OR (:tail IS NOT NULL AND replace(replace(replace(replace(replace(IFNULL(mobile, ''), ' ', ''), '-', ''),
        '(', ''), ')', ''), '.', '') LIKE :tail)
  OR (:tail IS NOT NULL AND replace(replace(replace(replace(replace(IFNULL(business_phone, ''), ' ', ''),
        '-', ''), '(', ''), ')', ''), '.', '') LIKE :tail))
LIMIT 500
"""
_FIND_ACCOUNT = """
SELECT id, name, website, phone, owner, status FROM accounts
WHERE IFNULL(status, 0) <> -1 AND (
     (:word IS NOT NULL AND lower(name) LIKE :word ESCAPE '\\')
  OR (:domain IS NOT NULL AND lower(website) LIKE :domain ESCAPE '\\')
  OR (:tail IS NOT NULL AND replace(replace(replace(replace(replace(IFNULL(phone, ''), ' ', ''), '-', ''),
        '(', ''), ')', ''), '.', '') LIKE :tail))
LIMIT 500
"""
_OPEN_OPPS = """
SELECT o.id, o.title, o.account_id, a.name AS account_name, o.owner_id, o.owner, o.sales_stage, o.amount,
       o.probability, o.expected_closure_date, o.sales_team_id, o.region_id, o.industry_id, o.next_action,
       o.creation_date, o.modification_time
FROM opportunities o LEFT JOIN accounts a ON a.id = o.account_id
WHERE o.sales_stage < 900000
  AND (:team IS NULL OR o.sales_team_id = :team)
  AND (:region IS NULL OR o.region_id = :region)
  AND (:industry IS NULL OR o.industry_id = :industry)
  AND (:owned_by IS NULL OR o.owner LIKE :owned_by ESCAPE '\\')
ORDER BY o.expected_closure_date, o.id LIMIT 5000
"""
_OVERDUE_REMINDERS = """
SELECT r.id, r.description, r.opportunity_id, r.status, r.assigned_to, r.due_date
FROM opportunity_reminders r JOIN opportunities o ON o.id = r.opportunity_id
WHERE o.sales_stage < 900000 AND typeof(r.status) = 'integer' AND r.status NOT IN (61000, 70000, -1)
  AND substr(r.due_date, 1, 10) < :today
ORDER BY r.due_date, r.id LIMIT 200
"""
_TEAMS = (
    "SELECT id, name, description, owner, status FROM sales_teams WHERE IFNULL(status, 0) <> -1 ORDER BY name"
)
_TEAM_MEMBERS = (
    "SELECT sales_team_id, user_id, first_name, last_name FROM sales_team_members ORDER BY last_name"
)
_CONTACT = """
SELECT id, first_name, last_name, company, account_id FROM contacts WHERE id = ? AND IFNULL(status, 0) <> -1
"""
_TL_ACTIVITIES = """
SELECT id, subject, type, status, owner, due_date, creation_date, opportunity_id, contact_id FROM activities
WHERE (account_id = :account OR contact_id = :contact) AND IFNULL(status, 0) <> -1 LIMIT 1000
"""
_TL_OPPS = """
SELECT id, title, sales_stage, amount, creation_date, expected_closure_date, owner FROM opportunities
WHERE account_id = :account LIMIT 1000
"""
_TL_QUOTES = """
SELECT q.id, q.title, q.status, q.creation_date, q.opportunity_id FROM quotes q
JOIN opportunities o ON o.id = q.opportunity_id WHERE o.account_id = :account LIMIT 1000
"""
_TL_TICKETS = """
SELECT id, short_name, status, creation_time FROM tickets
WHERE account_id = :account AND IFNULL(status, 0) <> -1 LIMIT 1000
"""
_TL_INSTALLED = """
SELECT id, product_id, quantity, purchase_date FROM inventory_items WHERE account_id = :account LIMIT 1000
"""


# ---------------------------------------------------------------- result models


class Page(BaseModel):
    rows: list[dict[str, Any]]
    next_cursor: str | None = Field(description="Pass to the same call for the next page; null at the end")


class Customer360(BaseModel):
    account: dict[str, Any]
    primary_contact: dict[str, Any] | None = None
    contacts: list[dict[str, Any]] | None = None
    open_opportunities: list[dict[str, Any]] | None = None
    recent_activities: list[dict[str, Any]] | None = None
    quotes: list[dict[str, Any]] | None = None
    open_tickets: list[dict[str, Any]] | None = None
    projects: list[dict[str, Any]] | None = None
    products_owned: list[dict[str, Any]] | None = None
    notes: list[str] = Field(description="What this overview does not include, and why")


class Candidate(BaseModel):
    record: dict[str, Any]
    score: float = Field(description="0-1; 1 is a certain match")
    reasons: list[str]


class Candidates(BaseModel):
    candidates: list[Candidate]


class Group(BaseModel):
    key: str
    label: str | None
    count: int
    amount: float
    weighted: float


class Pipeline(BaseModel):
    group_by: str
    groups: list[Group]
    open_count: int
    open_amount: float
    weighted_amount: float
    stale: list[dict[str, Any]] = Field(description="Open deals unchanged for at least stale_days")
    past_expected_close: list[dict[str, Any]]
    overdue_follow_ups: list[dict[str, Any]] | None = Field(
        default=None, description="Open follow-up tasks past due"
    )
    note: str = NO_CURRENCY


class ForecastPeriod(BaseModel):
    period: str = Field(description="YYYY-MM, or 'unscheduled' when no close date is set")
    count: int
    best_case: float = Field(description="Sum of amount over all open deals closing in the period")
    commit: float = Field(description="Sum of amount over deals at or above the commit probability")
    weighted: float = Field(description="Sum of amount x probability")


class Forecast(BaseModel):
    periods: list[ForecastPeriod]
    by_owner: list[ForecastPeriod]
    by_team: list[ForecastPeriod]
    commit_probability: float
    note: str = NO_CURRENCY


class SalesTeams(BaseModel):
    teams: list[dict[str, Any]]


class TimelinePage(BaseModel):
    subject: str
    events: list[dict[str, Any]]
    next_cursor: str | None


# ---------------------------------------------------------------- helpers

_LEGAL: Final = frozenset(
    {
        "as",
        "asa",
        "ab",
        "ltd",
        "limited",
        "inc",
        "llc",
        "gmbh",
        "co",
        "corp",
        "corporation",
        "company",
        "plc",
        "sa",
        "bv",
        "oy",
        "aps",
        "ag",
        "the",
    }
)
_WORD: Final = re.compile(r"[^\w]+", re.UNICODE)


def _digits(phone: str | None) -> str:
    """Phone digits for comparison: separators dropped, an international 00 or + prefix and leading
    zeros removed (an approximation of E.164 without a default country)."""
    d = re.sub(r"\D", "", phone or "")
    return d[2:].lstrip("0") if d.startswith("00") else d.lstrip("0")


def _same_phone(a: str | None, b: str | None) -> bool:
    x, y = _digits(a), _digits(b)
    if len(x) < 8 or len(y) < 8:  # noqa: PLR2004 - fewer digits are too weak to call a match
        return False
    return x.endswith(y) or y.endswith(x)


def _norm_name(*parts: str | None) -> str:
    return " ".join(w for p in parts for w in _WORD.split((p or "").casefold()) if w)


def _norm_company(name: str | None) -> str:
    return " ".join(w for w in _norm_name(name).split() if w not in _LEGAL)


def _domain(text: str | None) -> str | None:
    t = (text or "").strip().casefold()
    if "@" in t:
        t = t.rsplit("@", 1)[1]
    t = re.sub(r"^[a-z]+://", "", t).split("/", 1)[0].split(":", 1)[0]
    t = t.removeprefix("www.")
    return t if "." in t else None


def _combine(scores: list[float]) -> float:
    miss = 1.0
    for s in scores:
        miss *= 1.0 - s
    return round(1.0 - miss, 3)


def _probability(value: Any) -> float:
    try:
        p = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if p > 1:
        p /= 100.0
    return max(0.0, min(p, 1.0))


def _opp(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("crm", "opportunity", row["id"])
    if "account_id" in row:
        row["account"] = handle("crm", "account", row.pop("account_id"))
    if "probability" in row:
        row["probability"] = _probability(row["probability"])
    stage = row.get("sales_stage")
    row["is_open"] = None if stage is None else int(stage) < OPEN_BELOW
    return with_labels(row, sales_stage=SALES_STAGE, priority=PRIORITY, source=SOURCE)


def _account(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("crm", "account", row["id"])
    return with_labels(row, type=BUSINESS_TYPE, status=ACTIVE)


def _contact(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("crm", "contact", row["id"])
    if "account_id" in row:
        row["account"] = handle("crm", "account", row.pop("account_id"))
    return with_labels(row, status=ACTIVE)


def activity_row(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("crm", "activity", row["id"])
    for ref, model in (("contact_id", "contact"), ("opportunity_id", "opportunity")):
        if ref in row:
            row[model] = handle("crm", model, row.pop(ref))
    return with_labels(row, type=ACTIVITY_TYPE, status=TASK_STATUS, priority=ACTIVITY_PRIORITY)


def _month(text: str | None) -> str:
    day = parse_day(text)
    return day.strftime("%Y-%m") if day else "unscheduled"


# ---------------------------------------------------------------- tools


def register(reg: Registrar, ctx: ToolContext) -> None:  # noqa: PLR0915 - one closure per tool
    def search_accounts(
        *,
        text: ShortText | None = None,
        type: int | None = None,
        status: int | None = None,
        rating: int | None = None,
        owned_by: ShortText | None = None,
        sales_team_id: int | None = None,
        sort: Literal["name", "modified"] = "name",
        limit: int = 25,
        cursor: CursorText | None = None,
    ) -> Page:
        """Find customer accounts by words in the name or description, business type (1 Public Company,
        2 Private Company, 3 Individual, 4 Govt., 5 Small Business, 6 Non-Profit), status (1000 Active,
        5000 Inactive), rating, account manager name or sales team. Page with `cursor` (limit 1-100)."""
        caller = ctx.caller()
        ctx.require(caller, ACCOUNT)
        args = {
            "text": text,
            "type": type,
            "status": status,
            "rating": rating,
            "owned_by": owned_by,
            "sales_team_id": sales_team_id,
            "sort": sort,
            "limit": limit,
        }
        size = ctx.page_size(limit)
        offset = ctx.offset("crm.search_accounts", args, caller, cursor)
        rows = ctx.rows(
            "crm",
            _SEARCH_ACCOUNTS,
            {
                "text": like(text) if text else None,
                "type": type,
                "status": status,
                "rating": rating,
                "owned_by": like(owned_by) if owned_by else None,
                "team": sales_team_id,
                "sort": sort,
                "limit": size + 1,
                "offset": offset,
            },
        )
        return Page(
            rows=[_account(r) for r in rows[:size]],
            next_cursor=ctx.next_cursor(
                "crm.search_accounts", args, caller, offset=offset, limit=size, fetched=len(rows)
            ),
        )

    reg(search_accounts, "crm.search_accounts", "Search accounts")

    def get_account(account: HandleText) -> Customer360:
        """Everything about one customer (handle crm:account:<id>): contacts, open opportunities,
        recent activities, quotes with computed totals, open support tickets, projects and products
        owned. Sections from modules you cannot read are null."""
        caller = ctx.caller()
        ctx.require_record(caller, ACCOUNT, f"account {account}")
        aid = record_id(account, "crm", "account", "account")
        row = ctx.one("crm", _ACCOUNT, (aid,))
        if row is None:
            raise NotFound(f"account {account} not found")
        out = Customer360(
            account=_account(dict(row)),
            notes=[
                "Money owed (cfm receivables) and orders (scm) are not included: their status codes "
                "have no decided meaning yet (MPC001-253).",
                NO_CURRENCY,
            ],
        )
        if ctx.allowed(caller, CONTACT):
            contacts = [_contact(r) for r in ctx.rows("crm", _CONTACTS_OF, (aid, SECTION))]
            out.contacts = contacts
            out.primary_contact = next((c for c in contacts if c["id"] == row.get("contact_id")), None)
        if ctx.allowed(caller, OPPORTUNITY):
            out.open_opportunities = [_opp(r) for r in ctx.rows("crm", _OPEN_OPPS_OF, (aid, SECTION))]
        if ctx.allowed(caller, ACTIVITY):
            out.recent_activities = [activity_row(r) for r in ctx.rows("crm", _ACTIVITIES_OF, (aid, 20))]
        if ctx.allowed(caller, QUOTE):
            out.quotes = [
                {
                    **r,
                    "handle": handle("crm", "quote", r["id"]),
                    "opportunity": handle("crm", "opportunity", r.pop("opportunity_id")),
                }
                for r in ctx.rows("crm", _QUOTES_OF, (aid, SECTION))
            ]
        if ctx.allowed(caller, TICKET):
            out.open_tickets = [
                with_labels(
                    {**r, "handle": handle("im", "ticket", r["id"])}, status=TICKET_STATUS, priority=PRIORITY
                )
                for r in ctx.rows("im", _TICKETS_OF, (aid, SECTION))
            ]
        if ctx.allowed(caller, PROJECT):
            out.projects = [
                with_labels({**r, "handle": handle("pm", "project", r["id"])}, stage=PROJECT_STAGE)
                for r in ctx.rows("pm", _PROJECTS_OF, (aid, SECTION))
            ]
        if ctx.allowed(caller, INSTALLED):
            out.products_owned = [
                {**r, "handle": handle("crm", "inventory_item", r["id"])}
                for r in ctx.rows("crm", _INSTALLED_OF, (aid, SECTION))
            ]
        return out

    reg(get_account, "crm.get_account", "Customer 360")

    def find_contact(
        *,
        email: ShortText | None = None,
        phone: ShortText | None = None,
        first_name: ShortText | None = None,
        last_name: ShortText | None = None,
    ) -> Candidates:
        """Check whether a person is already in the CRM. Returns ranked candidates with a score and the
        reasons: same email, same phone (compared by digits), same name, or same email domain."""
        caller = ctx.caller()
        ctx.require(caller, CONTACT)
        if not any((email, phone, last_name)):
            raise ValidationError("give an email, a phone or a last name", field="email")
        mail = (email or "").strip().casefold() or None
        dom = _domain(mail) if mail else None
        tail = _digits(phone)[-7:] if phone and len(_digits(phone)) >= 7 else None  # noqa: PLR2004
        rows = ctx.rows(
            "crm",
            _FIND_CONTACT,
            {
                "email": mail,
                "email_domain": like("@" + dom) if dom else None,
                "last": (last_name or "").strip().casefold() or None,
                "tail": "%" + tail if tail else None,
            },
        )
        wanted = _norm_name(first_name, last_name)
        out: list[Candidate] = []
        for r in rows:
            scores, reasons = [], []
            if mail and (r.get("email") or "").strip().casefold() == mail:
                scores.append(1.0)
                reasons.append("same email")
            if phone and (_same_phone(phone, r.get("mobile")) or _same_phone(phone, r.get("business_phone"))):
                scores.append(0.9)
                reasons.append("same phone number")
            have = _norm_name(r.get("first_name"), r.get("last_name"))
            if first_name and last_name and have == wanted:
                scores.append(0.7)
                reasons.append("same name")
            elif last_name and _norm_name(r.get("last_name")) == _norm_name(last_name):
                scores.append(0.3)
                reasons.append("same last name")
            if dom and _domain(r.get("email")) == dom and "same email" not in reasons:
                scores.append(0.2)
                reasons.append("same email domain")
            if scores:
                out.append(Candidate(record=_contact(r), score=_combine(scores), reasons=reasons))
        out.sort(key=lambda c: (-c.score, c.record["id"]))
        return Candidates(candidates=out[:20])

    reg(find_contact, "crm.find_contact", "Find a contact")

    def find_account(
        *, name: ShortText | None = None, website: ShortText | None = None, phone: ShortText | None = None
    ) -> Candidates:
        """Check whether a company is already an account. Returns ranked candidates with a score and the
        reasons: same name (ignoring legal forms such as AS, Ltd, Inc), same website domain, same phone."""
        caller = ctx.caller()
        ctx.require(caller, ACCOUNT)
        if not any((name, website, phone)):
            raise ValidationError("give a name, a website or a phone", field="name")
        norm = _norm_company(name)
        word = max(norm.split(), key=len) if norm else None
        dom = _domain(website)
        tail = _digits(phone)[-7:] if phone and len(_digits(phone)) >= 7 else None  # noqa: PLR2004
        rows = ctx.rows(
            "crm",
            _FIND_ACCOUNT,
            {
                "word": like(word) if word else None,
                "domain": like(dom) if dom else None,
                "tail": "%" + tail if tail else None,
            },
        )
        out: list[Candidate] = []
        for r in rows:
            scores, reasons = [], []
            have = _norm_company(r.get("name"))
            if norm and have == norm:
                scores.append(0.8)
                reasons.append("same name")
            elif norm and have and (norm in have or have in norm):
                scores.append(0.4)
                reasons.append("similar name")
            if dom and _domain(r.get("website")) == dom:
                scores.append(0.9)
                reasons.append("same website domain")
            if phone and _same_phone(phone, r.get("phone")):
                scores.append(0.7)
                reasons.append("same phone number")
            if scores:
                out.append(Candidate(record=_account(r), score=_combine(scores), reasons=reasons))
        out.sort(key=lambda c: (-c.score, c.record["id"]))
        return Candidates(candidates=out[:20])

    reg(find_account, "crm.find_account", "Find an account")

    def _open_opps(
        sales_team_id: int | None, region_id: int | None, industry_id: int | None, owned_by: str | None
    ) -> list[dict[str, Any]]:
        return ctx.rows(
            "crm",
            _OPEN_OPPS,
            {
                "team": sales_team_id,
                "region": region_id,
                "industry": industry_id,
                "owned_by": like(owned_by) if owned_by else None,
            },
        )

    def pipeline(
        *,
        group_by: Literal["stage", "owner", "team", "close_month"] = "stage",
        stale_days: int = 30,
        sales_team_id: int | None = None,
        region_id: int | None = None,
        industry_id: int | None = None,
        owned_by: ShortText | None = None,
    ) -> Pipeline:
        """The open sales pipeline (deals with sales_stage below 900000) grouped by stage, owner, sales
        team or expected close month, with counts, amounts and probability-weighted amounts; deals with
        no change for `stale_days`; deals past their expected close date; and overdue follow-up tasks.
        Filter by sales team, region, industry or owner name."""
        caller = ctx.caller()
        ctx.require(caller, OPPORTUNITY)
        now = ctx.now()
        today = now.date()
        stale_before = stamp(now - dt.timedelta(days=max(1, stale_days)))
        opps = [_opp(r) for r in _open_opps(sales_team_id, region_id, industry_id, owned_by)]
        teams = {t["id"]: t["name"] for t in ctx.rows("crm", _TEAMS)} if ctx.allowed(caller, TEAM) else {}
        groups: dict[str, Group] = {}
        for o in opps:
            if group_by == "stage":
                key, name = str(o.get("sales_stage")), o.get("sales_stage_label")
            elif group_by == "owner":
                key = name = o.get("owner") or "(no owner)"
            elif group_by == "team":
                tid = o.get("sales_team_id") or 0
                key, name = str(tid), teams.get(tid) or ("(no team)" if not tid else None)
            else:
                key = name = _month(o.get("expected_closure_date"))
            g = groups.setdefault(key, Group(key=key, label=name, count=0, amount=0.0, weighted=0.0))
            amount = float(o.get("amount") or 0)
            g.count += 1
            g.amount = round(g.amount + amount, 2)
            g.weighted = round(g.weighted + amount * o["probability"], 2)
        stale = [
            o for o in opps if (o.get("modification_time") or o.get("creation_date") or "") < stale_before
        ]
        past = [o for o in opps if (d := parse_day(o.get("expected_closure_date"))) and d < today]
        overdue = None
        if ctx.allowed(caller, REMINDER):
            open_ids = {o["id"] for o in opps}
            overdue = [
                with_labels(
                    {**r, "opportunity": handle("crm", "opportunity", r["opportunity_id"])},
                    status=TASK_STATUS,
                )
                for r in ctx.rows("crm", _OVERDUE_REMINDERS, {"today": today.isoformat()})
                if r["opportunity_id"] in open_ids
            ]
        return Pipeline(
            group_by=group_by,
            groups=sorted(groups.values(), key=lambda g: g.key),
            open_count=len(opps),
            open_amount=round(sum(float(o.get("amount") or 0) for o in opps), 2),
            weighted_amount=round(sum(float(o.get("amount") or 0) * o["probability"] for o in opps), 2),
            stale=stale[:SECTION],
            past_expected_close=past[:SECTION],
            overdue_follow_ups=overdue,
        )

    reg(pipeline, "crm.pipeline", "Sales pipeline")

    def forecast(
        *,
        months: int = 6,
        commit_probability: float = 0.7,
        sales_team_id: int | None = None,
        owned_by: ShortText | None = None,
    ) -> Forecast:
        """Expected revenue by close month for the next 1-24 months (from this month), and by owner and
        sales team: best case (every open deal), commit (deals at or above `commit_probability`, 0-1) and
        weighted (amount x probability). Deals with no close date are 'unscheduled'."""
        caller = ctx.caller()
        ctx.require(caller, OPPORTUNITY)
        n = max(1, min(months, 24))
        threshold = max(0.0, min(commit_probability, 1.0))
        first = ctx.now().date().replace(day=1)
        window: list[str] = []
        y, m = first.year, first.month
        for _ in range(n):
            window.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)  # noqa: PLR2004
        opps = [_opp(r) for r in _open_opps(sales_team_id, None, None, owned_by)]
        teams = {t["id"]: t["name"] for t in ctx.rows("crm", _TEAMS)} if ctx.allowed(caller, TEAM) else {}

        def bucket(keyfn: Any) -> list[ForecastPeriod]:
            acc: dict[str, ForecastPeriod] = {}
            for o in opps:
                month = _month(o.get("expected_closure_date"))
                if month != "unscheduled" and month not in window:
                    continue
                key = keyfn(o, month)
                p = acc.setdefault(
                    key, ForecastPeriod(period=key, count=0, best_case=0, commit=0, weighted=0)
                )
                amount = float(o.get("amount") or 0)
                p.count += 1
                p.best_case = round(p.best_case + amount, 2)
                p.weighted = round(p.weighted + amount * o["probability"], 2)
                if o["probability"] >= threshold:
                    p.commit = round(p.commit + amount, 2)
            return sorted(acc.values(), key=lambda p: p.period)

        periods = {p.period: p for p in bucket(lambda o, month: month)}
        ordered = [
            periods.get(w) or ForecastPeriod(period=w, count=0, best_case=0, commit=0, weighted=0)
            for w in window
        ]
        if "unscheduled" in periods:
            ordered.append(periods["unscheduled"])
        return Forecast(
            periods=ordered,
            by_owner=bucket(lambda o, month: o.get("owner") or "(no owner)"),
            by_team=bucket(
                lambda o, month: (
                    teams.get(o.get("sales_team_id") or 0) or str(o.get("sales_team_id") or "(no team)")
                )
            ),
            commit_probability=threshold,
        )

    reg(forecast, "crm.forecast", "Sales forecast")

    def list_sales_teams() -> SalesTeams:
        """Sales teams with their members, for filtering the pipeline, forecast and account search."""
        caller = ctx.caller()
        ctx.require(caller, TEAM)
        members: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in ctx.rows("crm", _TEAM_MEMBERS):
            members[int(r["sales_team_id"] or 0)].append(
                {"user_id": r["user_id"], "name": _norm_name(r["first_name"], r["last_name"]).title() or None}
            )
        return SalesTeams(
            teams=[{**t, "members": members.get(int(t["id"]), [])} for t in ctx.rows("crm", _TEAMS)]
        )

    reg(list_sales_teams, "crm.list_sales_teams", "Sales teams")

    def timeline(  # noqa: PLR0912 - one branch per guarded section
        *,
        account: HandleText | None = None,
        contact: HandleText | None = None,
        limit: int = 25,
        cursor: CursorText | None = None,
    ) -> TimelinePage:
        """What has happened with one account (crm:account:<id>) or contact (crm:contact:<id>), newest
        first: activities, opportunities created and expected to close, quotes, support tickets and
        products bought. Stage-change history is not kept by webos, so each deal shows its current stage."""
        caller = ctx.caller()
        if (account is None) == (contact is None):
            raise ValidationError("give exactly one of account or contact", field="account")
        args = {"account": account, "contact": contact, "limit": limit}
        size = ctx.page_size(limit)
        offset = ctx.offset("crm.timeline", args, caller, cursor)
        if account:
            ctx.require_record(caller, ACCOUNT, f"account {account}")
            aid = record_id(account, "crm", "account", "account")
            if ctx.one("crm", _ACCOUNT, (aid,)) is None:
                raise NotFound(f"account {account} not found")
            cid, subject = -1, account
        else:
            assert contact is not None  # noqa: S101 - checked above
            ctx.require_record(caller, CONTACT, f"contact {contact}")
            cid = record_id(contact, "crm", "contact", "contact")
            if ctx.one("crm", _CONTACT, (cid,)) is None:
                raise NotFound(f"contact {contact} not found")
            aid, subject = -1, contact
        keys = {"account": aid, "contact": cid}
        events: list[dict[str, Any]] = []
        if ctx.allowed(caller, ACTIVITY):
            for r in ctx.rows("crm", _TL_ACTIVITIES, keys):
                events.append(
                    {
                        "when": r.get("due_date") or r.get("creation_date"),
                        "kind": "activity",
                        **activity_row(r),
                    }
                )
        if aid > 0 and ctx.allowed(caller, OPPORTUNITY):
            for r in ctx.rows("crm", _TL_OPPS, keys):
                o = _opp(r)
                events.append({"when": o.get("creation_date"), "kind": "opportunity created", **o})
                if o.get("expected_closure_date"):
                    events.append(
                        {"when": o["expected_closure_date"], "kind": "opportunity expected close", **o}
                    )
        if aid > 0 and ctx.allowed(caller, QUOTE):
            for r in ctx.rows("crm", _TL_QUOTES, keys):
                events.append(
                    {
                        "when": r.get("creation_date"),
                        "kind": "quote",
                        "handle": handle("crm", "quote", r["id"]),
                        **r,
                    }
                )
        if aid > 0 and ctx.allowed(caller, TICKET):
            for r in ctx.rows("im", _TL_TICKETS, keys):
                events.append(
                    {
                        "when": r.get("creation_time"),
                        "kind": "ticket",
                        **with_labels({**r, "handle": handle("im", "ticket", r["id"])}, status=TICKET_STATUS),
                    }
                )
        if aid > 0 and ctx.allowed(caller, INSTALLED):
            for r in ctx.rows("crm", _TL_INSTALLED, keys):
                events.append(
                    {
                        "when": r.get("purchase_date"),
                        "kind": "product bought",
                        "handle": handle("crm", "inventory_item", r["id"]),
                        **r,
                    }
                )
        events.sort(key=lambda e: (str(e.get("when") or ""), e["kind"], str(e.get("handle"))), reverse=True)
        page = events[offset : offset + size + 1]
        return TimelinePage(
            subject=subject,
            events=page[:size],
            next_cursor=ctx.next_cursor(
                "crm.timeline", args, caller, offset=offset, limit=size, fetched=len(page)
            ),
        )

    reg(timeline, "crm.timeline", "Account or contact timeline")
