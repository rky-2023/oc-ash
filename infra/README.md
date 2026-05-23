# infra/

Infrastructure-as-code for the openclaw stack.

**Files (planned):**
- `docker-compose.openclaw.yml` — NATS, immudb, MinIO, Vault, core, edge, MCP servers (each in a gVisor sandbox), notifier, audit viewer.
- `terraform/` — optional Cloudflare Tunnel, B2 bucket for backups, DNS records.
- `ansible/` — bootstrap roles: LUKS volume layout, Tang server, Wazuh agent install, Falco rules, systemd units for `fswatch` and the git-hooks bootstrap.

**Reused from existing Ashboard stack:** Postgres, Redis, Mosquitto, Grafana, Prometheus (see `/home/asher/docker-compose.yml`).

**Network policy:** all internal services bind to the docker-internal network only. The only externally reachable services are `edge` (Caddy) and `cloudflared`.
