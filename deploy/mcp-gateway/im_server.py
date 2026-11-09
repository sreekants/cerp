"""Prototype: a minimal read-only MCP server over the webos incident database (im.s3db).

Throwaway exploration code for cerp, not part of the package. Follows docs/webos/im/im.INSTRUCTIONS.md
(read-only connections when looking, bound parameters) and the code meanings in docs/webos/im/im.md
section 3. It opens the database read-only (MPC001-162), builds SQL only from fixed identifiers with
bound values (MPC001-065), and never points at the templates in data/template (it serves the copy
next to this file by default).

Run from the cerp repository root:

    uv run python workspace/prototype/im_server.py              # stdio (Claude Desktop / Claude Code)
    uv run python workspace/prototype/im_server.py --http       # http://127.0.0.1:7412/mcp
    uv run python workspace/prototype/im_server.py --db other.s3db

Add to Claude Code:

    claude mcp add im-proto -- uv run --directory <cerp> python workspace/prototype/im_server.py
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

DEFAULT_DB = Path(__file__).with_name("im.s3db")
MAX_LIMIT = 100
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Code labels from lang.en.s3db, the authority (docs/webos/lang/lang.md, docs/webos/im/im.md section 3).
STATUS = {1000: "Open", 2000: "Fixed", 3000: "Reviewed", 4000: "Tested", 5000: "Closed"}
TYPE = {
    100: "Defect",
    200: "Feature Requirement",
    250: "Feature Request",
    300: "Nice to Have",
    350: "Cosmetic",
}
SEVERITY = {100: "Error or Fault", 200: "No Work Around", 300: "Work Around"}
PRIORITY = {9: "Critical", 7: "High", 5: "Medium", 1: "Low"}
REPRODUCIBLE = {100: "Always", 200: "Sometimes", 300: "Rarely", 400: "Could Not", 500: "Not Tested"}
LINK_TYPE = {1: "Linked To", 11: "Links", 2: "Cloned By", 12: "Clones", 3: "Duplicated By", 13: "Duplicates"}
LABELS = {
    "status": STATUS,
    "type": TYPE,
    "severity": SEVERITY,
    "priority": PRIORITY,
    "reproducible": REPRODUCIBLE,
    "link_type": LINK_TYPE,
}

# Columns returned in search results; the full row comes from im.get_ticket.
SUMMARY_COLUMNS = (
    "id",
    "short_name",
    "status",
    "severity",
    "priority",
    "type",
    "released_product_id",
    "product_component_id",
    "owner",
    "entered_by",
    "creation_time",
    "due_date",
    "modification_time",
)


# ---------------------------------------------------------------- data access


class Database:
    """Read-only access to one im.s3db file. One short-lived connection per call."""

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
    """Add *_label fields to a ticket or ticket-link row."""
    for column, names in LABELS.items():
        if column in row:
            row[f"{column}_label"] = names.get(row[column], "unknown")
    return row


# ---------------------------------------------------------------- result models


class Product(BaseModel):
    id: int
    name: str
    status: int | None = None
    components: list[dict[str, Any]] = Field(default_factory=list)


class ProductList(BaseModel):
    products: list[Product]


class TicketPage(BaseModel):
    total: int = Field(description="Number of tickets matching the filters")
    offset: int
    limit: int
    tickets: list[dict[str, Any]]


class TicketDetail(BaseModel):
    ticket: dict[str, Any]
    comments: list[dict[str, Any]]
    links: list[dict[str, Any]]
    product: dict[str, Any] | None
    component: dict[str, Any] | None


class Count(BaseModel):
    value: Any
    label: str | None = None
    count: int


class TicketStats(BaseModel):
    total: int
    by_status: list[Count]
    by_severity: list[Count]
    by_priority: list[Count]
    by_product: list[Count]


# ---------------------------------------------------------------- server


def build_server(db: Database) -> MCPServer:
    mcp = MCPServer(
        name="im-prototype",
        title="webos incidents (prototype)",
        instructions=(
            "Read-only access to a webos incident-management database: tickets, products, components, "
            "comments and links. Status, type, severity and priority are numeric codes returned with a "
            "*_label (status 1000 Open .. 5000 Closed; severity 100 Error or Fault, 200 No Work Around, "
            "300 Work Around; priority 9 Critical, 7 High, 5 Medium, 1 Low). Codes without a label are "
            "labelled 'unknown' (older imports stored severity 400/500). Use im.ticket_stats "
            "for an overview, im.search_tickets to find tickets and im.get_ticket for one ticket's "
            "full record."
        ),
    )

    @mcp.tool(name="im.list_products", title="List products", annotations=READ_ONLY)
    def list_products() -> ProductList:
        """List released products and their components. Use the ids to filter im.search_tickets."""
        components = db.rows("SELECT id, name, released_product_id FROM product_components ORDER BY name")
        products = []
        for p in db.rows("SELECT id, name, status FROM released_products ORDER BY name"):
            parts = [
                {"id": c["id"], "name": c["name"]} for c in components if c["released_product_id"] == p["id"]
            ]
            products.append(Product(id=p["id"], name=p["name"], status=p["status"], components=parts))
        return ProductList(products=products)

    @mcp.tool(name="im.search_tickets", title="Search tickets", annotations=READ_ONLY)
    def search_tickets(
        *,
        text: str | None = None,
        status: int | None = None,
        severity: int | None = None,
        priority: int | None = None,
        released_product_id: int | None = None,
        owner: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> TicketPage:
        """Find tickets, newest first.

        text: words to look for in the ticket title or description (case-insensitive substring).
        status: 1000 Open, 2000 Fixed, 3000 Reviewed, 4000 Tested, 5000 Closed.
        severity: 100 Error or Fault, 200 No Work Around, 300 Work Around.
        priority: 9 Critical, 7 High, 5 Medium, 1 Low.
        released_product_id: product id from im.list_products.
        owner: owner name, matched as a case-insensitive substring.
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        where: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("status", status),
            ("severity", severity),
            ("priority", priority),
            ("released_product_id", released_product_id),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if text:
            where.append("(short_name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
            pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [pattern, pattern]
        if owner:
            where.append("owner LIKE ? ESCAPE '\\'")
            params.append("%" + owner.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        total = db.one(f"SELECT COUNT(*) AS n FROM tickets{clause}", tuple(params))  # noqa: S608
        page = db.rows(
            f"SELECT {', '.join(SUMMARY_COLUMNS)} FROM tickets{clause} "  # noqa: S608
            "ORDER BY creation_time DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return TicketPage(
            total=total["n"] if total else 0, offset=offset, limit=limit, tickets=[_label(t) for t in page]
        )

    @mcp.tool(name="im.get_ticket", title="Get ticket", annotations=READ_ONLY)
    def get_ticket(ticket_id: int) -> TicketDetail:
        """Return one ticket with its comments, links to other tickets, product and component."""
        ticket = db.one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if ticket is None:
            raise ToolError(f"ticket {ticket_id} not found")
        return TicketDetail(
            ticket=_label(ticket),
            comments=db.rows(
                "SELECT id, creation_time, owner, source, body FROM ticket_comments "
                "WHERE ticket_id = ? ORDER BY creation_time",
                (ticket_id,),
            ),
            links=[
                _label(link)
                for link in db.rows(
                    "SELECT id, link_type, linked_from, linked_to FROM ticket_links "
                    "WHERE linked_from = ? OR linked_to = ?",
                    (ticket_id, ticket_id),
                )
            ],
            product=db.one(
                "SELECT id, name, status FROM released_products WHERE id = ?",
                (ticket["released_product_id"],),
            ),
            component=db.one(
                "SELECT id, name FROM product_components WHERE id = ?", (ticket["product_component_id"],)
            ),
        )

    @mcp.tool(name="im.ticket_stats", title="Ticket statistics", annotations=READ_ONLY)
    def ticket_stats() -> TicketStats:
        """Count tickets overall and by status, severity, priority and product."""

        def by(column: str) -> list[Count]:
            # column is one of the fixed names below, never caller input
            sql = f"SELECT {column} AS value, COUNT(*) AS n FROM tickets GROUP BY {column} ORDER BY n DESC"  # noqa: S608
            rows = db.rows(sql)
            names: dict[int, str] = LABELS.get(column, {})
            return [
                Count(
                    value=r["value"],
                    label=names.get(int(r["value"])) if r["value"] is not None else None,
                    count=r["n"],
                )
                for r in rows
            ]

        product_rows = db.rows(
            "SELECT COALESCE(p.name, t.released_product_id) AS value, COUNT(*) AS n FROM tickets t "
            "LEFT JOIN released_products p ON p.id = t.released_product_id GROUP BY value ORDER BY n DESC"
        )
        total = db.one("SELECT COUNT(*) AS n FROM tickets")
        return TicketStats(
            total=total["n"] if total else 0,
            by_status=by("status"),
            by_severity=by("severity"),
            by_priority=by("priority"),
            by_product=[Count(value=r["value"], count=r["n"]) for r in product_rows],
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal read-only MCP server over im.s3db (prototype)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="database file (default: im.s3db here)")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=7412)
    args = parser.parse_args()

    server = build_server(Database(args.db))
    if args.http:
        server.run("streamable-http", host="127.0.0.1", port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
