"""Load the generated webos model registry (MPC002-006, MPC001-080).

The registry YAML is the server's only source of model metadata; the server never reads ``.wsm``,
``.wsidl`` or ``datasources.xml`` itself. Loading refuses an unsupported schema version, and
:attr:`Registry.fingerprint` (a hash over the recorded source hashes) is logged at startup so the audit
trail shows which model set was in force.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from cerp.data.datasources import DatasourceError, DatasourceLocator

SCHEMA_VERSION: Final = 1


class RegistryError(Exception):
    pass


def field_name(class_name: str) -> str:
    """Port of ``BusinessObject::GetFieldName``: ``ProductCategory`` → ``product_category``."""
    if not class_name:
        return ""
    out = [class_name[0].lower()]
    for ch in class_name[1:]:
        out.append("_" + ch.lower() if ch.isupper() else ch)
    return "".join(out)


def pluralize(word: str) -> str:
    """English plural as used for webos table names. Mismatches are caught by the live-schema check."""
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def table_name_for(class_name: str) -> str:
    """The table a ``Database.Table`` model uses when its manifest has no ``<Table>``."""
    return pluralize(field_name(class_name)).lower()


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    not_null: bool
    default: str | None
    primary_key: bool


@dataclass(frozen=True)
class Model:
    key: str  # e.g. "objects/support/ticket"
    type: str | None
    model: str | None
    description: str | None
    datasource: str | None
    table: str | None
    columns: tuple[Column, ...] | None
    deleted_state: tuple[str, Any] | None
    model_defaults: Mapping[str, str | None]
    convert_time_fields: tuple[str, ...]
    privilege_model: str | None
    access_enforced: bool
    methods: tuple[Mapping[str, Any], ...]

    @property
    def column_names(self) -> list[str] | None:
        return None if self.columns is None else [c.name for c in self.columns]

    @property
    def is_table(self) -> bool:
        return self.type == "Database.Table"


@dataclass(frozen=True)
class Registry:
    schema_version: int
    generator_version: str
    source_hashes: Mapping[str, str]
    datasources: Mapping[str, str]
    models: Mapping[str, Model]
    warnings: tuple[Mapping[str, str], ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(dict(sorted(self.source_hashes.items())), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def database_files(self, dbroot: Path) -> dict[str, Path]:
        """Datasource name → database path with ``$(DBROOT)`` = ``dbroot``, resolved by the datasource
        locator (MPC001-200). Datasources that need other variables are left out."""
        out: dict[str, Path] = {}
        for name, f in self.datasources.items():
            try:
                out[name] = DatasourceLocator({name: f}, {"DBROOT": str(dbroot)}, Path.cwd(), {}).path(name)
            except DatasourceError:
                continue
        return out


def _model(key: str, raw: Mapping[str, Any]) -> Model:
    ident = raw.get("identity", {})
    storage = raw.get("storage", {})
    behaviour = raw.get("behaviour", {})
    access = raw.get("access", {})
    cols = raw.get("columns")
    deleted = behaviour.get("deleted_state")
    return Model(
        key=key,
        type=ident.get("type"),
        model=ident.get("model"),
        description=ident.get("description"),
        datasource=storage.get("datasource"),
        table=storage.get("table"),
        columns=None
        if cols is None
        else tuple(
            Column(
                c["name"],
                c.get("type") or "",
                bool(c.get("not_null")),
                c.get("default"),
                bool(c.get("primary_key")),
            )
            for c in cols
        ),
        deleted_state=None if not deleted else (deleted["field"], deleted["value"]),
        model_defaults=dict(behaviour.get("model_defaults") or {}),
        convert_time_fields=tuple(behaviour.get("convert_time_fields") or ()),
        privilege_model=access.get("privilege_model"),
        access_enforced=bool(access.get("enforced")),
        methods=tuple(raw.get("methods") or ()),
    )


def load_registry(path: Path) -> Registry:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RegistryError(f"cannot read registry {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise RegistryError(f"registry {path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise RegistryError(f"registry {path} must be a mapping")
    version = raw.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RegistryError(
            f"registry {path} has schema version {version!r}; this server supports {SCHEMA_VERSION}. "
            "Regenerate it with `cerp registry generate`."
        )
    try:
        models = {k: _model(k, v) for k, v in (raw.get("models") or {}).items()}
        return Registry(
            schema_version=version,
            generator_version=str((raw.get("generator") or {}).get("version", "")),
            source_hashes=dict((raw.get("sources") or {}).get("sha256") or {}),
            datasources={k: v["file"] for k, v in (raw.get("datasources") or {}).items()},
            models=models,
            warnings=tuple(raw.get("warnings") or ()),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise RegistryError(f"registry {path} is malformed: {exc!r}") from None
