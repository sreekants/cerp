"""Model exposure: which registry models, verbs and fields the server offers (MPC001-080).

The exposure file names, per registry model, the verbs exposed, a field allow-list, sortable fields and the
descriptions a model sees::

    models:
      objects/support/ticket:
        name: ticket                 # tool-facing model name (default: from the webos class name)
        verbs: [describe]
        fields: [id, short_name, status, severity, priority, owner, creation_time]
        sortable: [creation_time]
        description: A customer or internal support ticket.
        field_descriptions:
          status: "1000 Open, 2000 Fixed, 3000 Reviewed, 4000 Tested, 5000 Closed"

Startup fails, with every problem listed, if the exposure names a model, field or verb the registry does
not have, or if an exposed table's live columns differ from the registry (drift since generation).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

from cerp.data.registry import Model, Registry, field_name

VERBS: Final = ("describe", "query", "get", "create", "update", "transition")


class ExposureError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("invalid exposure:\n  " + "\n  ".join(errors))


@dataclass(frozen=True)
class ExposedModel:
    key: str
    module: str  # the datasource name, used as the tool prefix
    name: str
    model: Model
    verbs: tuple[str, ...]
    fields: tuple[str, ...]
    sortable: tuple[str, ...]
    description: str
    field_descriptions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Exposure:
    models: tuple[ExposedModel, ...]

    def get(self, module: str, name: str) -> ExposedModel | None:
        return next((m for m in self.models if (m.module, m.name) == (module, name)), None)


def _strs(value: Any, where: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{where}: must be a list of strings")
        return []
    return list(value)


LiveColumns = Callable[[str, str], list[str] | None]


def _drift(where: str, model: Model, live_columns: LiveColumns) -> list[str]:
    if not model.datasource or not model.table or model.column_names is None:
        return []  # _entry has already reported this model
    live = live_columns(model.datasource, model.table)
    if live is None:
        return [f"{where}: table {model.datasource}.{model.table} does not exist in the database"]
    missing = [c for c in model.column_names if c not in live]
    added = [c for c in live if c not in model.column_names]
    if not (missing or added):
        return []
    return [
        f"{where}: table {model.datasource}.{model.table} has drifted since the registry was generated "
        f"(missing: {', '.join(missing) or '-'}; new: {', '.join(added) or '-'})"
    ]


def _entry(key: str, spec: Any, registry: Registry, errors: list[str]) -> ExposedModel | None:
    where = f"models.{key}"
    model = registry.models.get(key)
    if model is None:
        errors.append(f"{where}: no such model in the registry")
        return None
    if not isinstance(spec, dict):
        errors.append(f"{where}: must be a mapping")
        return None
    if not model.is_table or not model.datasource or not model.table:
        errors.append(f"{where}: only Database.Table models with a datasource and table can be exposed")
        return None
    columns = model.column_names
    if columns is None:
        errors.append(f"{where}: registry has no columns for {model.table}; regenerate with --dbroot")
        return None
    verbs = _strs(spec.get("verbs"), f"{where}.verbs", errors) or []
    fields = _strs(spec.get("fields"), f"{where}.fields", errors)
    sortable = _strs(spec.get("sortable"), f"{where}.sortable", errors)
    fdesc = spec.get("field_descriptions") or {}
    if not isinstance(fdesc, dict):
        errors.append(f"{where}.field_descriptions: must be a mapping")
        fdesc = {}
    errors += [
        f"{where}.verbs: unknown verb {v!r} (allowed: {', '.join(VERBS)})" for v in verbs if v not in VERBS
    ]
    errors += [f"{where}.fields: no column {f!r} in {model.table}" for f in fields if f not in columns]
    errors += [f"{where}.sortable: {f!r} is not an exposed field" for f in sortable if f not in fields]
    errors += [f"{where}.field_descriptions: {f!r} is not an exposed field" for f in fdesc if f not in fields]
    errors += [
        f"{where}.{k}: at least one entry is required"
        for k, v in (("verbs", verbs), ("fields", fields))
        if not v
    ]
    return ExposedModel(
        key=key,
        module=model.datasource,
        name=str(spec.get("name") or field_name(model.model or key.rsplit("/", 1)[-1])),
        model=model,
        verbs=tuple(verbs),
        fields=tuple(fields),
        sortable=tuple(sortable),
        description=str(spec.get("description") or model.description or key),
        field_descriptions={str(k): str(v) for k, v in fdesc.items()},
    )


def load_exposure(path: Path, registry: Registry, live_columns: LiveColumns | None = None) -> Exposure:
    """Validate the exposure file against the registry and, if given, the live schema."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ExposureError([f"{path}: cannot read ({exc.strerror})"]) from None
    except yaml.YAMLError as exc:
        raise ExposureError([f"{path}: not valid YAML ({exc})"]) from None
    entries = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise ExposureError([f"{path}: top level must have a 'models' mapping"])
    errors: list[str] = []
    out: list[ExposedModel] = []
    for key, spec in sorted(entries.items()):
        em = _entry(str(key), spec, registry, errors)
        if em is None:
            continue
        if live_columns is not None:
            errors += _drift(f"models.{key}", em.model, live_columns)
        out.append(em)
    if errors:
        raise ExposureError(errors)
    return Exposure(tuple(out))
