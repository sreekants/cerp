"""Build the MCP server instance. Tools are registered by later work (MPC001-080, MPC001-088)."""

from mcp.server.mcpserver import MCPServer

from cerp import __version__

INSTRUCTIONS = (
    "cerp gives access to a small business's webos ERP records (CRM, service, projects, inventory). "
    "No tools are registered yet in this development build."
)


def build_server() -> MCPServer:
    """Return a new, unconfigured cerp MCP server."""
    return MCPServer(name="cerp", title="cerp (Claude ERP)", version=__version__, instructions=INSTRUCTIONS)
