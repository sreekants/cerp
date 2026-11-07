"""SQLite access to the webos module databases, coexisting with the web UI (MPC001-162).

* One connection per thread per database and mode, reused; ``busy_timeout`` and ``foreign_keys`` as
  configured.
* :meth:`Databases.reader` gives a **read-only** connection (``mode=ro`` URI): a read tool cannot write even
  if its code tries.
* :meth:`Databases.writer` gives a read-write connection inside ``BEGIN IMMEDIATE`` … ``COMMIT``. The block
  must be short and synchronous: it is a plain ``with`` block, so it cannot span an ``await``. Blocks that
  exceed the write budget (default 200 ms) are logged.
* :meth:`Databases.check_journal_modes` reports databases that are not in WAL mode. It never changes the
  journal mode or schema of a webos database.

This module imports nothing else from cerp (MPC001-005).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("cerp.data")


class UnknownDatasource(KeyError):
    pass


@dataclass(frozen=True)
class DbOptions:
    busy_timeout_ms: int = 5000
    foreign_keys: bool = False
    write_budget_ms: int = 200


class Databases:
    """The set of module databases named in the datasource map."""

    def __init__(self, files: Mapping[str, Path], options: DbOptions | None = None) -> None:
        self._files = {name: Path(p).resolve() for name, p in files.items()}
        self._opts = options or DbOptions()
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()

    @property
    def names(self) -> list[str]:
        return sorted(self._files)

    def path(self, name: str) -> Path:
        try:
            return self._files[name]
        except KeyError:
            raise UnknownDatasource(name) from None

    def _open(self, name: str, *, writable: bool) -> sqlite3.Connection:
        path = self.path(name)
        if not path.is_file():
            raise FileNotFoundError(f"database for datasource {name!r} not found: {path}")
        mode = "rw" if writable else "ro"
        con = sqlite3.connect(
            f"{path.as_uri()}?mode={mode}",
            uri=True,
            timeout=self._opts.busy_timeout_ms / 1000,
            isolation_level=None,  # explicit transactions only
            check_same_thread=False,  # one thread uses each connection; close_all() may run elsewhere
        )
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout = {int(self._opts.busy_timeout_ms)}")
        con.execute(f"PRAGMA foreign_keys = {'ON' if self._opts.foreign_keys else 'OFF'}")
        if not writable:
            con.execute("PRAGMA query_only = ON")
        return con

    def _conn(self, name: str, *, writable: bool) -> sqlite3.Connection:
        cache: dict[tuple[str, bool], sqlite3.Connection] | None = getattr(self._local, "cache", None)
        if cache is None:
            cache = {}
            self._local.cache = cache
        key = (name, writable)
        con = cache.get(key)
        if con is None:
            con = cache[key] = self._open(name, writable=writable)
            with self._all_lock:
                self._all.append(con)
        return con

    @contextmanager
    def reader(self, name: str) -> Iterator[sqlite3.Connection]:
        """A read-only connection. Reads outside a transaction see each statement's own snapshot."""
        yield self._conn(name, writable=False)

    @contextmanager
    def writer(self, name: str) -> Iterator[sqlite3.Connection]:
        """A write transaction: ``BEGIN IMMEDIATE`` now, ``COMMIT`` on success, ``ROLLBACK`` on error."""
        con = self._conn(name, writable=True)
        started = time.perf_counter()
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")
        finally:
            took_ms = (time.perf_counter() - started) * 1000
            if took_ms > self._opts.write_budget_ms:
                log.warning(
                    "write transaction on %s took %.0f ms (budget %d ms)",
                    name,
                    took_ms,
                    self._opts.write_budget_ms,
                )

    def can_open(self, name: str) -> bool:
        """True if the database exists and answers a read (readiness check, MPC001-037)."""
        try:
            with self.reader(name) as con:
                con.execute("SELECT 1").fetchone()
        except (sqlite3.Error, OSError, UnknownDatasource):
            return False
        return True

    def check_journal_modes(self) -> dict[str, str]:
        """Journal mode per existing database; logs a warning for each that is not WAL."""
        modes: dict[str, str] = {}
        for name in self.names:
            if not self._files[name].is_file():
                continue
            with self.reader(name) as con:
                mode = str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            modes[name] = mode
            if mode != "wal":
                log.warning(
                    "database %s is in %s journal mode, not WAL; concurrent writes will block readers",
                    name,
                    mode,
                )
        return modes

    def table_columns(self, name: str, table: str) -> list[str] | None:
        """Column names of a live table, or None if it does not exist."""
        with self.reader(name) as con:
            rows = con.execute("SELECT name FROM pragma_table_info(?)", (table,)).fetchall()
        return [r[0] for r in rows] or None

    def close_all(self) -> None:
        """Close every connection opened by any thread (graceful shutdown, MPC001-117).

        Call only after request handling has stopped. An open transaction is rolled back by SQLite on close.
        """
        with self._all_lock:
            conns, self._all = self._all, []
        for con in conns:
            try:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                con.close()
            except sqlite3.ProgrammingError:
                pass  # created in another thread and already closed there
        cache = getattr(self._local, "cache", None)
        if cache:
            cache.clear()

    def close(self) -> None:
        cache: dict[tuple[str, bool], sqlite3.Connection] | None = getattr(self._local, "cache", None)
        if cache:
            for con in cache.values():
                con.close()
            cache.clear()
