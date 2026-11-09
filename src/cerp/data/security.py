"""Read access to the webos security database (MPC001-060, MPC001-061).

Ports the three queries webos uses (``Session.cpp``, ``DbConnect.cpp``): a user by name or id, the user's
rights (``memberships`` → ``groups`` → ``privileges`` → ``rights``) and membership of the Administrators
group (id 1, ``ADMIN_GROUP_ID``). Rights are returned as exact names; matching them is the authorization
function's job (:mod:`cerp.auth.authorize`), which never matches by substring (WOS-BUG-035).

Every failure to read (missing file, missing table, SQLite error) raises :class:`SecurityUnavailable`, so a
caller can only fail closed (MPC001-061 requirement 4). Connections are read-only (MPC001-162).

This module imports nothing else from cerp except the data layer (MPC001-005).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Final

from cerp.data.sqlite import Databases, UnknownDatasource

SECURITY: Final = "security"
ADMIN_GROUP_ID: Final = 1

_ENABLED_TEXT: Final = frozenset({"1", "y", "yes", "true"})

_USER_BY_NAME: Final = "SELECT id, name, first_name, last_name, enabled FROM users WHERE name = ?"
_USER_BY_ID: Final = "SELECT id, name, first_name, last_name, enabled FROM users WHERE id = ?"
_RIGHTS: Final = (
    "SELECT DISTINCT rights.name FROM memberships, groups, privileges, rights "
    "WHERE privileges.right_id = rights.id AND privileges.group_id = groups.id "
    "AND groups.id = memberships.group_id AND memberships.user_id = ?"
)
_IN_GROUP: Final = "SELECT COUNT(*) FROM memberships WHERE user_id = ? AND group_id = ?"


class SecurityUnavailable(Exception):
    """The security database could not be read. Callers must deny."""


def is_enabled(value: Any) -> bool:
    """webos stores ``users.enabled`` inconsistently (``1``, ``0``, ``'Y'``, NULL). Only clear true values
    count as enabled; anything else (NULL, ``0``, ``'N'``, unknown text) is disabled (fail closed)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _ENABLED_TEXT
    return False


@dataclass(frozen=True)
class SecurityUser:
    id: int
    name: str
    display_name: str
    enabled: bool


class SecurityDirectory:
    """Users, rights and administrator membership from ``security.s3db``."""

    def __init__(self, databases: Databases) -> None:
        self._dbs = databases

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        try:
            with self._dbs.reader(SECURITY) as con:
                return list(con.execute(sql, params).fetchall())
        except (sqlite3.Error, OSError, UnknownDatasource) as exc:
            raise SecurityUnavailable(f"security database unreadable: {type(exc).__name__}") from None

    @staticmethod
    def _user(row: sqlite3.Row | None) -> SecurityUser | None:
        if row is None:
            return None
        names = [n for n in (row["first_name"], row["last_name"]) if n and n != "NULL"]
        return SecurityUser(
            id=int(row["id"]),
            name=str(row["name"]),
            display_name=" ".join(names) or str(row["name"]),
            enabled=is_enabled(row["enabled"]),
        )

    def user_by_name(self, name: str) -> SecurityUser | None:
        rows = self._rows(_USER_BY_NAME, (name,))
        return self._user(rows[0] if rows else None)

    def user_by_id(self, user_id: int) -> SecurityUser | None:
        rows = self._rows(_USER_BY_ID, (user_id,))
        return self._user(rows[0] if rows else None)

    def rights(self, user_id: int) -> frozenset[str]:
        return frozenset(str(r[0]) for r in self._rows(_RIGHTS, (user_id,)) if r[0])

    def is_administrator(self, user_id: int) -> bool:
        rows = self._rows(_IN_GROUP, (user_id, ADMIN_GROUP_ID))
        return bool(rows and rows[0][0])
