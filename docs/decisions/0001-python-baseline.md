# 0001 — Python implementation baseline

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-22 |
| **Requirement** | MPC001-160 |

## Context

The MCP server for webos was first specified as a C++ library (`cmpc`) inside Cortex. Reading the webos code showed
that most models are generic table wrappers, workflows run in the browser, and authorization is a naming convention.
The work that matters — curated tools, approvals, queues, the persona runner, tests — is new code that is faster and
safer to write in Python, and the Claude Agent SDK exists for Python but not C++.

## Decision

- **Name:** cerp (Claude ERP). Package `cerp`, command `cerp`, repository `github.com/sreekants/cerp`.
- **Runtime:** CPython 3.13 or later on Windows 11 and Linux (x86-64, ARM64). Project, environments and dependencies
  managed with uv and a committed `uv.lock`.
- **Protocol:** the official MCP Python SDK (`mcp` 2.x, `MCPServer`) in Streamable HTTP mode served by uvicorn, plus
  stdio for local clients (MPC001-161, MPC001-168).
- **Data:** direct SQLite access to the webos module databases, described by a generated model registry
  (MPC002-006). webos models and sessions are not invoked. Authorization is ported from `AuthenticationAspect` and
  must be equal to or stricter than it, proved by a parity test (MPC001-061, MPC002-007).
- **Authentication:** OAuth 2.1 bearer tokens (external identity provider or a built-in authorization server);
  static tokens on loopback for development; operating-system user mapping in stdio mode. No HMAC.
- **Persona layer:** queues, scheduler and approvals in Python on SQLite; the runner uses the Claude Agent SDK for
  Python, confined to cerp's own tools (MPC002-082, MPC002-133).
- **Quality:** ruff, mypy strict, import-linter layer contract, pytest and Hypothesis; CI on Windows and Linux.
- **Out of scope:** RTOS and embedded targets; the C++ `cmpc` library.

## Consequences

- cerp and the webos C++ web UI write the same SQLite files; cerp must be a careful second writer (MPC001-162).
- webos authorization weaknesses (WOS-BUG-033 to WOS-BUG-035) must not be copied; cerp denies where webos would allow
  by accident.
- Protocol behaviour is largely the SDK's; an SDK coverage map records what the SDK provides and what cerp adds.
