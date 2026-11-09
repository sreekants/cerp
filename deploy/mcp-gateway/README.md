# MCP gateway (prototype servers)

The four read-only prototype servers from `workspace/prototype/` (im, pm, crm, cfm), served by one
process on one port. Copies of the server files live here because `workspace/` is not tracked.

| Path | Server |
|---|---|
| `/mcp/im` | incidents |
| `/mcp/pm` | projects |
| `/mcp/crm` | CRM |
| `/mcp/cfm` | cash flow |

- Listens on `127.0.0.1:7410` only. nginx (`location /mcp/`) adds TLS and the bearer-token check.
- Databases: `<name>.s3db` in `MCP_DB_DIR` (default `./db`, git-ignored), opened read-only.
- No per-user permissions: anyone with the token reads all four databases.

## Install on the web server

```
cd ~/projects/cerp && uv sync --frozen
mkdir -p deploy/mcp-gateway/db   # copy im/pm/crm/cfm .s3db here
sudo install -m 0644 deploy/mcp-gateway/mcp-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mcp-gateway
```
