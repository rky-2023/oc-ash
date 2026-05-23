# openclaw

Self-hosted personal agent fabric on `/home/asher`. Syncs Google Calendar + Gmail, pushes notifications to an Android app for repo/codebase activity, routes work through MCP servers and an A2A controller with fully-auditable context exchange, and auto-opens GitHub PRs for any codebase changes it makes.

**Status:** Phase 0 — scaffolding only.

## Where to start reading

1. [`PLAN.md`](./PLAN.md) — full phase-wise build plan.
2. [`docs/THREAT_MODEL.md`](./docs/THREAT_MODEL.md) — what we defend against, what we don't.
3. [`docs/ADR/`](./docs/ADR/) — architectural decisions of record.

## Layout

```
core/        FastAPI service (auth, MCP proxy, A2A router, audit appender)
mcp/         MCP servers, one per integration (calendar, gmail, github, fs, fcm)
agents/      A2A controllers and planner agents
edge/        Caddy, WireGuard, Cloudflare Tunnel
ingest/      inotify, git hooks, GitHub webhooks, Claude Code hooks
notifier/    Outbound channels (FCM first; email/Telegram later)
mobile/      Android React Native client
infra/       docker-compose, terraform, ansible
audit/       immudb client, log readers, replay tool, Next.js viewer
policy/      OPA rego bundles (default-deny)
docs/        Threat model, runbook, ADRs
sessions/    Per-session manifests written by Claude Code hooks
```

## Honest framing

This stack does **not** claim resistance to a targeted nation-state adversary on a commodity home server — no such claim is credible. It aims for a "raise compromise cost above well-funded criminal groups and most non-targeted state activity" posture. See `docs/THREAT_MODEL.md` for the exact threat tiers in and out of scope.

## License

TBD (will land in Phase 0 alongside ADR-001).
