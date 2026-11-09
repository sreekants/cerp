"""MPC001-200 datasource locator and template bootstrap."""

from __future__ import annotations

import copy
import hashlib
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cerp.app.config import ConfigError, Settings
from cerp.app.server import StartupError, build_runtime
from cerp.data import datasources
from cerp.data.datasources import DatasourceError, DatasourceLocator
from cerp.data.registry import load_registry
from cerp.data.sqlite import UnknownDatasource
from cerp.registry.generate import build, render
from tests.conftest import PORTAL, REPO, TEMPLATES, TICKET_EXPOSURE

REPO_XML = REPO / "config" / "datasources.xml"


def _tenant(tmp_path: Path) -> Path:
    """An application root with the repository's datasource file and a copy of the templates."""
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    shutil.copy2(REPO_XML, root / "config" / "datasources.xml")
    (root / "data" / "template").mkdir(parents=True)
    for f in TEMPLATES.glob("*.s3db"):
        shutil.copy2(f, root / "data" / "template" / f.name)
    return root


def _hashes(folder: Path) -> dict[str, str]:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(folder.glob("*"))}


def _xml(tmp_path: Path, body: str, env: str = "") -> Path:
    p = tmp_path / "ds.xml"
    p.write_text(f"<DataSources><Environment>{env}</Environment><Sources>{body}</Sources></DataSources>")
    return p


def _ds(name: str, conn: str) -> str:
    return f"<DataSource><Name>{name}</Name><ConnectString>{conn}</ConnectString></DataSource>"


def _var(name: str, value: str) -> str:
    return f"<Variable><Name>{name}</Name><Value>{value}</Value></Variable>"


def test_repository_file_resolves_against_root(tmp_path: Path) -> None:
    loc = DatasourceLocator.load(REPO_XML, {}, root=tmp_path)
    assert loc.path("im") == (tmp_path / "data" / "im.s3db").resolve()
    assert loc.path("bi.crm") == (tmp_path / "data" / "bi.s3db").resolve()
    assert loc.template_for("im") == (tmp_path / "data" / "template" / "im.s3db").resolve()
    assert loc.variables == {"DBROOT": "data", "TEMPLATE": "data/template"}
    with pytest.raises(UnknownDatasource):
        loc.path("nope")


def test_variable_resolution_order(tmp_path: Path) -> None:
    body = _ds("a", "DRIVER={SQLite};DBQ=$(FILEVAR)/a.s3db") + _ds(
        "b", "DRIVER={SQLite};DBQ=$(ENVVAR)/b.s3db;"
    )
    xml = _xml(tmp_path, body, _var("FILEVAR", "from-file"))
    loc = DatasourceLocator.load(xml, {"ENVVAR": "from-env", "FILEVAR": "ignored"}, root=tmp_path)
    assert loc.path("a") == (tmp_path / "from-file" / "a.s3db").resolve()
    assert loc.path("b") == (tmp_path / "from-env" / "b.s3db").resolve()
    with pytest.raises(DatasourceError, match=r"datasource b: variable \$\(ENVVAR\)"):
        DatasourceLocator.load(xml, {}, root=tmp_path)


def test_non_sqlite_and_dbq_less_sources_are_skipped(tmp_path: Path) -> None:
    body = (
        _ds("ok", "DRIVER={SQLite};DBQ=x/ok.s3db")
        + _ds("odbc", "DRIVER={SQL Server};SERVER=db;DATABASE=x")
        + _ds("nodbq", "DRIVER={SQLite};")
    )
    loc = DatasourceLocator.load(_xml(tmp_path, body), {}, root=tmp_path)
    assert loc.names() == ["ok"]
    assert loc.skipped == ["odbc", "nodbq"]


def test_bootstrap_creates_from_templates_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = _tenant(tmp_path)
    before = _hashes(root / "data" / "template")
    loc = DatasourceLocator.load(root / "config" / "datasources.xml", {}, root=root)
    caplog.set_level(logging.INFO, logger="cerp.data")
    report = loc.bootstrap()
    created = {name for name, _, _ in report.created}
    templated = {n for n in loc.names() if (root / "data" / "template" / loc.path(n).name).is_file()}
    assert created == templated
    assert set(report.unavailable) == set(loc.names()) - templated
    assert "security" in report.unavailable
    lines = [r.getMessage() for r in caplog.records]
    assert sum(m.startswith("datasource ") and "from template" in m for m in lines) == len(created)
    assert any(m.startswith(f"datasources: {len(loc.names())} found, {len(created)} created") for m in lines)
    assert _hashes(root / "data" / "template") == before
    for _, db, tpl in report.created:
        assert db.read_bytes() == tpl.read_bytes()

    caplog.clear()
    again = DatasourceLocator.load(root / "config" / "datasources.xml", {}, root=root).bootstrap()
    assert again.created == []
    assert not any(": created " in r.getMessage() for r in caplog.records)


def test_existing_file_is_never_replaced(tmp_path: Path) -> None:
    root = _tenant(tmp_path)
    (root / "data" / "im.s3db").write_bytes(b"mine")
    DatasourceLocator.load(root / "config" / "datasources.xml", {}, root=root).bootstrap()
    assert (root / "data" / "im.s3db").read_bytes() == b"mine"


def test_interrupted_copy_leaves_no_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _tenant(tmp_path)

    def half(r: Any, w: Any) -> None:
        w.write(r.read(100))
        raise KeyboardInterrupt  # the process is stopped mid-copy

    monkeypatch.setattr(datasources.shutil, "copyfileobj", half)
    loc = DatasourceLocator.load(root / "config" / "datasources.xml", {}, root=root)
    with pytest.raises(KeyboardInterrupt):
        loc.bootstrap()
    leftovers = [p.name for p in (root / "data").iterdir() if p.is_file()]
    assert leftovers == []


def test_datasource_inside_template_folder_is_refused(tmp_path: Path) -> None:
    xml = _xml(
        tmp_path,
        _ds("bad", "DRIVER={SQLite};DBQ=$(TEMPLATE)/im.s3db"),
        _var("TEMPLATE", "data/template"),
    )
    with pytest.raises(DatasourceError, match="templates must stay pristine"):
        DatasourceLocator.load(xml, {}, root=tmp_path)


def _settings(root: Path, **data: Any) -> Settings:
    return Settings.model_validate(
        {"data": {"datasources_file": str(root / "config" / "datasources.xml"), "root": str(root), **data}}
    )


def test_server_bootstraps_and_refuses_when_disabled(tmp_path: Path) -> None:
    root = _tenant(tmp_path)
    with pytest.raises(ConfigError, match="bootstrap is disabled"):
        build_runtime(_settings(root, bootstrap_from_templates=False), {})
    _, rt = build_runtime(_settings(root), {})
    assert (root / "data" / "im.s3db").is_file()
    assert "security" in rt.unavailable
    assert rt.databases is not None
    assert "security" not in rt.databases.names


def test_startup_fails_only_if_an_exposed_model_needs_an_unavailable_datasource(
    tmp_path: Path, write_yaml: Callable[[str, Any], Path]
) -> None:
    root = _tenant(tmp_path)
    reg = tmp_path / "reg.yaml"
    reg.write_bytes(render(build(PORTAL, REPO_XML, root / "data" / "template")))
    exposure = write_yaml("e.yaml", copy.deepcopy(TICKET_EXPOSURE))
    base = {"registry": {"path": str(reg)}, "exposure": {"path": str(exposure)}}
    s = Settings.model_validate(base | {"data": _settings(root).data.model_dump(mode="json")})
    _, rt = build_runtime(s, {})  # security has no template, but nothing exposed needs it
    assert rt.tool_names == ["im.describe_ticket"]

    (root / "data" / "im.s3db").unlink()
    (root / "data" / "template" / "im.s3db").unlink()
    with pytest.raises(StartupError, match=r"unavailable datasources.*\bim\b"):
        build_runtime(s, {})


def test_generator_and_server_resolve_the_same_paths(tmp_path: Path) -> None:
    dbroot = tmp_path / "tenant"
    reg = tmp_path / "reg.yaml"
    reg.write_bytes(render(build(PORTAL, REPO_XML, None)))
    from_registry = load_registry(reg).database_files(dbroot)
    loc = DatasourceLocator.load(REPO_XML, {}, root=Path.cwd(), overrides={"DBROOT": str(dbroot)})
    assert from_registry == loc.paths()


def test_non_strict_locator_lists_unresolvable_datasources(tmp_path: Path) -> None:
    xml = tmp_path / "datasources.xml"
    xml.write_text(
        "<DataSources>"
        "<DataSource><Name>a</Name><ConnectString>DRIVER={SQLite};DBQ=$(DBROOT)/a.s3db</ConnectString></DataSource>"
        "<DataSource><Name>b</Name><ConnectString>DRIVER={SQLite};DBQ=$(DOMAINROOT)/b.s3db</ConnectString></DataSource>"
        "</DataSources>",
        encoding="utf-8",
    )
    with pytest.raises(DatasourceError, match="DOMAINROOT"):
        DatasourceLocator.load(xml, {}, root=tmp_path, overrides={"DBROOT": str(tmp_path)})
    loc = DatasourceLocator.load(xml, {}, root=tmp_path, overrides={"DBROOT": str(tmp_path)}, strict=False)
    assert loc.names() == ["a"]
    assert loc.unresolved == ["b"]


def test_server_with_lenient_datasources_never_creates_files(tmp_path: Path) -> None:
    root = _tenant(tmp_path)
    before = sorted(p.name for p in (root / "data").glob("*.s3db"))
    _, rt = build_runtime(_settings(root, bootstrap_from_templates=False, strict_datasources=False), {})
    assert sorted(p.name for p in (root / "data").glob("*.s3db")) == before
    assert "security" in rt.unavailable
