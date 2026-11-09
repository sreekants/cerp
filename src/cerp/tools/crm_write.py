"""Contact, customer and vendor write tools: ``crm.*`` (MPC001-254, MPC001-255, MPC001-259, decision 0004).

Procedures: ``docs/webos/crm/crm.INSTRUCTIONS.md`` §5, §6 and §6.1-§6.4 (MPC001-250 requirement 8).

* Rights through the one authorization function (MPC001-061): ``Account``, ``Contact``, ``Vendor`` and
  ``ProjectContact`` with ``Add`` / ``Edit`` / ``Delete``.
* Creating runs the duplicate check of MPC001-256 first; a likely duplicate (score >= 0.7) refuses the
  create and returns the candidates unless ``confirm`` is true (MPC001-254 requirement 1).
* "Delete" sets ``status = -1`` on accounts, contacts and vendors (plain-PK tables whose ids would be
  reused, ``crm.md`` §8 rule 3). Unlinking a contact from a project deletes its ``project_contacts`` row:
  the one ``DELETE`` in cerp, allowed by decision 0004 because that table has no status and never reuses ids.
* ``owner_id`` / ``owner`` come from the caller (MPC001-250 requirement 3). Personal contact fields are
  never written or returned (MPC001-255 requirement 2).
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field, StringConstraints

from cerp.auth.authorize import Caller
from cerp.data.records import clean_row
from cerp.protocol.args import HandleText
from cerp.protocol.errors import NotFound, ValidationError
from cerp.tools.context import (
    IDEMPOTENT_WRITE,
    WRITE,
    Guard,
    Reads,
    Registrar,
    ToolContext,
    handle,
    record_id,
    stamp,
)
from cerp.tools.crm import (
    _FIND_ACCOUNT,
    ACCOUNT,
    CONTACT,
    Candidate,
    company_candidates,
    contact_candidates,
)
from cerp.tools.labels import ACTIVE, BUSINESS_TYPE, with_labels
from cerp.tools.pm import PROJECT

VENDOR: Final = Guard("vendors", model_key="objects/inventories/vendor")
PROJECT_CONTACT: Final = Guard("project contacts", model_key="objects/sales/project_contact")
GUARDS: Final = (ACCOUNT, CONTACT, VENDOR, PROJECT_CONTACT, PROJECT)

ACTIVE_CODE: Final = 1000
DELETED: Final = -1
LIKELY: Final = 0.7  # a candidate at or above this score blocks a create without confirm
OWNER_MAX: Final = 80


Name = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
FirstName = Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
Name64 = Annotated[str, StringConstraints(max_length=64, strip_whitespace=True)]
Title10 = Annotated[str, StringConstraints(max_length=10, strip_whitespace=True)]
Phone = Annotated[str, StringConstraints(max_length=64, strip_whitespace=True)]
Email = Annotated[
    str, StringConstraints(max_length=80, strip_whitespace=True, pattern=r"^$|^[^@\s]+@[^@\s]+$")
]
Address = Annotated[str, StringConstraints(max_length=256)]
LongAddress = Annotated[str, StringConstraints(max_length=1024)]
Web = Annotated[str, StringConstraints(max_length=1024, strip_whitespace=True)]
Notes = Annotated[str, StringConstraints(max_length=65536)]
Comments = Annotated[str, StringConstraints(max_length=4096)]
Role = Annotated[str, StringConstraints(max_length=128, strip_whitespace=True)]
Status = Literal[1000, 5000]
BusinessType = Literal[0, 1, 2, 3, 4, 5, 6]

_COMPANY_COLS: Final = (
    "id", "owner_id", "owner", "name", "description", "creation_date", "modification_date", "contact_id",
    "address", "website", "type", "status", "shipping_address", "billing_address", "comments", "phone",
)  # fmt: skip
READS: Final[Reads] = {
    ("crm", "accounts"): (*_COMPANY_COLS, "sales_team_id"),
    ("crm", "vendors"): (*_COMPANY_COLS, "purchasing_team_id"),
    ("crm", "contacts"): (
        "id", "first_name", "last_name", "title", "company", "description", "email", "mobile",
        "business_phone", "business_address", "webpage", "location", "account_id", "vendor_id", "status",
    ),
    ("crm", "project_contacts"): ("id", "project_id", "contact_id", "role"),
    ("pm", "projects"): ("id", "name"),
}  # fmt: skip

# ---------------------------------------------------------------- SQL (constant; values bound)

_ACCOUNT_ROW = "SELECT id, status, contact_id FROM accounts WHERE id = ?"
_VENDOR_ROW = "SELECT id, status, contact_id FROM vendors WHERE id = ?"
_CONTACT_ROW = "SELECT id, status, account_id, vendor_id FROM contacts WHERE id = ?"
_ACCOUNT_OUT = """
SELECT id, name, description, type, status, website, phone, address, billing_address, shipping_address,
       comments, contact_id, owner, creation_date, modification_date
FROM accounts WHERE id = ?
"""
_VENDOR_OUT = """
SELECT id, name, description, status, website, phone, address, billing_address, shipping_address,
       comments, contact_id, owner, creation_date, modification_date
FROM vendors WHERE id = ?
"""
_CONTACT_OUT = """
SELECT id, first_name, last_name, title, company, description, email, mobile, business_phone,
       business_address, webpage, location, account_id, vendor_id, status
FROM contacts WHERE id = ?
"""
_FIND_VENDOR = """
SELECT id, name, website, phone, owner, status FROM vendors
WHERE IFNULL(status, 0) <> -1 AND (
     (:word IS NOT NULL AND lower(name) LIKE :word ESCAPE '\\')
  OR (:domain IS NOT NULL AND lower(website) LIKE :domain ESCAPE '\\')
  OR (:tail IS NOT NULL AND replace(replace(replace(replace(replace(IFNULL(phone, ''), ' ', ''), '-', ''),
        '(', ''), ')', ''), '.', '') LIKE :tail))
LIMIT 500
"""
_INSERT_ACCOUNT = """
INSERT INTO accounts (
  id, owner_id, owner, name, description, creation_date, modification_date, contact_id, type, status, rating,
  sales_team_id, gl_account, credit_limit, address, shipping_address, billing_address, website, phone,
  comments
)
SELECT IFNULL((SELECT MAX(id) FROM accounts), 1000) + 1, :owner_id, :owner, :name, :description, :now, :now,
  0, :type, 1000, 0, 0, 0, 0.0, :address, :shipping_address, :billing_address, :website, :phone, :comments
"""
_INSERT_VENDOR = """
INSERT INTO vendors (
  id, owner_id, owner, name, description, creation_date, modification_date, contact_id, type, status, rating,
  purchasing_team_id, gl_account, credit_limit, address, shipping_address, billing_address, website, phone,
  comments
)
SELECT IFNULL((SELECT MAX(id) FROM vendors), 1000) + 1, :owner_id, :owner, :name, :description, :now, :now,
  0, 0, 1000, 0, 0, 0, 0.0, :address, :shipping_address, :billing_address, :website, :phone, :comments
"""
_UPDATE_ACCOUNT = """
UPDATE accounts SET
  name = COALESCE(:name, name), description = COALESCE(:description, description),
  type = COALESCE(:type, type), status = COALESCE(:status, status),
  website = COALESCE(:website, website), phone = COALESCE(:phone, phone),
  address = COALESCE(:address, address), billing_address = COALESCE(:billing_address, billing_address),
  shipping_address = COALESCE(:shipping_address, shipping_address), comments = COALESCE(:comments, comments),
  modification_date = :now
WHERE id = :id AND IFNULL(status, 0) <> -1
"""
_UPDATE_VENDOR = """
UPDATE vendors SET
  name = COALESCE(:name, name), description = COALESCE(:description, description),
  status = COALESCE(:status, status),
  website = COALESCE(:website, website), phone = COALESCE(:phone, phone),
  address = COALESCE(:address, address), billing_address = COALESCE(:billing_address, billing_address),
  shipping_address = COALESCE(:shipping_address, shipping_address), comments = COALESCE(:comments, comments),
  modification_date = :now
WHERE id = :id AND IFNULL(status, 0) <> -1
"""
_DELETE_ACCOUNT = (
    "UPDATE accounts SET status = -1, modification_date = ? WHERE id = ? AND IFNULL(status, 0) <> -1"
)
_DELETE_VENDOR = (
    "UPDATE vendors SET status = -1, modification_date = ? WHERE id = ? AND IFNULL(status, 0) <> -1"
)
_INSERT_CONTACT = """
INSERT INTO contacts (
  id, first_name, last_name, title, company, description, email, mobile, business_phone, business_address,
  webpage, location, account_id, vendor_id, status
)
SELECT IFNULL((SELECT MAX(id) FROM contacts), 1000) + 1, :first_name, :last_name, :title, :company,
  :description, :email, :mobile, :business_phone, :business_address, :webpage, :location, 0, 0, 1000
"""
_UPDATE_CONTACT = """
UPDATE contacts SET
  first_name = COALESCE(:first_name, first_name), last_name = COALESCE(:last_name, last_name),
  title = COALESCE(:title, title), company = COALESCE(:company, company),
  description = COALESCE(:description, description), email = COALESCE(:email, email),
  mobile = COALESCE(:mobile, mobile), business_phone = COALESCE(:business_phone, business_phone),
  business_address = COALESCE(:business_address, business_address), webpage = COALESCE(:webpage, webpage),
  location = COALESCE(:location, location), status = COALESCE(:status, status)
WHERE id = :id AND IFNULL(status, 0) <> -1
"""
_DELETE_CONTACT = "UPDATE contacts SET status = -1 WHERE id = ? AND IFNULL(status, 0) <> -1"
_CLEAR_PRIMARY_ACCOUNTS = "UPDATE accounts SET contact_id = 0, modification_date = ? WHERE contact_id = ?"
_CLEAR_PRIMARY_VENDORS = "UPDATE vendors SET contact_id = 0, modification_date = ? WHERE contact_id = ?"
_SET_CONTACT_ACCOUNT = "UPDATE contacts SET account_id = ? WHERE id = ?"
_SET_CONTACT_VENDOR = "UPDATE contacts SET vendor_id = ? WHERE id = ?"
_SET_PRIMARY_ACCOUNT = "UPDATE accounts SET contact_id = ?, modification_date = ? WHERE id = ?"
_SET_PRIMARY_VENDOR = "UPDATE vendors SET contact_id = ?, modification_date = ? WHERE id = ?"
_UNSET_PRIMARY_ACCOUNT = (
    "UPDATE accounts SET contact_id = 0, modification_date = ? WHERE id = ? AND contact_id = ?"
)
_UNSET_PRIMARY_VENDOR = (
    "UPDATE vendors SET contact_id = 0, modification_date = ? WHERE id = ? AND contact_id = ?"
)
_PROJECT_EXISTS = "SELECT id, name FROM projects WHERE id = ?"
_LINK_ROW = "SELECT id FROM project_contacts WHERE project_id = ? AND contact_id = ?"
_LINK_INSERT = "INSERT INTO project_contacts (project_id, contact_id, role) VALUES (?, ?, ?)"
_LINK_ROLE = "UPDATE project_contacts SET role = ? WHERE project_id = ? AND contact_id = ?"
# The one DELETE in cerp (decision 0004): project_contacts has no status and never reuses ids.
_LINK_DELETE = "DELETE FROM project_contacts WHERE project_id = ? AND contact_id = ?"
_LINKS_OF = "SELECT project_id, role FROM project_contacts WHERE contact_id = ? ORDER BY project_id LIMIT 200"


class Written(BaseModel):
    record: dict[str, Any] | None = Field(
        description="The record as stored after the change; null if refused"
    )
    created: bool = False
    duplicates: list[Candidate] = Field(
        default_factory=list,
        description="Likely duplicates that stopped the create; call again with confirm=true to create",
    )


# ---------------------------------------------------------------- helpers


def _one(con: sqlite3.Connection, statement: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = con.execute(statement, params).fetchone()
    return clean_row(dict(row)) if row is not None else None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _live(con: sqlite3.Connection, statement: str, rid: int, what: str) -> dict[str, Any]:
    row = _one(con, statement, (rid,))
    if row is None or _as_int(row["status"]) == DELETED:
        raise NotFound(f"{what} not found")
    return row


def _owner(caller: Caller) -> str:
    return (caller.display_name or caller.user_name)[:OWNER_MAX]


def _company(row: dict[str, Any], model: str) -> dict[str, Any]:
    row["handle"] = handle("crm", model, row["id"])
    row["primary_contact"] = handle("crm", "contact", row.pop("contact_id", None))
    if model == "account":
        with_labels(row, type=BUSINESS_TYPE)
    return with_labels(row, status=ACTIVE)


def _vendor_candidate(row: dict[str, Any]) -> dict[str, Any]:
    row["handle"] = handle("crm", "vendor", row["id"])
    return with_labels(row, status=ACTIVE)


def _contact_out(con: sqlite3.Connection, cid: int) -> dict[str, Any]:
    row = _one(con, _CONTACT_OUT, (cid,)) or {"id": cid}
    row["handle"] = handle("crm", "contact", cid)
    row["account"] = handle("crm", "account", row.pop("account_id", None))
    row["vendor"] = handle("crm", "vendor", row.pop("vendor_id", None))
    row["projects"] = [
        {"project": handle("pm", "project", r["project_id"]), "role": r["role"]}
        for r in con.execute(_LINKS_OF, (cid,)).fetchall()
    ]
    return with_labels(row, status=ACTIVE)


def _likely(found: list[Candidate]) -> list[Candidate]:
    return [c for c in found if c.score >= LIKELY]


def _company_args(**values: Any) -> dict[str, Any]:
    return {k: values.get(k) for k in (
        "name", "description", "type", "status", "website", "phone", "address", "billing_address",
        "shipping_address", "comments",
    )}  # fmt: skip


# ---------------------------------------------------------------- tools


def register(reg: Registrar, ctx: ToolContext) -> None:  # noqa: PLR0915 - one closure per tool
    def now() -> str:
        return stamp(ctx.now())

    # ------------------------------------------------ accounts (customers)

    def create_account(
        name: Name,
        *,
        description: Notes | None = None,
        type: BusinessType | None = None,
        website: Web | None = None,
        phone: Phone | None = None,
        address: Address | None = None,
        billing_address: LongAddress | None = None,
        shipping_address: LongAddress | None = None,
        comments: Comments | None = None,
        confirm: bool = False,
    ) -> Written:
        """Create a customer account owned by you. Type: 1 Public Company, 2 Private Company, 3 Individual,
        4 Govt., 5 Small Business, 6 Non-Profit. If a likely duplicate exists (same name, website or
        phone) nothing is created and the candidates are returned; call again with confirm=true to create."""
        caller = ctx.caller()
        ctx.require_to(caller, ACCOUNT, "Add", "create accounts")
        if not confirm and (dups := _likely(company_candidates(ctx, _FIND_ACCOUNT, name, website, phone))):
            return Written(record=None, duplicates=dups)
        with ctx.writer("crm") as con:
            cur = con.execute(
                _INSERT_ACCOUNT,
                {
                    **_company_args(
                        name=name, description=description, website=website, phone=phone, address=address,
                        billing_address=billing_address, shipping_address=shipping_address, comments=comments,
                    ),
                    "type": type or 0,
                    "owner_id": caller.user_id,
                    "owner": _owner(caller),
                    "now": now(),
                },
            )  # fmt: skip
            rid = int(cur.lastrowid or 0)
            return Written(
                record=_company(_one(con, _ACCOUNT_OUT, (rid,)) or {"id": rid}, "account"), created=True
            )

    reg(create_account, "crm.create_account", "Create a customer account", WRITE)

    def update_account(
        account: HandleText,
        *,
        name: Name | None = None,
        description: Notes | None = None,
        type: BusinessType | None = None,
        status: Status | None = None,
        website: Web | None = None,
        phone: Phone | None = None,
        address: Address | None = None,
        billing_address: LongAddress | None = None,
        shipping_address: LongAddress | None = None,
        comments: Comments | None = None,
    ) -> Written:
        """Change a customer account (handle crm:account:<id>). Fields you leave out are unchanged; an
        empty string clears a text field. Status: 1000 Active, 5000 Inactive."""
        caller = ctx.caller()
        ctx.require_to(caller, ACCOUNT, "Edit", "change accounts")
        aid = record_id(account, "crm", "account", "account")
        with ctx.writer("crm") as con:
            _live(con, _ACCOUNT_ROW, aid, f"account {account}")
            con.execute(
                _UPDATE_ACCOUNT,
                {
                    **_company_args(
                        name=name, description=description, type=type, status=status, website=website,
                        phone=phone, address=address, billing_address=billing_address,
                        shipping_address=shipping_address, comments=comments,
                    ),
                    "id": aid,
                    "now": now(),
                },
            )  # fmt: skip
            return Written(record=_company(_one(con, _ACCOUNT_OUT, (aid,)) or {"id": aid}, "account"))

    reg(update_account, "crm.update_account", "Change a customer account", IDEMPOTENT_WRITE)

    def delete_account(account: HandleText) -> Written:
        """Delete a customer account (handle crm:account:<id>): it is marked deleted (status -1) and hidden;
        nothing is erased. Its contacts stay linked to it."""
        caller = ctx.caller()
        ctx.require_to(caller, ACCOUNT, "Delete", "delete accounts")
        aid = record_id(account, "crm", "account", "account")
        with ctx.writer("crm") as con:
            _live(con, _ACCOUNT_ROW, aid, f"account {account}")
            con.execute(_DELETE_ACCOUNT, (now(), aid))
            return Written(record=_company(_one(con, _ACCOUNT_OUT, (aid,)) or {"id": aid}, "account"))

    reg(delete_account, "crm.delete_account", "Delete a customer account", WRITE)

    # ------------------------------------------------ vendors

    def create_vendor(
        name: Name,
        *,
        description: Notes | None = None,
        website: Web | None = None,
        phone: Phone | None = None,
        address: Address | None = None,
        billing_address: LongAddress | None = None,
        shipping_address: LongAddress | None = None,
        comments: Comments | None = None,
        confirm: bool = False,
    ) -> Written:
        """Create a vendor (a supplier) owned by you. If a likely duplicate exists (same name, website or
        phone) nothing is created and the candidates are returned; call again with confirm=true to create."""
        caller = ctx.caller()
        ctx.require_to(caller, VENDOR, "Add", "create vendors")
        found = company_candidates(ctx, _FIND_VENDOR, name, website, phone, wrap=_vendor_candidate)
        if not confirm and (dups := _likely(found)):
            return Written(record=None, duplicates=dups)
        with ctx.writer("crm") as con:
            cur = con.execute(
                _INSERT_VENDOR,
                {
                    **_company_args(
                        name=name, description=description, website=website, phone=phone, address=address,
                        billing_address=billing_address, shipping_address=shipping_address, comments=comments,
                    ),
                    "owner_id": caller.user_id,
                    "owner": _owner(caller),
                    "now": now(),
                },
            )  # fmt: skip
            rid = int(cur.lastrowid or 0)
            return Written(
                record=_company(_one(con, _VENDOR_OUT, (rid,)) or {"id": rid}, "vendor"), created=True
            )

    reg(create_vendor, "crm.create_vendor", "Create a vendor", WRITE)

    def update_vendor(
        vendor: HandleText,
        *,
        name: Name | None = None,
        description: Notes | None = None,
        status: Status | None = None,
        website: Web | None = None,
        phone: Phone | None = None,
        address: Address | None = None,
        billing_address: LongAddress | None = None,
        shipping_address: LongAddress | None = None,
        comments: Comments | None = None,
    ) -> Written:
        """Change a vendor (handle crm:vendor:<id>). Fields you leave out are unchanged; an empty string
        clears a text field. Status: 1000 Active, 5000 Inactive."""
        caller = ctx.caller()
        ctx.require_to(caller, VENDOR, "Edit", "change vendors")
        vid = record_id(vendor, "crm", "vendor", "vendor")
        with ctx.writer("crm") as con:
            _live(con, _VENDOR_ROW, vid, f"vendor {vendor}")
            con.execute(
                _UPDATE_VENDOR,
                {
                    **_company_args(
                        name=name, description=description, status=status, website=website, phone=phone,
                        address=address, billing_address=billing_address, shipping_address=shipping_address,
                        comments=comments,
                    ),
                    "id": vid,
                    "now": now(),
                },
            )  # fmt: skip
            return Written(record=_company(_one(con, _VENDOR_OUT, (vid,)) or {"id": vid}, "vendor"))

    reg(update_vendor, "crm.update_vendor", "Change a vendor", IDEMPOTENT_WRITE)

    def delete_vendor(vendor: HandleText) -> Written:
        """Delete a vendor (handle crm:vendor:<id>): it is marked deleted (status -1) and hidden; nothing is
        erased. Its contacts stay linked to it."""
        caller = ctx.caller()
        ctx.require_to(caller, VENDOR, "Delete", "delete vendors")
        vid = record_id(vendor, "crm", "vendor", "vendor")
        with ctx.writer("crm") as con:
            _live(con, _VENDOR_ROW, vid, f"vendor {vendor}")
            con.execute(_DELETE_VENDOR, (now(), vid))
            return Written(record=_company(_one(con, _VENDOR_OUT, (vid,)) or {"id": vid}, "vendor"))

    reg(delete_vendor, "crm.delete_vendor", "Delete a vendor", WRITE)

    # ------------------------------------------------ contacts

    def create_contact(
        first_name: FirstName,
        *,
        last_name: Name64 | None = None,
        title: Title10 | None = None,
        company: Name64 | None = None,
        description: Notes | None = None,
        email: Email | None = None,
        mobile: Phone | None = None,
        business_phone: Phone | None = None,
        business_address: Address | None = None,
        webpage: Web | None = None,
        location: Name64 | None = None,
        confirm: bool = False,
    ) -> Written:
        """Create a contact (a person). Link it afterwards with crm.link_contact_account,
        crm.link_contact_vendor or crm.link_contact_project. If a likely duplicate exists (same email,
        phone or name) nothing is created and the candidates are returned; call again with confirm=true."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Add", "create contacts")
        if not confirm:
            found = contact_candidates(
                ctx, email=email, phone=mobile or business_phone, first_name=first_name, last_name=last_name
            )
            if dups := _likely(found):
                return Written(record=None, duplicates=dups)
        with ctx.writer("crm") as con:
            cur = con.execute(
                _INSERT_CONTACT,
                {
                    "first_name": first_name, "last_name": last_name, "title": title, "company": company,
                    "description": description, "email": email, "mobile": mobile,
                    "business_phone": business_phone, "business_address": business_address,
                    "webpage": webpage, "location": location,
                },
            )  # fmt: skip
            return Written(record=_contact_out(con, int(cur.lastrowid or 0)), created=True)

    reg(create_contact, "crm.create_contact", "Create a contact", WRITE)

    def update_contact(
        contact: HandleText,
        *,
        first_name: FirstName | None = None,
        last_name: Name64 | None = None,
        title: Title10 | None = None,
        company: Name64 | None = None,
        description: Notes | None = None,
        email: Email | None = None,
        mobile: Phone | None = None,
        business_phone: Phone | None = None,
        business_address: Address | None = None,
        webpage: Web | None = None,
        location: Name64 | None = None,
        status: Status | None = None,
    ) -> Written:
        """Change a contact (handle crm:contact:<id>): name, title, company, description, email, mobile or
        business phone, business address, web page, location or status (1000 Active, 5000 Inactive).
        Fields you leave out are unchanged; an empty string clears a text field."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Edit", "change contacts")
        cid = record_id(contact, "crm", "contact", "contact")
        with ctx.writer("crm") as con:
            _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            con.execute(
                _UPDATE_CONTACT,
                {
                    "id": cid, "first_name": first_name, "last_name": last_name, "title": title,
                    "company": company, "description": description, "email": email, "mobile": mobile,
                    "business_phone": business_phone, "business_address": business_address,
                    "webpage": webpage, "location": location, "status": status,
                },
            )  # fmt: skip
            return Written(record=_contact_out(con, cid))

    reg(update_contact, "crm.update_contact", "Change a contact", IDEMPOTENT_WRITE)

    def delete_contact(contact: HandleText) -> Written:
        """Delete a contact (handle crm:contact:<id>): it is marked deleted (status -1) and hidden; nothing
        is erased. It stops being any account's or vendor's primary contact."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Delete", "delete contacts")
        cid = record_id(contact, "crm", "contact", "contact")
        with ctx.writer("crm") as con:
            _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            con.execute(_DELETE_CONTACT, (cid,))
            con.execute(_CLEAR_PRIMARY_ACCOUNTS, (now(), cid))
            con.execute(_CLEAR_PRIMARY_VENDORS, (now(), cid))
            return Written(record=_contact_out(con, cid))

    reg(delete_contact, "crm.delete_contact", "Delete a contact", WRITE)

    # ------------------------------------------------ links

    def link_contact_account(contact: HandleText, account: HandleText, primary: bool = False) -> Written:
        """Link a contact (crm:contact:<id>) with a customer account (crm:account:<id>). With primary=true
        it also becomes the account's primary contact. A contact belongs to one account or one vendor:
        unlink it from a vendor first."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Edit", "change contacts")
        if primary:
            ctx.require_to(caller, ACCOUNT, "Edit", "change accounts")
        cid = record_id(contact, "crm", "contact", "contact")
        aid = record_id(account, "crm", "account", "account")
        with ctx.writer("crm") as con:
            row = _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            _live(con, _ACCOUNT_ROW, aid, f"account {account}")
            if _as_int(row["vendor_id"]) > 0:
                raise ValidationError("the contact is linked to a vendor; unlink it first", field="contact")
            old = _as_int(row["account_id"])
            if old and old != aid:
                con.execute(_UNSET_PRIMARY_ACCOUNT, (now(), old, cid))
            con.execute(_SET_CONTACT_ACCOUNT, (aid, cid))
            if primary:
                con.execute(_SET_PRIMARY_ACCOUNT, (cid, now(), aid))
            return Written(record=_contact_out(con, cid))

    reg(link_contact_account, "crm.link_contact_account", "Link a contact with a customer", IDEMPOTENT_WRITE)

    def unlink_contact_account(contact: HandleText) -> Written:
        """Unlink a contact (crm:contact:<id>) from its customer account; it also stops being that
        account's primary contact."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Edit", "change contacts")
        cid = record_id(contact, "crm", "contact", "contact")
        with ctx.writer("crm") as con:
            row = _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            if old := _as_int(row["account_id"]):
                con.execute(_UNSET_PRIMARY_ACCOUNT, (now(), old, cid))
            con.execute(_SET_CONTACT_ACCOUNT, (0, cid))
            return Written(record=_contact_out(con, cid))

    reg(
        unlink_contact_account,
        "crm.unlink_contact_account",
        "Unlink a contact from a customer",
        IDEMPOTENT_WRITE,
    )

    def link_contact_vendor(contact: HandleText, vendor: HandleText, primary: bool = False) -> Written:
        """Link a contact (crm:contact:<id>) with a vendor (crm:vendor:<id>). With primary=true it also
        becomes the vendor's primary contact. A contact belongs to one account or one vendor: unlink it
        from an account first."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Edit", "change contacts")
        if primary:
            ctx.require_to(caller, VENDOR, "Edit", "change vendors")
        cid = record_id(contact, "crm", "contact", "contact")
        vid = record_id(vendor, "crm", "vendor", "vendor")
        with ctx.writer("crm") as con:
            row = _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            _live(con, _VENDOR_ROW, vid, f"vendor {vendor}")
            if _as_int(row["account_id"]) > 0:
                raise ValidationError("the contact is linked to an account; unlink it first", field="contact")
            old = _as_int(row["vendor_id"])
            if old and old != vid:
                con.execute(_UNSET_PRIMARY_VENDOR, (now(), old, cid))
            con.execute(_SET_CONTACT_VENDOR, (vid, cid))
            if primary:
                con.execute(_SET_PRIMARY_VENDOR, (cid, now(), vid))
            return Written(record=_contact_out(con, cid))

    reg(link_contact_vendor, "crm.link_contact_vendor", "Link a contact with a vendor", IDEMPOTENT_WRITE)

    def unlink_contact_vendor(contact: HandleText) -> Written:
        """Unlink a contact (crm:contact:<id>) from its vendor; it also stops being that vendor's primary
        contact."""
        caller = ctx.caller()
        ctx.require_to(caller, CONTACT, "Edit", "change contacts")
        cid = record_id(contact, "crm", "contact", "contact")
        with ctx.writer("crm") as con:
            row = _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            if old := _as_int(row["vendor_id"]):
                con.execute(_UNSET_PRIMARY_VENDOR, (now(), old, cid))
            con.execute(_SET_CONTACT_VENDOR, (0, cid))
            return Written(record=_contact_out(con, cid))

    reg(
        unlink_contact_vendor, "crm.unlink_contact_vendor", "Unlink a contact from a vendor", IDEMPOTENT_WRITE
    )

    def link_contact_project(contact: HandleText, project: HandleText, role: Role | None = None) -> Written:
        """Link a contact (crm:contact:<id>) with a project (pm:project:<id>), with an optional role such
        as "sponsor". Linking an already linked pair only changes the role."""
        caller = ctx.caller()
        ctx.require_to(caller, PROJECT_CONTACT, "Add", "link contacts with projects")
        ctx.require_record(caller, PROJECT, f"project {project}")
        cid = record_id(contact, "crm", "contact", "contact")
        pid = record_id(project, "pm", "project", "project")
        if ctx.one("pm", _PROJECT_EXISTS, (pid,)) is None:
            raise NotFound(f"project {project} not found")
        with ctx.writer("crm") as con:
            _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            if con.execute(_LINK_ROW, (pid, cid)).fetchone() is None:
                con.execute(_LINK_INSERT, (pid, cid, role))
            elif role is not None:
                con.execute(_LINK_ROLE, (role, pid, cid))
            return Written(record=_contact_out(con, cid))

    reg(link_contact_project, "crm.link_contact_project", "Link a contact with a project", IDEMPOTENT_WRITE)

    def unlink_contact_project(contact: HandleText, project: HandleText) -> Written:
        """Unlink a contact (crm:contact:<id>) from a project (pm:project:<id>). The link is removed; the
        contact and the project are not changed."""
        caller = ctx.caller()
        ctx.require_to(caller, PROJECT_CONTACT, "Delete", "unlink contacts from projects")
        cid = record_id(contact, "crm", "contact", "contact")
        pid = record_id(project, "pm", "project", "project")
        with ctx.writer("crm") as con:
            _live(con, _CONTACT_ROW, cid, f"contact {contact}")
            if con.execute(_LINK_DELETE, (pid, cid)).rowcount == 0:
                raise NotFound(f"contact {contact} is not linked with project {project}")
            return Written(record=_contact_out(con, cid))

    reg(unlink_contact_project, "crm.unlink_contact_project", "Unlink a contact from a project", WRITE)
