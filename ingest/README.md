# ingest/

Adapters that turn external events into messages on the NATS `oc.event.*` subjects. Every interesting thing under `/home/asher/` flows through here.

**Subdirectories:**

- `fswatch/` — Rust binary using `notify` crate. Watches `/home/asher/` recursively. Emits `oc.event.fs.<repo>.<kind>`. Runs as a dedicated `oc-fswatch` user, read-only.
- `githooks/` — shared `core.hooksPath` directory. Installs via a bootstrap script that walks every repo under `/home/asher/*`. Hooks: `post-commit`, `post-merge`, `post-checkout`, `post-rewrite`, `pre-push`.
- `gh-webhook/` — FastAPI mini-app behind Caddy. HMAC-validated. IP-allowlisted to GitHub's published webhook ranges. Per-repo secret rotated weekly.
- `claude-hooks/` — settings.json hooks (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `Notification`) that `curl` core over a Unix socket. Payload includes `session_id`, `cwd`, tool name, args/results.

**Design rule:** ingesters never call core's business logic directly. They only publish to NATS. Core consumes.
