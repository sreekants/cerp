"""Output filtering for records returned by tools (MPC001-066).

* Only fields on the exposed model's allow-list are returned.
* A field whose name looks like a secret (password, hash, salt, secret, token, key, ssn, ...) is never
  returned, even if someone allow-lists it; :mod:`cerp.data.exposure` also refuses to expose one.
* Values are returned as data, unchanged: free text from the database goes into ``structuredContent``
  as a field value and is never concatenated into instruction-like text.

This module imports nothing else from cerp (MPC001-005).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "hash",
        "salt",
        "secret",
        "token",
        "key",
        "ssn",
        "pin",
        "otp",
        "apikey",
    }
)
_SPLIT = re.compile(r"[^a-z0-9]+|(?<=[a-z])(?=[A-Z])")


def is_secret_field(name: str) -> bool:
    """True if any word of the column name is a secret word (``user_password``, ``ApiKey``, ``pw_hash``)."""
    words = {w.lower() for w in _SPLIT.split(name) if w}
    words |= {w for w in _SPLIT.split(name.lower()) if w}
    return bool(words & SECRET_WORDS)


def filter_record(row: Mapping[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
    """The allow-listed, non-secret fields of ``row``, in allow-list order."""
    return {f: row[f] for f in allowed if f in row and not is_secret_field(f)}
