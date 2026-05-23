# agents/

A2A controllers and planner agents.

Each agent is an HTTP service that:
- Publishes an `agent-card.json` (capability descriptor) at a well-known path.
- Speaks A2A (JSON-RPC: `task`, `artifact`, `message` primitives).
- Holds a `context_budget` per conversation (tokens + wall-clock + tool-call count). Exceeding it halts the conversation and emits an audit event.

**Planned agents:**
- `agents/planner/` — top-level task decomposer.
- `agents/calendar-bot/` — handles calendar-related intents.
- `agents/mail-bot/` — handles mail triage.
- `agents/repo-bot/` — handles "open a PR to do X" intents.

**Status:** scaffold (Phase 4 will populate).
