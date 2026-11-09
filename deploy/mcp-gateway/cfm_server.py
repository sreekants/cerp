"""Prototype: a minimal read-only MCP server over the webos cashflow database (cfm.s3db).

Throwaway exploration code for cerp, not part of the package. Modelled on im_server.py and crm_server.py.
Follows docs/webos/cfm/cfm.INSTRUCTIONS.md (read-only connections when looking, bound parameters) and the
meanings in docs/webos/cfm/cfm.md. It opens the database read-only (MPC001-162), builds SQL only from fixed
identifiers with bound values (MPC001-065), and serves the copy next to this file by default.

What cfm.md establishes, and how this prototype uses it:

* PAYMENT_TYPES and PAYMENT_METHODS are curated, authoritative vocabulary (section 2.3). Their names are
  read from those tables, not hard-coded, and returned as type_label / method_label.
* Every status, bill_type, contact_type, account_type, coa_type, payment_category, category_id and
  link_type domain is unknown (section 8 rule 8). Those are returned raw, with no label.
* The unset sentinel is '' (section 4); it is returned as null.
* coas.name is empty on every row; the account name is in description (section 9.1). coas.account_class_id
  is unset, so the class is inferred from the leading digit of the code (section 2.2) and flagged inferred.
* Documents (payables, receivables, payments, receipts) store an amount that nothing keeps in step with
  their detail lines, and cash_accounts.balance likewise (section 8 rule 9): both are returned next to a
  computed figure.
* No currency base is recorded (section 4), so this server never adds amounts across currencies as if they
  were comparable; totals say so.

Run from the cerp repository root:

    uv run python workspace/prototype/cfm_server.py              # stdio (Claude Desktop / Claude Code)
    uv run python workspace/prototype/cfm_server.py --http       # http://127.0.0.1:7414/mcp
    uv run python workspace/prototype/cfm_server.py --db other.s3db

Add to Claude Code:

    claude mcp add cfm-proto -- uv run --directory <cerp> python workspace/prototype/cfm_server.py
"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, NamedTuple

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

DEFAULT_DB = Path(__file__).with_name("cfm.s3db")
MAX_LIMIT = 100
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

NOW = "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime')"


class Obligation(NamedTuple):
    """One half of the payables / receivables symmetry (cfm.md section 1)."""

    table: str
    party: str  # payee or payer
    details: str
    fk: str  # the detail table's parent key
    settled_by: str  # settlement table that references it
    settled_column: str  # column on that table holding this document's id


class Settlement(NamedTuple):
    """One half of the payments / receipts symmetry."""

    table: str
    party: str
    type_column: str  # payment_type or receipt_type
    details: str
    fk: str


OBLIGATIONS = {
    "payable": Obligation("payables", "payee", "payable_details", "payable_id", "payments", "bill_id"),
    "receivable": Obligation(
        "receivables", "payer", "receivable_details", "receivable_id", "receipts", "invoice_id"
    ),
}
SETTLEMENTS = {
    "payment": Settlement("payments", "payee", "payment_type", "payment_details", "payment_id"),
    "receipt": Settlement("receipts", "payer", "receipt_type", "receipt_details", "receipt_id"),
}
ObligationKind = Literal["payable", "receivable"]
SettlementKind = Literal["payment", "receipt"]

# Columns returned in search results; the full row comes from the cfm.get_* tools.
OBLIGATION_COLUMNS = ("id", "bill_number", "bill_type", "status", "amount", "required_date", "cancel_date",
                      "contact_id", "contact_type", "owner", "project_id", "creation_time",
                      "modification_time")  # fmt: skip
SETTLEMENT_COLUMNS = ("id", "doc_number", "bill_number", "status", "amount", "paid_on", "payment_method",
                      "currency_type", "exchange_rate", "coa_id", "invoice_id", "bill_id",
                      "cash_transaction_id", "customer_id", "vendor_id", "owner",
                      "creation_time")  # fmt: skip


# ---------------------------------------------------------------- data access


class Database:
    """Read-only access to one cfm.s3db file. One short-lived connection per call."""

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

    def count(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        found = self.one(sql, params)
        return int(found["n"]) if found else 0


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    # cfm uses '' for unset values (cfm.md section 4); webos also writes the literal 'NULL' (MPC002-047).
    return {k: (None if v in ("", "NULL") else v) for k, v in row.items()}


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


class ReferenceData(BaseModel):
    account_classes: list[dict[str, Any]]
    payment_types: list[dict[str, Any]]
    payment_methods: list[dict[str, Any]]


class Document(BaseModel):
    document: dict[str, Any]
    lines: list[dict[str, Any]] = Field(description="Detail lines with line_total = quantity * unit_cost")
    computed_total: float = Field(description="Sum of line totals; compare with the stored document amount")
    amount_matches_lines: bool
    related: list[dict[str, Any]] = Field(
        description="Settlements that reference this document, or the document a settlement settles"
    )


class Ledger(BaseModel):
    cash_account: dict[str, Any]
    stored_balance: float | None
    computed_balance: float = Field(description="Sum of credit_amount - debit_amount over all lines")
    balance_matches: bool
    total: int
    transactions: list[dict[str, Any]]


class TableTotal(BaseModel):
    table: str
    rows: int
    amount: float | None = Field(
        description="Sum of amount; no currency base is recorded, so indicative only"
    )


class Overview(BaseModel):
    tables: list[TableTotal]
    cash_accounts: int
    cash_transactions: int
    chart_of_accounts: int
    note: str


# ---------------------------------------------------------------- server


def build_server(db: Database) -> MCPServer:  # noqa: PLR0915 - one registration per tool
    mcp = MCPServer(
        name="cfm-prototype",
        title="webos cashflow (prototype)",
        instructions=(
            "Read-only access to a webos cashflow database: chart of accounts, payables and "
            "receivables (money owed out and in), payments and receipts (money that moved), and the "
            "bank ledger. Payment types (1000 Payment .. 7000 Other Receipt) and methods (1 Cash .. 13 "
            "Bank Drafts) come with type_label / method_label. status, bill_type, contact_type, "
            "account_type, coa_type, payment_category and link_type have no known meaning and are "
            "returned raw; do not guess what they stand for. No currency base is recorded and currency "
            "is stored inconsistently, so never add amounts across currencies. Documents' stored amount "
            "and the bank balance are not maintained from their lines: tools return a computed figure "
            "beside them. The transactional tables may be empty. Use cfm.overview first, "
            "cfm.reference_data for the vocabularies, cfm.search_* to find records and cfm.get_* for one "
            "record in full."
        ),
    )

    def page(table: str, select: str, f: Filter, order: str, *, limit: int, offset: int) -> Page:
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        # table, select and order are fixed text from this module, never caller input
        total = db.count(f"SELECT COUNT(*) AS n FROM {table}{f.clause}", tuple(f.params))  # noqa: S608
        rows = db.rows(
            f"SELECT {select} FROM {table}{f.clause} ORDER BY {order} LIMIT ? OFFSET ?",  # noqa: S608
            (*f.params, limit, offset),
        )
        return Page(total=total, offset=offset, limit=limit, rows=rows)

    def lines(table: str, fk: str, parent_id: int) -> list[dict[str, Any]]:
        # table and fk come from OBLIGATIONS / SETTLEMENTS, never caller input
        return db.rows(
            "SELECT id, product_id, description, quantity, unit_cost, currency, exchange_rate, "  # noqa: S608
            f"quantity * unit_cost AS line_total FROM {table} WHERE {fk} = ? ORDER BY id",
            (parent_id,),
        )

    def document(
        *, table: str, fk: str, details: str, doc_id: int, noun: str, related: list[dict[str, Any]]
    ) -> Document:
        # table, fk and details come from OBLIGATIONS / SETTLEMENTS, never caller input
        row = db.one(f"SELECT * FROM {table} WHERE id = ?", (doc_id,))  # noqa: S608
        if row is None:
            raise ToolError(f"{noun} {doc_id} not found")
        detail_lines = lines(details, fk, doc_id)
        computed = round(sum(line["line_total"] or 0 for line in detail_lines), 2)
        stored = row.get("amount")
        return Document(
            document=row,
            lines=detail_lines,
            computed_total=computed,
            amount_matches_lines=stored is not None and round(stored, 2) == computed,
            related=related,
        )

    @mcp.tool(name="cfm.overview", title="Cashflow overview", annotations=READ_ONLY)
    def overview() -> Overview:
        """Row counts for each ledger table, with the sum of stored amounts. Start here: the
        transactional tables may be empty."""
        tables = []
        for name in ("payables", "receivables", "payments", "receipts"):
            found = db.one(f"SELECT COUNT(*) AS n, SUM(amount) AS s FROM {name}")  # noqa: S608
            tables.append(
                TableTotal(table=name, rows=found["n"] if found else 0, amount=found["s"] if found else None)
            )
        return Overview(
            tables=tables,
            cash_accounts=db.count("SELECT COUNT(*) AS n FROM cash_accounts"),
            cash_transactions=db.count("SELECT COUNT(*) AS n FROM cash_transactions"),
            chart_of_accounts=db.count("SELECT COUNT(*) AS n FROM coas"),
            note="Amounts are summed as stored; no currency base is recorded, so treat them as indicative.",
        )

    @mcp.tool(name="cfm.reference_data", title="Reference data", annotations=READ_ONLY)
    def reference_data() -> ReferenceData:
        """The curated vocabularies: account classes, payment types (payments take 1000-3000, receipts
        4000-7000 by their descriptions) and payment methods."""
        return ReferenceData(
            account_classes=db.rows(
                "SELECT id, name, parent_id, category_code, description FROM ACCOUNT_CLASS ORDER BY id"
            ),
            payment_types=db.rows("SELECT id, name, description FROM PAYMENT_TYPES ORDER BY id"),
            payment_methods=db.rows("SELECT id, name, description FROM PAYMENT_METHODS ORDER BY id"),
        )

    @mcp.tool(name="cfm.list_accounts", title="Chart of accounts", annotations=READ_ONLY)
    def list_accounts(
        *, text: str | None = None, account_class: int | None = None, limit: int = 50, offset: int = 0
    ) -> Page:
        """List chart-of-accounts entries by code. The name is in account_name (the stored description).

        text: words in the account name or code (case-insensitive substring).
        account_class: top-level class id 1-7 (1 Assets, 2 Liabilities, 3 Equity, 4 Revenue, 5 Cost of
            Goods, 6 Expenses, 7 Miscellaneous Expenses). Inferred from the leading digit of the code,
            because the stored class link is empty; account 90000 has no class.
        limit: page size, 1-100 (default 50). offset: rows to skip for the next page.
        """
        f = Filter()
        f.contains(("c.description", "c.code"), text)
        f.equals("CAST(substr(c.code, 1, 1) AS INTEGER)", account_class)
        return page(
            "coas c LEFT JOIN ACCOUNT_CLASS a ON a.id = CAST(substr(c.code, 1, 1) AS INTEGER)",
            "c.id, c.code, c.description AS account_name, a.id AS class_id, a.name AS class_name, "
            "1 AS class_inferred",
            f,
            "c.code",
            limit=limit,
            offset=offset,
        )

    @mcp.tool(name="cfm.search_obligations", title="Search payables / receivables", annotations=READ_ONLY)
    def search_obligations(
        *,
        kind: ObligationKind,
        text: str | None = None,
        party: str | None = None,
        status: int | None = None,
        due_before: str | None = None,
        min_amount: float | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page:
        """Find bills owed to vendors (kind payable) or invoices owed by customers (kind receivable),
        soonest due first.

        text: words in the bill number or description (case-insensitive substring).
        party: payee (payable) or payer (receivable) name, matched as a case-insensitive substring.
        status: raw status code (its meaning is not defined for this database).
        due_before: required date before this, as YYYY-MM-DD.
        min_amount: only documents of at least this amount (no currency is recorded).
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        o = OBLIGATIONS[kind]
        f = Filter()
        f.contains(("bill_number", "description"), text)
        f.contains((o.party,), party)
        f.equals("status", status)
        if due_before:
            f.raw("required_date < ?", due_before)
        if min_amount is not None:
            f.raw("amount >= ?", min_amount)
        return page(
            o.table,
            f"{o.party} AS party, " + ", ".join(OBLIGATION_COLUMNS),
            f,
            "required_date, id",
            limit=limit,
            offset=offset,
        )

    @mcp.tool(name="cfm.get_obligation", title="Get payable / receivable", annotations=READ_ONLY)
    def get_obligation(kind: ObligationKind, obligation_id: int) -> Document:
        """Return one payable or receivable with its detail lines, a recomputed total, and the payments
        (for a payable) or receipts (for a receivable) that settle it."""
        o = OBLIGATIONS[kind]
        # settled_by and settled_column come from OBLIGATIONS, never caller input
        settlements = db.rows(
            f"SELECT id, doc_number, amount, paid_on, payment_method, status FROM {o.settled_by} "  # noqa: S608
            f"WHERE {o.settled_column} = ? ORDER BY paid_on, id",
            (obligation_id,),
        )
        noun = kind
        return document(
            table=o.table, fk=o.fk, details=o.details, doc_id=obligation_id, noun=noun, related=settlements
        )

    @mcp.tool(name="cfm.aged_obligations", title="Overdue payables / receivables", annotations=READ_ONLY)
    def aged_obligations(*, kind: ObligationKind, open_status: int, limit: int = 50) -> list[dict[str, Any]]:
        """List documents past their required date, most overdue first.

        open_status: the raw status code that means 'still open'. Its meaning is not defined for this
            database, so it must be supplied; ask the user rather than guessing.
        limit: 1-100 (default 50).
        """
        o = OBLIGATIONS[kind]
        limit = max(1, min(limit, MAX_LIMIT))
        return db.rows(
            f"SELECT id, {o.party} AS party, bill_number, amount, required_date, "  # noqa: S608
            "CAST(julianday('now', 'localtime') - julianday(required_date) AS INTEGER) AS days_overdue "
            f"FROM {o.table} WHERE status = ? AND required_date < {NOW} "
            "ORDER BY days_overdue DESC, id LIMIT ?",
            (open_status, limit),
        )

    @mcp.tool(name="cfm.search_settlements", title="Search payments / receipts", annotations=READ_ONLY)
    def search_settlements(
        *,
        kind: SettlementKind,
        text: str | None = None,
        party: str | None = None,
        type_id: int | None = None,
        payment_method: int | None = None,
        coa_id: int | None = None,
        paid_from: str | None = None,
        paid_to: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Page:
        """Find money that moved out (kind payment) or in (kind receipt), newest first.

        text: words in the document number, bill number or description.
        party: payee (payment) or payer (receipt) name, matched as a case-insensitive substring.
        type_id: PAYMENT_TYPES id; payments use 1000-3000, receipts 4000-7000.
        payment_method: PAYMENT_METHODS id, 1-13.
        coa_id: the chart-of-accounts row it posts to (id from cfm.list_accounts).
        paid_from / paid_to: value-date range, as YYYY-MM-DD (paid_to is inclusive of that day).
        limit: page size, 1-100 (default 25). offset: rows to skip for the next page.
        """
        s = SETTLEMENTS[kind]
        f = Filter()
        f.contains(("t.doc_number", "t.bill_number", "t.description"), text)
        f.contains((f"t.{s.party}",), party)
        f.equals(f"t.{s.type_column}", type_id)
        f.equals("t.payment_method", payment_method)
        f.equals("t.coa_id", coa_id)
        if paid_from:
            f.raw("t.paid_on >= ?", paid_from)
        if paid_to:
            f.raw("t.paid_on < date(?, '+1 day')", paid_to)
        select = ", ".join(f"t.{c}" for c in SETTLEMENT_COLUMNS)
        return page(
            f"{s.table} t LEFT JOIN PAYMENT_TYPES pt ON pt.id = t.{s.type_column} "
            "LEFT JOIN PAYMENT_METHODS pm ON pm.id = t.payment_method",
            f"t.{s.party} AS party, t.{s.type_column} AS type_id, pt.name AS type_label, "
            f"pm.name AS method_label, {select}",
            f,
            "t.paid_on DESC, t.id DESC",
            limit=limit,
            offset=offset,
        )

    @mcp.tool(name="cfm.get_settlement", title="Get payment / receipt", annotations=READ_ONLY)
    def get_settlement(kind: SettlementKind, settlement_id: int) -> Document:
        """Return one payment or receipt with its detail lines, a recomputed total, type and method
        labels, and the bank line and chart-of-accounts row it references."""
        s = SETTLEMENTS[kind]
        # table and type_column come from SETTLEMENTS, never caller input
        head = db.one(
            f"SELECT t.coa_id, t.cash_transaction_id, pt.name AS type_label, pm.name AS method_label "  # noqa: S608
            f"FROM {s.table} t LEFT JOIN PAYMENT_TYPES pt ON pt.id = t.{s.type_column} "
            "LEFT JOIN PAYMENT_METHODS pm ON pm.id = t.payment_method WHERE t.id = ?",
            (settlement_id,),
        )
        related: list[dict[str, Any]] = []
        if head:
            related.append({"type_label": head["type_label"], "method_label": head["method_label"]})
            for sql, key in (
                ("SELECT id, code, description AS account_name FROM coas WHERE id = ?", "coa_id"),
                ("SELECT * FROM cash_transactions WHERE id = ?", "cash_transaction_id"),
            ):
                if head[key] is not None:
                    found = db.one(sql, (head[key],))
                    if found:
                        related.append({key: found})
        return document(
            table=s.table, fk=s.fk, details=s.details, doc_id=settlement_id, noun=kind, related=related
        )

    @mcp.tool(name="cfm.list_cash_accounts", title="List bank accounts", annotations=READ_ONLY)
    def list_cash_accounts() -> list[dict[str, Any]]:
        """List bank accounts with their stored balance and the balance recomputed from the ledger."""
        return db.rows(
            "SELECT a.id, a.account_number, a.bank_name, a.location, a.account_type, a.balance AS "
            "stored_balance, COUNT(t.id) AS transactions, "
            "ROUND(IFNULL(SUM(IFNULL(t.credit_amount, 0) - IFNULL(t.debit_amount, 0)), 0), 2) AS "
            "computed_balance FROM cash_accounts a LEFT JOIN cash_transactions t ON t.cash_account_id = a.id "
            "GROUP BY a.id ORDER BY a.id"
        )

    @mcp.tool(name="cfm.cash_ledger", title="Bank ledger", annotations=READ_ONLY)
    def cash_ledger(*, cash_account_id: int, limit: int = 50, offset: int = 0) -> Ledger:
        """Return one bank account's statement lines, oldest first, with a running balance
        (credit minus debit; the two are separate columns and never signed).

        limit: page size, 1-100 (default 50). offset: lines to skip. The running balance always starts
        from the first line, not from the page start.
        """
        account = db.one("SELECT * FROM cash_accounts WHERE id = ?", (cash_account_id,))
        if account is None:
            raise ToolError(f"cash account {cash_account_id} not found")
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)
        total = db.one(
            "SELECT COUNT(*) AS n, "
            "ROUND(IFNULL(SUM(IFNULL(credit_amount, 0) - IFNULL(debit_amount, 0)), 0), 2) AS s "
            "FROM cash_transactions WHERE cash_account_id = ?",
            (cash_account_id,),
        )
        computed = float(total["s"]) if total else 0.0
        stored = account.get("balance")
        transactions = db.rows(
            "SELECT * FROM (SELECT id, creation_time, description, payee, cheque_number, bill_number, "
            "debit_amount, credit_amount, due_date, currency_type, payment_category, "
            "ROUND(SUM(IFNULL(credit_amount, 0) - IFNULL(debit_amount, 0)) "
            "OVER (ORDER BY creation_time, id), 2) AS running_balance "
            "FROM cash_transactions WHERE cash_account_id = ?) ORDER BY creation_time, id LIMIT ? OFFSET ?",
            (cash_account_id, limit, offset),
        )
        return Ledger(
            cash_account=account,
            stored_balance=stored,
            computed_balance=computed,
            balance_matches=stored is not None and round(stored, 2) == computed,
            total=total["n"] if total else 0,
            transactions=transactions,
        )

    @mcp.tool(name="cfm.cash_positions", title="Latest cash positions", annotations=READ_ONLY)
    def cash_positions(coa_id: int | None = None) -> list[dict[str, Any]]:
        """Latest balance snapshot per chart-of-accounts row, or the full snapshot history of one
        account when coa_id is given (newest first)."""
        if coa_id is not None:
            return db.rows(
                "SELECT p.id, p.coa_id, c.code, c.description AS account_name, p.amount, p.snapshot_time "
                "FROM cash_positions p LEFT JOIN coas c ON c.id = p.coa_id WHERE p.coa_id = ? "
                "ORDER BY p.snapshot_time DESC, p.id DESC",
                (coa_id,),
            )
        return db.rows(
            "SELECT p.id, p.coa_id, c.code, c.description AS account_name, p.amount, p.snapshot_time "
            "FROM cash_positions p LEFT JOIN coas c ON c.id = p.coa_id "
            "WHERE p.snapshot_time = "
            "(SELECT MAX(snapshot_time) FROM cash_positions x WHERE x.coa_id = p.coa_id) "
            "ORDER BY c.code"
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal read-only MCP server over cfm.s3db (prototype)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="database file (default: cfm.s3db here)")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=7414)
    args = parser.parse_args()

    server = build_server(Database(args.db))
    if args.http:
        server.run("streamable-http", host="127.0.0.1", port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
