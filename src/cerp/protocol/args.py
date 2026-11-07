"""Bounded argument types for tool signatures (MPC001-065).

Tools declare string arguments with these types so every string in a tool's input schema has a
``maxLength``; ``tests/test_args.py`` fails the build if a registered tool has an unbounded string.
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import StringConstraints

SHORT_MAX: Final = 4096
FREE_TEXT_MAX: Final = 65536

ShortText = Annotated[str, StringConstraints(max_length=SHORT_MAX)]
"""Names, titles, filters, codes: at most 4,096 characters."""

FreeText = Annotated[str, StringConstraints(max_length=FREE_TEXT_MAX)]
"""Descriptions, comments, message bodies: at most 65,536 characters."""

HandleText = Annotated[
    str, StringConstraints(max_length=200, pattern=r"^[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*:[1-9][0-9]*$")
]
"""A record handle ``<module>:<model>:<id>`` (MPC001-087)."""
