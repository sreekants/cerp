"""MPC001-162 SQLite access and coexistence."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from cerp.data.sqlite import Databases, DbOptions
from tests.conftest import seed_tickets


def _dbs(dbroot: Path, **opts: int) -> Databases:
    return Databases({"im": dbroot / "im.s3db", "crm": dbroot / "crm.s3db"}, DbOptions(**opts))  # type: ignore[arg-type]


def test_reader_cannot_write_even_if_code_tries(dbroot: Path) -> None:
    dbs = _dbs(dbroot)
    with (
        dbs.reader("im") as con,
        pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only|query_only"),
    ):
        con.execute("UPDATE tickets SET priority = 1")


def test_writer_commits_and_rolls_back(dbroot: Path) -> None:
    seed_tickets(dbroot)
    dbs = _dbs(dbroot)
    with dbs.reader("im") as con:
        before = con.execute("SELECT priority FROM tickets ORDER BY id LIMIT 1").fetchone()[0]

    def update_then_fail() -> None:
        with dbs.writer("im") as con:
            con.execute("UPDATE tickets SET priority = 9 WHERE id = (SELECT MIN(id) FROM tickets)")
            raise RuntimeError("abort")

    with pytest.raises(RuntimeError):
        update_then_fail()
    with dbs.reader("im") as con:
        assert con.execute("SELECT priority FROM tickets ORDER BY id LIMIT 1").fetchone()[0] == before
    with dbs.writer("im") as con:
        con.execute("UPDATE tickets SET priority = 9 WHERE id = (SELECT MIN(id) FROM tickets)")
    with dbs.reader("im") as con:
        assert con.execute("SELECT priority FROM tickets ORDER BY id LIMIT 1").fetchone()[0] == 9


def test_writer_uses_begin_immediate(dbroot: Path) -> None:
    """A second writer is refused at BEGIN, not later at COMMIT."""
    dbs = _dbs(dbroot, busy_timeout_ms=50)
    other = sqlite3.connect(dbroot / "im.s3db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"), dbs.writer("im"):
            pass
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_one_connection_per_thread(dbroot: Path) -> None:
    dbs = _dbs(dbroot)
    ids: list[int] = []

    def grab() -> None:
        with dbs.reader("im") as a, dbs.reader("im") as b:
            assert a is b
            ids.append(id(a))

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    grab()
    assert len(set(ids)) == 3


def test_journal_mode_check_warns_and_never_changes_mode(
    dbroot: Path, caplog: pytest.LogCaptureFixture
) -> None:
    dbs = _dbs(dbroot)
    modes = dbs.check_journal_modes()
    assert modes == {"crm": "delete", "im": "delete"}
    assert "not WAL" in caplog.text
    assert _dbs(dbroot).check_journal_modes() == modes


def test_slow_write_is_logged(dbroot: Path, caplog: pytest.LogCaptureFixture) -> None:
    import time  # noqa: PLC0415

    dbs = _dbs(dbroot, write_budget_ms=1)
    with dbs.writer("im"):
        time.sleep(0.01)
    assert "budget 1 ms" in caplog.text


def test_unknown_datasource(dbroot: Path) -> None:
    with pytest.raises(KeyError), _dbs(dbroot).reader("nope"):
        pass


def test_table_columns(dbroot: Path) -> None:
    dbs = _dbs(dbroot)
    cols = dbs.table_columns("im", "tickets")
    assert cols is not None
    assert "short_name" in cols
    assert dbs.table_columns("im", "no_such_table") is None
