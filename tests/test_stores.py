"""MPC001-166 server-owned stores: forward-only migrations, a backup first, no partial upgrades."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from cerp import cli
from cerp.data.stores import STORES, MigrationError, StoreSpec, current_version, migrate, migrate_all

V1 = ("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL);",)
V2 = (*V1, "ALTER TABLE notes ADD COLUMN author TEXT; CREATE INDEX notes_author ON notes(author);")


def _rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT * FROM notes ORDER BY id").fetchall()


def test_new_store_is_created_at_the_latest_version(tmp_path: Path) -> None:
    r = migrate(StoreSpec("s", V2), tmp_path / "s.s3db")
    assert (r.from_version, r.to_version, r.backup) == (0, 2, None)
    assert current_version(tmp_path / "s.s3db") == 2


def test_upgrade_keeps_data_and_takes_a_backup(tmp_path: Path) -> None:
    db = tmp_path / "s.s3db"
    migrate(StoreSpec("s", V1), db)
    with closing(sqlite3.connect(db)) as con:
        con.execute("INSERT INTO notes (body) VALUES ('kept')")
        con.commit()
    r = migrate(StoreSpec("s", V2), db)
    assert (r.from_version, r.to_version) == (1, 2)
    assert r.backup is not None
    assert current_version(r.backup) == 1
    assert _rows(db) == [(1, "kept", None)]
    again = migrate(StoreSpec("s", V2), db)
    assert again.backup is None  # nothing to do, nothing touched


def test_failed_migration_leaves_the_file_intact(tmp_path: Path) -> None:
    db = tmp_path / "s.s3db"
    migrate(StoreSpec("s", V1), db)
    with closing(sqlite3.connect(db)) as con:
        con.execute("INSERT INTO notes (body) VALUES ('kept')")
        con.commit()
    before = db.read_bytes()
    broken = (*V1, "ALTER TABLE notes ADD COLUMN x TEXT; CREATE TABLE notes (id INTEGER);")
    with pytest.raises(MigrationError, match="version 2"):
        migrate(StoreSpec("s", broken), db)
    assert current_version(db) == 1
    assert _rows(db) == [(1, "kept")]
    assert db.read_bytes() == before


def test_failed_new_store_leaves_nothing(tmp_path: Path) -> None:
    with pytest.raises(MigrationError):
        migrate(StoreSpec("s", ("CREATE TABLE t (;",)), tmp_path / "s.s3db")
    assert not (tmp_path / "s.s3db").exists()


def test_newer_file_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "s.s3db"
    migrate(StoreSpec("s", V2), db)
    with pytest.raises(MigrationError, match="refusing to downgrade"):
        migrate(StoreSpec("s", V1), db)


def test_release_stores_build_and_cli_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert {r.to_version for r in migrate_all(STORES, tmp_path / "a")} == {s.version for s in STORES}
    monkeypatch.setenv("CERP__DATA__STATE_DIR", str(tmp_path / "b"))
    assert cli.main(["migrate"]) == 0
    assert all((tmp_path / "b" / f"{s.name}.s3db").is_file() for s in STORES)
