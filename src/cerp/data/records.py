"""webos record conventions, derived from the registry (MPC001-163).

* **Deleted rows** are hidden: the model's ``DeletedState`` (e.g. ``status=-1``) or, for models without one,
  ``status = -1`` when the table has a ``status`` column (an assumption the registry lists as a warning).
* **Inserts** get the model's ``ModelDefaults`` plus creator/modification columns set to the mapped user and
  server time.
* **Concurrency token**: a hash of the record's current column values, so no schema change is needed.
* **'NULL' strings**: webos stores some empty values as the text ``'NULL'``; reads return them as ``None``.

Time-zone conversion for ``ConvertTimeFields`` is not applied yet: the ``TimezoneContext`` rule
(``Cookie:OTMZ``) depends on the web session and is still to be documented for webos.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cerp.data.registry import Model

CREATOR_COLUMNS = ("created_by",)
MODIFIER_COLUMNS = ("modified_by",)
CREATION_TIME_COLUMNS = ("creation_time",)
MODIFICATION_TIME_COLUMNS = ("modification_time",)


@dataclass(frozen=True)
class DeletedRule:
    column: str
    value: Any
    assumed: bool  # True when inferred from a status column rather than declared by DeletedState


def deleted_rule(model: Model) -> DeletedRule | None:
    if model.deleted_state is not None:
        return DeletedRule(model.deleted_state[0], model.deleted_state[1], assumed=False)
    cols = model.column_names or []
    if "status" in cols:
        return DeletedRule("status", -1, assumed=True)
    return None


def visible_clause(model: Model) -> tuple[str, tuple[Any, ...]]:
    """SQL condition (with bound values) that excludes deleted rows; ``("1=1", ())`` if none applies.

    The column name comes from the registry, never from a caller, and is quoted.
    """
    rule = deleted_rule(model)
    if rule is None:
        return "1=1", ()
    col = quote_ident(rule.column)
    return f"({col} IS NULL OR {col} <> ?)", (rule.value,)


def quote_ident(name: str) -> str:
    if not name or '"' in name or "\x00" in name:
        raise ValueError(f"unsafe identifier {name!r}")
    return f'"{name}"'


def clean_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the row with webos's literal ``'NULL'`` strings as ``None``."""
    return {k: (None if v == "NULL" else v) for k, v in row.items()}


def concurrency_token(row: Mapping[str, Any]) -> str:
    """A stable hash of a record's current column values (MPC001-163)."""
    blob = json.dumps({k: row[k] for k in sorted(row)}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _coerce_default(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def insert_values(
    model: Model, values: Mapping[str, Any], *, user: str | int, now: float | None = None
) -> dict[str, Any]:
    """The full column→value map for a new record, applying webos conventions.

    ``values`` wins over ``ModelDefaults``; creator and time columns are set when the table has them and the
    caller did not supply them. Only columns that exist in the table are returned.
    """
    cols = set(model.column_names or [])
    stamp = int(now if now is not None else time.time())
    out: dict[str, Any] = {k: _coerce_default(v) for k, v in model.model_defaults.items() if k in cols}
    for group, value in (
        (CREATOR_COLUMNS, user),
        (MODIFIER_COLUMNS, user),
        (CREATION_TIME_COLUMNS, stamp),
        (MODIFICATION_TIME_COLUMNS, stamp),
    ):
        for c in group:
            if c in cols:
                out[c] = value
    out.update({k: v for k, v in values.items() if k in cols})
    return out
