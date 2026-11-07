"""Locate webos databases from ``datasources.xml`` and bootstrap missing ones from templates (MPC001-200).

The datasource file has an ``<Environment>`` (variables such as ``DBROOT`` and ``TEMPLATE``) and
``<Sources>`` (each ``<DataSource>`` has a ``<Name>`` and a ``<ConnectString>`` of the form
``DRIVER={SQLite};DBQ=<path>``). :class:`DatasourceLocator` is the one place that turns a datasource name
into a database path:

* ``$(NAME)`` takes the ``<Environment>`` value, else the process environment, else it is an error naming
  the datasource and the variable;
* relative paths resolve against the application root;
* non-SQLite connect strings, and ones without ``DBQ``, are reported and skipped.

:meth:`DatasourceLocator.bootstrap` copies a template into place for every datasource whose file is missing
(temporary file + atomic rename, never overwriting, never writing to the template) and reports what it did.
A datasource that resolves into the template folder is refused: templates stay pristine.

Standard library only; imports nothing else from cerp (MPC001-005).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cerp.data.sqlite import UnknownDatasource

log = logging.getLogger("cerp.data")

_VAR = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
_DBQ = re.compile(r"(?:^|;)\s*DBQ\s*=\s*([^;]+)", re.I)
_DRIVER = re.compile(r"(?:^|;)\s*DRIVER\s*=\s*\{?([^};]+)\}?", re.I)


class DatasourceError(Exception):
    """The datasource file is unusable; every problem is listed."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def substitute(text: str, variables: Mapping[str, str], environ: Mapping[str, str], where: str) -> str:
    """Replace every ``$(NAME)``: file variables first, then the process environment."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in variables:
            return variables[name]
        if name in environ:
            return environ[name]
        raise DatasourceError(
            [f"{where}: variable $({name}) is not defined in <Environment> or the environment"]
        )

    return _VAR.sub(repl, text)


def read_datasource_file(path: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Parse ``datasources.xml`` without resolving anything: (name → raw DBQ, variables, skipped names)."""
    try:
        doc = ET.parse(path).getroot()  # noqa: S314 - operator-supplied local configuration file
    except (OSError, ET.ParseError) as exc:
        raise DatasourceError([f"{path}: cannot read datasource file ({exc})"]) from None
    variables: dict[str, str] = {}
    for var in doc.iter("Variable"):
        name = (var.findtext("Name") or "").strip()
        if name:
            variables[name] = (var.findtext("Value") or "").strip()
    raw: dict[str, str] = {}
    skipped: list[str] = []
    for ds in doc.iter("DataSource"):
        name = (ds.findtext("Name") or "").strip()
        conn = (ds.findtext("ConnectString") or "").strip()
        if not name:
            continue
        driver = _DRIVER.search(conn)
        dbq = _DBQ.search(conn)
        if not driver or driver.group(1).strip().lower() != "sqlite" or not dbq:
            log.warning("datasource %s: not a SQLite connect string with DBQ; skipped", name)
            skipped.append(name)
            continue
        raw[name] = dbq.group(1).strip()
    return raw, variables, skipped


@dataclass(frozen=True)
class BootstrapReport:
    found: int
    created: list[tuple[str, Path, Path]] = field(default_factory=list)  # (name, database, template)
    unavailable: list[str] = field(default_factory=list)


class DatasourceLocator:
    def __init__(
        self,
        raw: Mapping[str, str],
        variables: Mapping[str, str],
        root: Path,
        environ: Mapping[str, str],
        skipped: list[str] | None = None,
        *,
        strict: bool = True,
    ) -> None:
        self.root = root.resolve()
        self.raw = dict(raw)  # name -> DBQ text before substitution
        self.variables = dict(variables)
        self.skipped = list(skipped or [])
        errors: list[str] = []
        self.unresolved: list[str] = []
        self._paths: dict[str, Path] = {}
        for name, dbq in self.raw.items():
            try:
                self._paths[name] = self._absolute(
                    substitute(dbq, self.variables, environ, f"datasource {name}")
                )
            except DatasourceError as exc:
                if strict:
                    errors += exc.errors
                else:
                    self.unresolved.append(name)
        self._template_dir: Path | None = None
        if "TEMPLATE" in self.variables or "TEMPLATE" in environ:
            self._template_dir = self._absolute(
                substitute("$(TEMPLATE)", self.variables, environ, "TEMPLATE")
            )
            errors += [
                f"datasource {n}: resolves into the template folder {self._template_dir}; "
                "templates must stay pristine"
                for n, p in self._paths.items()
                if self._template_dir == p.parent or self._template_dir in p.parents
            ]
        if errors:
            raise DatasourceError(errors)

    def _absolute(self, text: str) -> Path:
        p = Path(text.strip().replace("\\", "/"))
        return (p if p.is_absolute() else self.root / p).resolve()

    @classmethod
    def load(
        cls,
        path: Path,
        environ: Mapping[str, str] | None = None,
        *,
        root: Path | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> DatasourceLocator:
        """Read and validate ``datasources.xml``.

        ``overrides`` replace ``<Environment>`` values, e.g. ``DBROOT`` for a test tenant.
        """
        env = os.environ if environ is None else environ
        raw, variables, skipped = read_datasource_file(path)
        variables.update(overrides or {})
        return cls(raw, variables, root or Path.cwd(), env, skipped)

    def names(self) -> list[str]:
        return sorted(self._paths)

    def path(self, name: str) -> Path:
        try:
            return self._paths[name]
        except KeyError:
            raise UnknownDatasource(name) from None

    def paths(self) -> dict[str, Path]:
        return dict(self._paths)

    def template_for(self, name: str) -> Path | None:
        """``$(TEMPLATE)/<file name of the DBQ path>``, or None if no TEMPLATE variable is defined."""
        if self._template_dir is None:
            return None
        return self._template_dir / self.path(name).name

    def bootstrap(self, *, enabled: bool = True) -> BootstrapReport:
        """Create missing databases from their templates. Existing files are never touched."""
        report = BootstrapReport(found=len(self._paths))
        for name in self.names():
            target = self.path(name)
            if target.is_file():
                continue
            template = self.template_for(name)
            if not enabled or template is None or not template.is_file():
                report.unavailable.append(name)
                log.warning(
                    "datasource %s: %s is missing%s; unavailable",
                    name,
                    target,
                    "" if enabled else " and bootstrap is disabled",
                )
                continue
            _copy_atomically(template, target)
            report.created.append((name, target, template))
            log.warning("datasource %s: created %s from template %s", name, target, template)
        log.info(
            "datasources: %d found, %d created from templates, %d unavailable",
            report.found,
            len(report.created),
            len(report.unavailable),
        )
        return report


def _place(tmp: Path, dst: Path) -> None:
    """Move a complete file into place without ever overwriting an existing one."""
    if os.name == "nt":
        tmp.rename(dst)  # Windows rename refuses an existing target
        return
    try:
        os.link(tmp, dst)  # POSIX: link refuses an existing target; unlinked by the caller
    except FileExistsError:
        raise
    except OSError:
        if dst.exists():  # file systems without hard links
            raise FileExistsError(dst) from None
        tmp.rename(dst)


def _copy_atomically(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".part", dir=dst.parent)
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        with src.open("rb") as r, tmp_path.open("wb") as w:
            shutil.copyfileobj(r, w)
            w.flush()
            os.fsync(w.fileno())
        try:
            _place(tmp_path, dst)
        except FileExistsError:
            log.warning("datasource file %s appeared during bootstrap; left as it is", dst)
    finally:
        tmp_path.unlink(missing_ok=True)
