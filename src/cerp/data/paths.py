"""Confine a caller-supplied relative path to a root folder (MPC001-065).

Used by any tool that touches files (e.g. project documents, MPC001-229). Rejects absolute paths, drive
letters, ``..`` segments, NUL and control characters, Windows device names and alternate data streams, and
anything that resolves outside the root (including through links).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_DEVICE = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.I)


class PathRejected(ValueError):
    pass


def confine(root: Path, relative: str) -> Path:
    if not relative or len(relative) > 1024:  # noqa: PLR2004 - generous bound for nested project folders
        raise PathRejected("empty or over-long path")
    text = unicodedata.normalize("NFC", relative).replace("\\", "/")
    if any(ord(c) < 32 or c in '<>:"|?*' for c in text):  # noqa: PLR2004 - control characters
        raise PathRejected("path contains a forbidden character")
    if text.startswith("/"):
        raise PathRejected("path must be relative")
    parts = text.split("/")  # not PurePosixPath: it silently drops "." and empty segments
    for part in parts:
        if part in ("", "..", ".") or _DEVICE.match(part) or part.endswith((" ", ".")):
            raise PathRejected(f"path segment {part!r} is not allowed")
    base = root.resolve()
    target = base.joinpath(*parts).resolve()
    if target != base and base not in target.parents:
        raise PathRejected("path leaves the allowed folder")
    return target
