"""Record handles ``<module>:<model>:<id>`` (MPC001-087).

A handle names a record for its whole life and is the only id form tools accept. It carries no authority:
every use is re-authorized, and a record the caller cannot read must be reported exactly like a missing one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PART = r"[a-z][a-z0-9_.]*"
_HANDLE = re.compile(rf"^({_PART}):({_PART}):([1-9][0-9]{{0,18}})$")
MAX_ID = 2**63 - 1


class HandleError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Handle:
    module: str
    model: str
    id: int

    def __str__(self) -> str:
        return f"{self.module}:{self.model}:{self.id}"

    @classmethod
    def parse(cls, text: str) -> Handle:
        m = _HANDLE.match(text.strip()) if isinstance(text, str) else None
        if not m or int(m.group(3)) > MAX_ID:
            raise HandleError(f"not a record handle: {text!r} (expected <module>:<model>:<positive id>)")
        return cls(m.group(1), m.group(2), int(m.group(3)))

    def expect(self, module: str, model: str) -> Handle:
        if (self.module, self.model) != (module, model):
            raise HandleError(f"handle {self} is not a {module}:{model} record")
        return self
