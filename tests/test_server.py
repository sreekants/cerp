import anyio
from mcp import Client

from cerp import __version__
from cerp.app.server import build_server


async def _list_tools() -> tuple[str, int]:
    async with Client(build_server()) as client:
        tools = await client.list_tools()
        return __version__, len(tools.tools)


def test_server_starts_with_no_tools() -> None:
    version, n_tools = anyio.run(_list_tools)
    assert version
    assert n_tools == 0
