"""cerp (Claude ERP): an MCP server and Claude personas for the webos small-business ERP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cerp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
