"""Shared fixtures. Tests never open data/template directly: they copy it to a temporary folder first."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from cerp.registry.generate import build, render

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "data" / "template"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "webos"
PORTAL = FIXTURE / "portal"
DATASOURCES = FIXTURE / "datasources.xml"

TICKET_EXPOSURE: dict[str, Any] = {
    "models": {
        "objects/support/ticket": {
            "verbs": ["describe"],
            "fields": ["id", "short_name", "status", "severity", "priority", "owner", "creation_time"],
            "sortable": ["creation_time"],
            "description": "A customer or internal support ticket.",
            "field_descriptions": {
                "status": "1000 Open, 2000 Fixed, 3000 Reviewed, 4000 Tested, 5000 Closed"
            },
        }
    }
}


@pytest.fixture
def dbroot(tmp_path: Path) -> Path:
    """A private copy of the template databases."""
    out = tmp_path / "db"
    out.mkdir()
    for f in TEMPLATES.glob("*.s3db"):
        shutil.copy2(f, out / f.name)
    return out


def seed_tickets(dbroot: Path, count: int = 2) -> None:
    """Insert test tickets into the private copy: templates may ship with empty tables."""
    con = sqlite3.connect(dbroot / "im.s3db")
    try:
        con.executemany(
            "INSERT INTO tickets (short_name, status, priority) VALUES (?, 1000, 5)",
            [(f"test ticket {i}",) for i in range(count)],
        )
        con.commit()
    finally:
        con.close()


@pytest.fixture
def registry_file(tmp_path: Path, dbroot: Path) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_bytes(render(build(PORTAL, DATASOURCES, dbroot)))
    return path


@pytest.fixture
def write_yaml(tmp_path: Path) -> Callable[[str, Any], Path]:
    def write(name: str, data: Any) -> Path:
        p = tmp_path / name
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
        return p

    return write
