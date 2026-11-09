"""Prototype: a minimal read-only MCP server over the webos CRM database (crm.s3db).

Throwaway exploration code for cerp, not part of the package. Modelled on im_server.py. Follows
docs/webos/crm/crm.INSTRUCTIONS.md (read-only connections when looking, bound parameters) and the code
meanings in docs/webos/crm/crm.md section 3. It opens the database read-only (MPC001-162), builds SQL only
from fixed identifiers with bound values (MPC001-065), and serves the copy next to this file by default.

crm.s3db is schema-only: every table is empty and, apart from sales_stage, no code domain (status, type,
priority, rating, source) is established (crm.md section 3). So unlike im_server this prototype returns
those codes raw, with no *_label, rather than inventing meanings. The one authoritative fact it uses is
that opportunities.sales_stage < 900000 means open (ActivitySummary.cpp:235).

Run from the cerp repository root:

    uv run python workspace/prototype/crm_server.py              # stdio (Claude Desktop / Claude Code)
    uv run python workspace/prototype/crm_server.py --http       # http://127.0.0.1:7413/mcp
    uv run python workspace/prototype/crm_server.py --db other.s3db

Add to Claude Code:

    claude mcp add crm-proto -- uv run --directory <cerp> python workspace/prototype/crm_server.py
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

DEFAULT_DB = Path(__file__).with_name("crm.s3db")
MAX_LIMIT = 100
OPEN_BELOW = 900000  # sales_stage < this is open / in-pipeline; >= is terminal (crm.md section 3.2)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Columns returned in search results; the full row comes from the crm.get_* tools.
ACCOUNT_COLUMNS = ("id", "name", "owner_id", "owner", "type", "status", "rating", "phone", "website",
                   "contact_id", "sales_team_id", "modification_date")  # fmt: skip
CONTACT_COLUMNS = ("id", "first_name", "last_name", "title", "company", "email", "mobile", "business_phone",
                   "account_id", "vendor_id", "status")  # fmt: skip
LEAD_COLUMNS = ("id", "title", "status", "type", "source", "company", "first_name", "last_name", "email",
                "phone", "owner_id", "owner", "account_id", "contact_id", "creation_date",
                "modification_date")  # fmt: skip
OPPORTUNITY_COLUMNS = ("id", "title", "account_id", "owner_id", "owner", "sales_stage", "amount",
                       "probability", "expected_closure_date", "priority", "rating", "lead_id", "quote_id",
                       "is_late", "is_risk", "creation_date", "modification_time")  # fmt: skip


# ---------------------------------------------------------------- data access


class Database:
    """Read-only access to one crm.s3db file. One short-lived connection per call."""

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


def _opportunity(row: dict[str, Any]) -> dict[str, Any]:
    """Add the derived is_open flag to an opportunity row."""
    stage = row.get("sales_stage")
    row["is_open"] = None if stage is None else stage < OPEN_BELOW
    return row


def _contains(value: str) -> str:
    """LIKE pattern matching value as a literal, case-insensitive substring (use with ESCAPE '\\')."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Filter:
    """Builds a WHERE clause from fixed column names and bound values."""

    def __init__(self) -> None:
        self.where: list[str] = []
        self.params: list[Any] = []

    def equals(self, column: str, value: Any) -> None:
        if value is not None:
            self.where.append(f"{column} = ?")
            self.params.append(value)

    def contains(self, columns: tuple[str, ...], value: str | None) -> None:
        if value:
            self.where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in columns) + ")")
            self.params += [_contains(value)] * len(columns)

    def raw(self, clause: str, *params: Any) -> None:
        self.where.append(clause)
        self.params += params

    @property
    def clause(self) -> str:
        return (" WHERE " + " AND ".join(self.where)) if self.where else ""


# ---------------------------------------------------------------- result models


class Page(BaseModel):
    total: int = Field(description="Number of rows matching the filters")
    offset: int
    limit: int
    rows: list[dict[str, Any]]


class AccountDetail(BaseModel):
    account: dict[str, Any]
    primary_contact: dict[str, Any] | None
    contacts: list[dict[str, Any]]
    opportunities: list[dict[str, Any]]
    leads: list[dict[str, Any]]
    inventory_items: list[dict[str, Any]]


class OpportunityDetail(BaseModel):
    opportunity: dict[str, Any]
    account: dict[str, Any] | None
    lead: dict[str, Any] | None
    quotes: list[dict[str, Any]] = Field(description="Quotes with their line items and computed total")
    activities: list[dict[str, Any]]
    reminders: list[dict[str, Any]]


class Count(BaseModel):
    value: Any
    count: int
    amount: float | None = None


class PipelineStats(BaseModel):
    accounts: int
    contacts: int
    leads: int
    opportunities: int
    open_opportunities: int
    open_amount: float = Field(description="Sum of amount over open opportunities (no currency is recorded)")
    weighted_open_amount: float = Field(description="Sum of amount * probability over open opportunities")
    by_sales_stage: list[Count]
    by_owner: list[Count]


# ---------------------------------------------------------------- server


def build_server(db: Database) -> MCPServer:  # noqa: PLR0915 - one registration per tool
    mcp = MCPServer(
        name="crm-prototype",
        title="webos CRM (prototype)",
        instructions=(
            "Read-only access to a webos CRM database: accounts, contacts, leads, opportunities, quotes, "
            "activities and installed-base inventory. The pipeline is leads -> opportunities -> quotes. "
            "The only code with an established meaning is opportunities.sales_stage: below 900000 is "
            "open, 900000 and above is closed (the individual stage names are not defined anywhere), so "
            "opportunities carry a derived is_open flag. status, type, priority, rating and source are "
            "returned as raw integers with no meaning known; do not guess what they stand for. Money "
            "has no currency. The database may be empty. Use crm.pipeline_stats for an overview, the "
            "crm.search_* tools to find records and the crm.get_* tools for one record in full."
        ),
    )

    def page(table: str, columns: tuple[str, ...], f: Filter, order: str, *, limit: int, offset: int) -> Page:
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        # table, columns and order are fixed names from this module, never caller input
        total = db.one(f"SELECT COUNT(*) AS n FROM {table}{f.clause}", tuple(f.params))  # noqa: S608
        rows = db.rows(
            f"SELECT {', '.join(columns)} FROM {table}{f.clause} ORDER BY {order} LIMIT ? OFFSET ?",  # noqa: S608
            (*f.params, limit, offset),
        )
        return Page(total=total["n"] if total else 0, offset=offset, limit=limit, rows=rows)

    @mcp.tool(name="crm.pipeline_stats", title="Pipeline statistics", annotations=READ_ONLY)
    def pipeline_stats() -> PipelineStats:
        """Count records overall, and summarise the open pipeline by sales stage and by owner."""

        def n(table: str) -> int:
            found = db.one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
            return found["n"] if found else 0

        open_totals = db.one(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount, "
            "COALESCE(SUM(amount * probability), 0) AS weighted FROM opportunities WHERE sales_stage < ?",
            (OPEN_BELOW,),
        )
        by_stage = db.rows(
            "SELECT sales_stage AS value, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount "
            "FROM opportunities GROUP BY sales_stage ORDER BY sales_stage"
        )
        by_owner = db.rows(
            "SELECT owner AS value, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount "
            "FROM opportunities WHERE sales_stage < ? GROUP BY owner ORDER BY amount DESC",
            (OPEN_BELOW,),
        )
        return PipelineStats(
            accounts=n("accounts"),
            contacts=n("contacts"),
            leads=n("leads"),
            opportunities=n("opportunities"),
            open_opportunities=open_totals["n"] if open_totals else 0,
            open_amount=open_totals["amount"] if open_totals else 0.0,
            weighted_open_amount=open_totals["weighted"] if open_totals else 0.0,
            by_sales_stage=[Count(value=r["value"], count=r["n"], amount=r["amount"]) for r in by_stage],
            by_owner=[Count(value=r["value"], count=r["n"], amount=r["amount"]) for r in by_owner],
        )

    @mcp.tool(name="crm.search_accounts", title="Search accounts", annotations=READ_ONLY)
    def search_accounts(
        *, text: str | None = None, owner: str | None = None, limit: int = 25, offset: int = 0
    ) -> Page:
        """Find customer accounts, by name.

        text: words to look for in the account name or description (case-insensitive substring).
        owner: account manager name, matched as a case-insensitive substring.
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        f = Filter()
        f.contains(("name", "description"), text)
        f.contains(("owner",), owner)
        return page("accounts", ACCOUNT_COLUMNS, f, "name, id", limit=limit, offset=offset)

    @mcp.tool(name="crm.get_account", title="Get account", annotations=READ_ONLY)
    def get_account(account_id: int) -> AccountDetail:
        """Return one account with its contacts, opportunities, leads and installed-base items."""
        account = db.one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        if account is None:
            raise ToolError(f"account {account_id} not found")
        return AccountDetail(
            account=account,
            primary_contact=db.one("SELECT * FROM contacts WHERE id = ?", (account["contact_id"],)),
            contacts=db.rows(
                f"SELECT {', '.join(CONTACT_COLUMNS)} FROM contacts WHERE account_id = ? "  # noqa: S608
                "ORDER BY last_name, first_name",
                (account_id,),
            ),
            opportunities=[
                _opportunity(o)
                for o in db.rows(
                    f"SELECT {', '.join(OPPORTUNITY_COLUMNS)} FROM opportunities "  # noqa: S608
                    "WHERE account_id = ? ORDER BY creation_date DESC, id DESC",
                    (account_id,),
                )
            ],
            leads=db.rows(
                f"SELECT {', '.join(LEAD_COLUMNS)} FROM leads WHERE account_id = ? "  # noqa: S608
                "ORDER BY creation_date DESC, id DESC",
                (account_id,),
            ),
            inventory_items=db.rows(
                "SELECT * FROM inventory_items WHERE account_id = ? ORDER BY support_end_date", (account_id,)
            ),
        )

    @mcp.tool(name="crm.search_contacts", title="Search contacts", annotations=READ_ONLY)
    def search_contacts(
        *,
        text: str | None = None,
        account_id: int | None = None,
        vendor_id: int | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page:
        """Find contacts (people) by name, email or company.

        text: words to look for in first name, last name, email or company (case-insensitive substring).
        account_id: only contacts of this account. vendor_id: only contacts of this vendor.
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        f = Filter()
        f.contains(("first_name", "last_name", "email", "company"), text)
        f.equals("account_id", account_id)
        f.equals("vendor_id", vendor_id)
        return page("contacts", CONTACT_COLUMNS, f, "last_name, first_name, id", limit=limit, offset=offset)

    @mcp.tool(name="crm.search_leads", title="Search leads", annotations=READ_ONLY)
    def search_leads(
        *,
        text: str | None = None,
        status: int | None = None,
        owner: str | None = None,
        account_id: int | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page:
        """Find leads, newest first.

        text: words to look for in the title, description, company, contact name or email.
        status: raw status code (its meaning is not defined for the CRM database).
        owner: owner name, matched as a case-insensitive substring.
        account_id: only leads already tied to this account.
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        f = Filter()
        f.contains(("title", "description", "company", "first_name", "last_name", "email"), text)
        f.contains(("owner",), owner)
        f.equals("status", status)
        f.equals("account_id", account_id)
        return page("leads", LEAD_COLUMNS, f, "creation_date DESC, id DESC", limit=limit, offset=offset)

    @mcp.tool(name="crm.search_opportunities", title="Search opportunities", annotations=READ_ONLY)
    def search_opportunities(
        *,
        text: str | None = None,
        open_only: bool | None = None,
        sales_stage: int | None = None,
        account_id: int | None = None,
        owner: str | None = None,
        min_amount: float | None = None,
        closing_before: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page:
        """Find opportunities (deals), largest amount first.

        text: words to look for in the title or description (case-insensitive substring).
        open_only: true for open deals (sales_stage < 900000), false for closed ones, omit for both.
        sales_stage: exact stage code.
        account_id: only deals for this account (id from crm.search_accounts).
        owner: salesperson name, matched as a case-insensitive substring.
        min_amount: only deals worth at least this much (no currency is recorded).
        closing_before: expected closure date before this, as YYYY-MM-DD.
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        f = Filter()
        f.contains(("title", "description"), text)
        f.contains(("owner",), owner)
        f.equals("sales_stage", sales_stage)
        f.equals("account_id", account_id)
        if open_only is True:
            f.raw("sales_stage < ?", OPEN_BELOW)
        elif open_only is False:
            f.raw("sales_stage >= ?", OPEN_BELOW)
        if min_amount is not None:
            f.raw("amount >= ?", min_amount)
        if closing_before:
            f.raw("expected_closure_date < ?", closing_before)
        result = page(
            "opportunities", OPPORTUNITY_COLUMNS, f, "amount DESC, id DESC", limit=limit, offset=offset
        )
        result.rows = [_opportunity(r) for r in result.rows]
        return result

    @mcp.tool(name="crm.get_opportunity", title="Get opportunity", annotations=READ_ONLY)
    def get_opportunity(opportunity_id: int) -> OpportunityDetail:
        """Return one opportunity with its account, originating lead, quotes (with line items),
        activities and reminders."""
        opp = db.one("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,))
        if opp is None:
            raise ToolError(f"opportunity {opportunity_id} not found")
        quotes = db.rows("SELECT * FROM quotes WHERE opportunity_id = ? ORDER BY id", (opportunity_id,))
        for q in quotes:
            lines = db.rows(
                "SELECT id, part_number, product, quantity, list_price, unit_price, "
                "quantity * unit_price AS line_total FROM quote_details WHERE quote_id = ? ORDER BY id",
                (q["id"],),
            )
            q["lines"] = lines
            # quotes.total_cost is a stored summary nothing keeps in step (crm.md section 2.5)
            q["computed_total"] = round(sum(line["line_total"] or 0 for line in lines), 2)
        return OpportunityDetail(
            opportunity=_opportunity(opp),
            account=db.one("SELECT * FROM accounts WHERE id = ?", (opp["account_id"],)),
            lead=db.one("SELECT * FROM leads WHERE id = ?", (opp["lead_id"],)),
            quotes=quotes,
            activities=db.rows(
                "SELECT * FROM activities WHERE opportunity_id = ? ORDER BY due_date, id", (opportunity_id,)
            ),
            reminders=db.rows(
                "SELECT * FROM opportunity_reminders WHERE opportunity_id = ? ORDER BY sort_order, id",
                (opportunity_id,),
            ),
        )

    @mcp.tool(name="crm.support_renewals", title="Support renewals due", annotations=READ_ONLY)
    def support_renewals(days: int = 90) -> list[dict[str, Any]]:
        """List installed-base items whose support ends within the next `days` days (1-730, default 90),
        soonest first, with the account name."""
        days = max(1, min(days, 730))
        return db.rows(
            "SELECT i.id, i.account_id, a.name AS account, i.product_id, i.quantity, "
            "i.support_start_date, i.support_end_date, i.status "
            "FROM inventory_items i LEFT JOIN accounts a ON a.id = i.account_id "
            "WHERE i.support_end_date BETWEEN strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime') "
            "AND strftime('%Y-%m-%d %H:%M:%S', 'now', ?, 'localtime') ORDER BY i.support_end_date",
            (f"+{days} days",),
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal read-only MCP server over crm.s3db (prototype)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="database file (default: crm.s3db here)")
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
