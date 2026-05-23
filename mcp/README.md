# mcp/

One subdirectory per **MCP server**. Each MCP server is an isolated process bound to a Unix socket; only `openclaw-core`'s MCP proxy talks to it.

**Hard rules:**
1. MCP servers never speak directly to other MCP servers or to agents — always through the proxy.
2. Each MCP runs in its own gVisor (`runsc`) sandbox with seccomp + read-only root + no-new-privileges + dropped caps.
3. Each MCP fetches its own secrets at startup from Vault using a 15-minute JWT minted by core; no env-baked secrets.
4. Each MCP ships an `agent-card.json` describing its tools, scopes, and rate limits.

**Planned servers:**
- `mcp/google-calendar/` — Phase 5
- `mcp/gmail/` — Phase 6
- `mcp/github/` — Phase 7
- `mcp/fs-asher/` — read-mostly filesystem view of `/home/asher/`
- `mcp/notifier-fcm/` — push fan-out

**Adding a new MCP server** = drop a directory here, register the agent card, add a `policy/mcp/<name>.rego`, add a service to `infra/docker-compose.openclaw.yml`. Core picks it up.
