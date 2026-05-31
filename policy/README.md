# policy/

[Open Policy Agent](https://www.openpolicyagent.org/) (OPA) rego bundles. Every policy is **default-deny**.

**Bundles:**
- `redaction.rego` ✅ — what fields are masked in the audit log (PII, secrets). Query `data.openclaw.redaction.decisions`. (Phase 2 task 2.7)
- `ingest.rego` ✅ — which fs/git/gh/claude events are kept vs. dropped (noise filtering). Query `data.openclaw.ingest.decision` → `"keep"|"drop"`. (Phase 3 task 3.5)
- `mcp.rego` *(planned)* — which agent may call which MCP tool with which args, when.
- `mcp/<name>.rego` *(planned)* — per-MCP-server overrides (e.g., calendar.rego, gmail.rego).
- `notify.rego` *(planned)* — `{event} → {deliver, channel, priority}` routing.
- `auth.rego` *(planned)* — which WebAuthn-bound identities may do what at the HTTP edge.

**Testing:** every rego bundle has a sibling `*_test.rego` and runs in CI via `opa test`. A bundle without tests fails CI.

**Hot-reload:** OPA pulls bundles from a signed S3-like endpoint every 60s; policy changes propagate without restarting core.
