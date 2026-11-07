"""Kill switches: turn off writes, a module or a tool at runtime (MPC001-116).

Switches live in configuration under ``kill_switches`` and are reloadable (MPC001-115)::

    kill_switches:
      writes: true            # every tool that is not read-only
      module:crm: true        # every crm.* tool
      tool:im.create_ticket: true

A switched-off tool disappears from ``tools/list`` (filtered by the request middleware in
:mod:`cerp.protocol.boundary`) and a call to it returns ``isError`` with code ``disabled``. The switches
are read on every request, so a reload takes effect on the next call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class KillSwitch:
    def __init__(self, switches: Callable[[], Mapping[str, bool]]) -> None:
        self._switches = switches

    def blocked(self, tool: str, *, read_only: bool) -> str | None:
        """The switch that blocks ``tool``, or None. Module is the part of the name before the first dot."""
        s = self._switches()
        module = tool.split(".", 1)[0]
        for key in (f"tool:{tool}", f"module:{module}"):
            if s.get(key):
                return key
        if not read_only and s.get("writes"):
            return "writes"
        return None

    def filter_tools(self, tools: list[Any]) -> list[Any]:
        """Drop switched-off tools from a wire-format ``tools/list`` result."""
        out = []
        for t in tools:
            if not isinstance(t, dict):
                out.append(t)
                continue
            ann = t.get("annotations") or {}
            if self.blocked(str(t.get("name", "")), read_only=bool(ann.get("readOnlyHint"))) is None:
                out.append(t)
        return out


NO_SWITCHES = KillSwitch(dict)
