"""Tool names and the tool catalogue version (MPC001-082, MPC001-119).

Generic tools are named ``<module>.<verb>_<model>`` (``crm.get_account``) and curated tools
``<module>.<task>`` (``im.assign_incident``). Names use only letters, digits, ``_``, ``-`` and ``.``, are at
most 64 characters, and are unique per server.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

# Bump on any breaking change to a tool's name, arguments or result shape (MPC001-119).
TOOL_CATALOGUE_VERSION: Final = 1

MAX_LENGTH: Final = 64
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_PART = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolNameError(ValueError):
    pass


def check_name(name: str) -> str:
    if len(name) > MAX_LENGTH:
        raise ToolNameError(f"tool name {name!r} is longer than {MAX_LENGTH} characters")
    if not _NAME.match(name):
        raise ToolNameError(f"tool name {name!r} may only contain letters, digits, '_', '-' and '.'")
    return name


def generic_name(module: str, verb: str, model: str) -> str:
    for label, part in (("module", module), ("verb", verb), ("model", model)):
        if not _PART.match(part):
            raise ToolNameError(f"{label} {part!r} must be lower_snake_case")
    return check_name(f"{module}.{verb}_{model}")


def curated_name(module: str, task: str) -> str:
    for label, part in (("module", module), ("task", task)):
        if not _PART.match(part):
            raise ToolNameError(f"{label} {part!r} must be lower_snake_case")
    return check_name(f"{module}.{task}")


def check_unique(names: Iterable[str]) -> None:
    seen: set[str] = set()
    dups: set[str] = set()
    for n in names:
        check_name(n)
        (dups if n in seen else seen).add(n)
    if dups:
        raise ToolNameError(f"duplicate tool names: {', '.join(sorted(dups))}")
