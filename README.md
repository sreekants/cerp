# cerp — Claude ERP

cerp is an [MCP](https://modelcontextprotocol.io) server that lets Claude read and act on a small business's
**webos** ERP records — CRM, service tickets, projects, inventory, finance, HR and assets — and a set of Claude
**personas** that stand in for missing back-office roles (bookkeeper, stock clerk, procurement, support, office admin
and others). Every persona works as its own webos user, proposes drafts rather than acting on its own, and asks a
human to approve anything that commits the business.

> **Status:** pre-alpha. The skeleton runs; no business tools are exposed yet.

## Requirements

- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/)
- A webos installation (its SQLite databases) for anything beyond the smoke tests

## Getting started

```
uv sync
uv run pytest
uv run cerp --version
uv run cerp stdio            # for Claude Desktop / Claude Code on the same machine
uv run cerp serve            # Streamable HTTP on http://127.0.0.1:7411/mcp
```

To try it in Claude Code: `claude mcp add cerp -- uv run --directory <path-to-cerp> cerp stdio`.

## Design and requirements

- `docs/decisions/` — architecture decisions, starting with the Python baseline.
- `CLAUDE.md` — conventions and rules for working on the code (for people and for Claude).
- Requirements are tracked in the Cortex repository under `bugs/ai/` (MPC001 for the server, MPC002 for personas).

## License

MIT — see `LICENSE`.
