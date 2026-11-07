"""The OAuth scope catalogue, generated from the model registry (MPC001-077).

For every webos module (datasource) with table models: ``webos.<module>.read`` (``*.View`` rights) and
``webos.<module>.write`` (``*.Add`` and ``*.Edit``). No scope covers ``*.Delete``. Checks live in
:mod:`cerp.protocol.scopes`.
"""

from __future__ import annotations

from dataclasses import dataclass

from cerp.data.registry import Registry


@dataclass(frozen=True)
class Scope:
    name: str
    description: str


def catalogue(registry: Registry) -> list[Scope]:
    modules = sorted({m.datasource for m in registry.models.values() if m.is_table and m.datasource})
    out: list[Scope] = []
    for mod in modules:
        out.append(Scope(f"webos.{mod}.read", f"Read {mod} records (webos View rights)."))
        out.append(
            Scope(f"webos.{mod}.write", f"Create and change {mod} records (webos Add and Edit rights).")
        )
    return out
