"""Server-owned SQLite stores with numbered, forward-only migrations (MPC001-166).

State that cerp owns (approvals, idempotency keys, confirm tokens, authorization-server clients and grants,
queues, saved views, the audit outbox) lives in cerp's own SQLite files, never in a webos database.

Each store is a :class:`StoreSpec`: a name and an ordered tuple of migrations (SQL scripts). The file
records its version in ``PRAGMA user_version``. :func:`migrate` brings a file up to date:

* nothing to do → the file is not touched;
* otherwise a backup is taken first (``<file>.v<old>.bak`` via SQLite's online backup), then the pending
  migrations run in **one** transaction; on any error the transaction is rolled back and the file is left
  exactly as it was;
* a file whose version is newer than the code knows is refused (no downgrades).

Changes to webos-owned tables are not made here; they are reviewed extensions with their own migration and
a note in the module docs.

Imports nothing else from cerp (MPC001-005).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("cerp.data")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class StoreSpec:
    name: str
    migrations: tuple[str, ...]  # migrations[0] takes the file from version 0 to 1, and so on

    @property
    def version(self) -> int:
        return len(self.migrations)


@dataclass(frozen=True)
class MigrationResult:
    store: str
    path: Path
    from_version: int
    to_version: int
    backup: Path | None


def current_version(path: Path) -> int:
    if not path.exists():
        return 0
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as con:
        return int(con.execute("PRAGMA user_version").fetchone()[0])


def _backup(path: Path, version: int) -> Path:
    target = path.with_name(f"{path.name}.v{version}.bak")
    with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    return target


def migrate(spec: StoreSpec, path: Path) -> MigrationResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    old = current_version(path)
    if old > spec.version:
        raise MigrationError(
            f"store {spec.name}: {path} is at version {old}, newer than this cerp ({spec.version}); "
            "refusing to downgrade"
        )
    if old == spec.version and existed:
        return MigrationResult(spec.name, path, old, old, None)
    backup = _backup(path, old) if existed and old > 0 else None
    con = sqlite3.connect(path, isolation_level=None)
    n = old
    try:
        con.execute("BEGIN IMMEDIATE")
        for n in range(old, spec.version):
            for statement in _statements(spec.migrations[n]):
                con.execute(statement)
        con.execute(f"PRAGMA user_version = {spec.version}")
        con.execute("COMMIT")
    except Exception as exc:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
        if not existed:
            path.unlink(missing_ok=True)  # a new store that failed to build leaves nothing behind
        raise MigrationError(f"store {spec.name}: migration to version {n + 1} failed: {exc}") from exc
    con.close()
    log.info("store %s: migrated %s from version %d to %d", spec.name, path.name, old, spec.version)
    return MigrationResult(spec.name, path, old, spec.version, backup)


def _statements(script: str) -> Iterable[str]:
    """Split a migration script into statements (DDL only; no semicolons inside literals)."""
    buf = ""
    for part in script.split(";"):
        buf += part
        if sqlite3.complete_statement(buf + ";"):
            if buf.strip():
                yield buf.strip()
            buf = ""
        else:
            buf += ";"
    if buf.strip():
        raise MigrationError(f"incomplete SQL statement in migration: {buf.strip()[:60]!r}")


def migrate_all(specs: Iterable[StoreSpec], folder: Path) -> list[MigrationResult]:
    """Migrate every store in ``folder`` (``<name>.s3db``)."""
    return [migrate(spec, folder / f"{spec.name}.s3db") for spec in specs]


# The stores this release defines. Later requirements append migrations; never edit a released one.
STORES: tuple[StoreSpec, ...] = (
    StoreSpec(
        "cerp_state",
        (
            """
            CREATE TABLE idempotency_keys (
                key TEXT PRIMARY KEY, principal TEXT NOT NULL, tool TEXT NOT NULL,
                result TEXT, created_at TEXT NOT NULL
            );
            CREATE TABLE saved_views (
                id INTEGER PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'user',
                module TEXT NOT NULL, name TEXT NOT NULL, filters TEXT NOT NULL, sort TEXT,
                created_at TEXT NOT NULL, UNIQUE (owner, module, name)
            );
            """,
        ),
    ),
)
