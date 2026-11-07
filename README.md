# cerp — Claude ERP

cerp is an [MCP](https://modelcontextprotocol.io) server that lets Claude read and act on a small business's
**webos** ERP records — CRM, service tickets, projects, inventory, finance, HR and assets — and a set of Claude
**personas** that stand in for missing back-office roles (bookkeeper, stock clerk, procurement, support, office admin
and others). Every persona works as its own webos user, proposes drafts rather than acting on its own, and asks a
human to approve anything that commits the business.

> **Status:** pre-alpha. The server foundations run (configuration, logging, errors, Streamable HTTP and stdio,
> development tokens, the model registry and exposure). The only tools are read-only `describe` tools; record
> tools wait on authorization (MPC001-061). See `docs/release-plan.md`.

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

## Running against webos data

1. Copy the databases you want to serve (never `data/template` itself) to a working folder, e.g. `var/db`.
2. Generate the model registry from the webos application tree (run again whenever webos models change):

   ```
   uv run cerp registry generate --portal G:/cluster/wwwroot/portal ^
       --datasources G:/devel/cortex/web/config/Inetpub/sys/config/datasources.xml ^
       --dbroot var/db --out var/registry/webos.yaml
   ```

3. Copy `config/cerp.example.yaml` to `config/cerp.local.yaml` (git-ignored), adjust paths, and choose what to
   expose in an exposure file (`config/exposure.example.yaml` shows the ticket model).
4. Check and run:

   ```
   uv run cerp config check --config config/cerp.local.yaml
   uv run cerp serve --config config/cerp.local.yaml
   ```

Any setting can be overridden with an environment variable such as `CERP__HTTP__PORT=8000`.

## Design and requirements

- `docs/webos/decisions/` — architecture decisions, starting with the Python baseline.
- `docs/coverage-map.md` — what the MCP SDK provides and what cerp adds (MPC001-161).
- `docs/release-plan.md` — MVP scope, waves and blockers (MPC001-167, draft).
- `CLAUDE.md` — conventions and rules for working on the code (for people and for Claude).
- `bugs/` — requirements: PRDs, MPC001 for the server, MPC002 for personas, and retired C++ material in `bugs/archive/`.

## License

MIT — see `LICENSE`.
