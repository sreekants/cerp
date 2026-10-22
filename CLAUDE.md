# cerp — notes for Claude

cerp (Claude ERP) is a Python MCP server that exposes a small business's webos ERP data to Claude, plus Claude
personas (Bookkeeper, Stock Clerk, …) that work as back-office staff under human approval.

## Where the requirements are

The requirements live in the Cortex repository, not here:

- `bugs/ai/MPC001/` — the server (protocol, transport, security, data binding, operation). Index: `MPC001/INDEX.md`.
- `bugs/ai/MPC002/` — the persona layer (lifecycle, platform tools, approvals, queues, runner, triage).
- `bugs/ai/PRD-MPC001.md`, `PRD-MPC002.md` — the product requirements both trace to.
- `bugs/ai/archive/` — retired C++ (`cmpc`) material. Do not implement anything from it.

On the development machine Cortex is at `G:\devel\cortex`. Read the ticket before implementing it and cite its id
(e.g. `MPC001-061`) in commit messages, PR titles and module docstrings. If code and a ticket disagree, ask; do
not silently change either.

The webos application (models, manifests, WSIDL) is at `G:\cluster\wwwroot\portal`; the persona specs and packaged
skills are in Cortex at `docs/webos/office/specs/`.

## Commands

```
uv sync                      # create .venv and install (dev group included)
uv run pytest                # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict
uv run lint-imports          # layer contract (MPC001-005)
uv run cerp --help
```

Always run all four checks before saying a change is done. Use `uv add` / `uv add --dev` to change dependencies so
`uv.lock` stays in step; never edit the lockfile by hand or `pip install` into the environment.

## Architecture (MPC001-005, MPC001-160)

`src/cerp/` is layered; import-linter enforces it:

| Layer | Package | May import |
|---|---|---|
| top | `cerp.app` — ASGI app, config, CLI | everything |
| | `cerp.persona` — approvals, queues, scheduler, runner | tools, protocol, auth, data |
| | `cerp.tools` — generic, curated, platform tools | protocol, auth, data |
| | `cerp.protocol` — MCP SDK wiring, registry, schemas | auth (never data) |
| | `cerp.auth` — authentication, principal mapping, authorization port | data |
| bottom | `cerp.data` — registry loader, SQLite access, record conventions | nothing in cerp |

Key choices: Python ≥ 3.13 with uv; official MCP Python SDK (`mcp`, `MCPServer`) over Streamable HTTP and stdio;
direct SQLite access to the webos databases through the generated model registry; OAuth only (no HMAC); the persona
runner uses the Claude Agent SDK (`claude-agent-sdk`, optional extra `runner`). See `docs/decisions/0001-python-baseline.md`.

## Rules that are easy to break

- **SQL:** identifiers only from the registry, values only as `?` parameters. Never build SQL with f-strings (MPC001-008, MPC001-065).
- **Read-only by default:** read tools use `mode=ro` connections; keep write transactions short (`BEGIN IMMEDIATE`, no `await` inside) (MPC001-162).
- **No deletes:** persona-reachable code never issues `DELETE`; use the model's soft-delete state (MPC002-044, MPC001-163).
- **Never change webos schemas** except through a reviewed migration (MPC001-166). The C++ web UI writes the same files.
- **Authorization** goes through one function and must be equal to or stricter than webos (MPC001-061, MPC002-007). Fail closed.
- **Drafts first:** nothing a persona creates is sent or finalised without an approval record (MPC002-045, MPC002-071).
- **Secrets** (API keys, signing keys) never in code, config files, logs or test fixtures (MPC001-056).
- **Errors** to clients carry a correlation id, never tracebacks, SQL or paths (MPC001-067).
- Code must run on Windows and Linux: use `pathlib`, no shell-specific commands in the package.

## Tests

pytest, with Hypothesis for parsers and validators. Test the server in-process with `mcp.Client(build_server())`.
Never point tests at real business databases; use fixtures built by the tenant seed script (MPC001-150) once it exists.
