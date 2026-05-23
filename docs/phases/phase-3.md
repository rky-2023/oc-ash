# Phase 3 — Event ingestion (fswatch + git hooks + Claude hooks + GitHub polling)

> **Goal:** Every interesting thing that happens under `/home/asher/` lands on the NATS bus as an `oc.event.*` message, gets signed and anchored in immudb by the audit pipeline already built in Phase 2, and becomes visible in the audit viewer.
> **Anchors:** ADR-001 D3 (NATS subject taxonomy), ADR-001 R4 (Tailscale-only edge), ADR-004 (defer-and-poll for GitHub, no public ingress yet), ADR-003 (audit pipeline catches all the messages).
> **Estimated wall-clock:** 4–5 working days.
> **Output:** `touch /home/asher/foo`, a `git commit` in any subrepo, a PR opened on GitHub, and a Claude Code tool call all appear in the audit viewer with the correct subject classification.
> **Parallel work eligible during this phase:** Phase 4 (MCP & A2A substrate) can start in parallel — it depends on Phase 2 spine, not on Phase 3 ingestion. Phase 8 (notifier bridge skeleton) can also start.

---

## Prerequisites (Phase 2 must be done)

- [ ] NATS JetStream is up with the 5 streams from Phase 2 task 2.4.
- [ ] immudb is up and the audit-appender is consuming `oc.*` subjects.
- [ ] The audit viewer is reachable on the tailnet and renders entries within 1 s.
- [ ] `tests/phase-2-smoke.sh` is green.
- [ ] `oc audit tail` works.
- [ ] You have a populated `openclaw.lookup` row for `github-app.openclaw-bot.installation_ids` (per ADR-002 D12) — Phase 2 task 2.12 set this up.
- [ ] The `openclaw-bot` GitHub App has `metadata:read`, `pull-requests:read`, `issues:read`, `actions:read`, `contents:read`, `checks:read` on every repo you want to monitor. (PR-write permissions come later in Phase 7.)

If any box is unchecked, fix it in Phase 2 before starting Phase 3.

---

## Architecture notes for this phase

All four ingesters share the same shape:

1. They run as their own services (their own AppRoles, their own mTLS identities, their own minimum Vault scope).
2. They never call core's business logic directly. They only **publish to NATS** with the right subject.
3. They never make a network decision themselves — what to deliver, who gets notified, whether to write a PR — all that lives downstream (Phase 4/7/8).
4. They are independently restartable; durable consumers on the bus mean a missed window in one ingester doesn't lose downstream consumers.

NATS subject taxonomy (final form):

| Subject pattern | Source | Example |
|---|---|---|
| `oc.event.fs.<repo-or-path>.<kind>` | fswatch | `oc.event.fs.ashboard-backend.modified` |
| `oc.event.git.<repo>.<hook-name>` | git hooks | `oc.event.git.openclaw.post-commit` |
| `oc.event.gh.<owner>.<repo>.<entity>` | GitHub poller | `oc.event.gh.rky-2023.oc-ash.pull_request.opened` |
| `oc.event.claude.<hook-name>` | Claude Code hooks | `oc.event.claude.PreToolUse` |

---

## Phase 3 task list

### 3.1 Write `ingest/fswatch/` (Rust, `notify` crate)

**Why:** inotify under the hood, but the Rust `notify` crate gives us cross-platform safety + good debouncing + low CPU overhead at scale. Watching `/home/asher/` recursively with naïve inotify can generate millions of events during a `npm install`; we need batched, filtered output.

**Steps:**
- `cargo new ingest/fswatch --name oc-fswatch`. Add deps: `notify = "6"`, `tokio = { features = ["full"] }`, `async-nats = "0.34"`, `serde_json`, `clap`, `tracing`, `tracing-subscriber`.
- Watch root: `/home/asher/` recursively, but apply an **allowlist negative** (exclusion list):
  - `.git/objects/**`, `.git/refs/**` (constant churn; we get the meaningful events from git hooks instead)
  - `node_modules/**`, `**/__pycache__/**`, `**/.next/cache/**`, `**/target/**`
  - `*.pyc`, `*.swp`, `*.swo`, `*~`
  - `/home/asher/openclaw/sessions/**` (this is *our* output; would create a feedback loop)
  - Add a config file `ingest/fswatch/exclude.toml` so this list is editable without recompile.
- Debounce: per-path, 1 s coalescing window. If the same file changes 50 times in 1 s, emit one event with `change_count: 50`.
- Event mapping:
  - `Create` → `oc.event.fs.<repo>.created`
  - `Modify` → `oc.event.fs.<repo>.modified`
  - `Remove` → `oc.event.fs.<repo>.deleted`
  - `Rename` → two events: `deleted` (old path) + `created` (new path) with a `rename_pair` correlation ID
- `<repo>` is computed as: the immediate child of `/home/asher/` containing the path (e.g., `Ashboard`, `ashboard-backend`, `openclaw`). For paths directly under `/home/asher/` (loose files), use `_root`.
- Payload schema:

  ```json
  {
    "path": "/home/asher/openclaw/PLAN.md",
    "repo": "openclaw",
    "kind": "modified",
    "size_bytes": 12847,
    "mtime": "2026-05-23T14:02:11.341Z",
    "change_count": 1
  }
  ```

- mTLS-authenticated NATS publish (Phase 2 set up per-service NATS creds).
- Dedicated `oc-fswatch` Linux user (uid > 10000); systemd unit with `ProtectSystem=strict`, `ReadOnlyPaths=/home/asher`, `NoNewPrivileges=yes`, `PrivateTmp=yes`.

**Verify:**
- `touch /home/asher/test.txt` → `oc.event.fs._root.created` arrives on NATS within 1.5 s; visible in the audit viewer within 2 s.
- `npm install` in a Node project → fswatch's CPU stays under 5 % and emits ≤ 20 events (not 20,000), thanks to the debounce + exclusion list.

---

### 3.2 Bootstrap script for git hooks across all `/home/asher/*` repos

**Why:** PLAN.md Phase 3 (and ADR-001) commits to git events being a distinct event source — *not* derived from fswatch — because fswatch sees `.git/refs/heads/main` change but doesn't know "was this a commit, a merge, a rebase, a checkout?" Git's own hooks know.

**Steps:**
- Shared hooks dir: `ingest/githooks/hooks/`. Subcommands:
  - `post-commit`, `post-merge`, `post-checkout`, `post-rewrite`, `pre-push`
- Each hook is a small shell script that:
  1. Detects the repo (`git rev-parse --show-toplevel`).
  2. Collects relevant context (commit SHA, ref name, author, message subject, etc.).
  3. Posts a JSON payload to a local Unix socket at `/run/openclaw/githooks.sock`.
  4. Fails non-blockingly — a slow or down core must not block the user's `git commit`.
- A bootstrap script `ingest/githooks/bootstrap.sh`:
  ```sh
  for dir in /home/asher/*/.git; do
    repo_root=$(dirname "$dir")
    git -C "$repo_root" config core.hooksPath /home/asher/openclaw/ingest/githooks/hooks
  done
  ```
  Idempotent; safe to re-run after adding new repos.
- A receiver service `ingest/githooks/receiver/` (small FastAPI app) listening on the Unix socket:
  - Validates the payload schema.
  - Looks up `<repo>` from the path.
  - Publishes `oc.event.git.<repo>.<hook-name>` to NATS.
- Runs as a dedicated `oc-githooks` user.

**Verify:**
- Run `ingest/githooks/bootstrap.sh`. Confirm `git config core.hooksPath` is set in every `.git/config` under `/home/asher/*`.
- `git commit` in a watched repo → `oc.event.git.<repo>.post-commit` lands on NATS with the commit SHA + author + message.
- `git checkout other-branch` → `oc.event.git.<repo>.post-checkout` lands.

---

### 3.3 Claude Code hook integration

**Why:** ADR-001 R4 + PLAN.md Phase 3 commits to Claude Code session events being ingested. These are the most useful events for "what is the agent doing right now?" telemetry.

Claude Code's hook system (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `Notification`, `SessionStart`) fires shell commands on each event. The pattern is: each hook is a tiny `curl` to a Unix socket.

**Steps:**
- Receiver service `ingest/claude-hooks/receiver/` listening on `/run/openclaw/claude-hooks.sock`. Mirrors the git-hooks receiver shape.
- Hook installer script that updates `~/.claude/settings.json` (use the `update-config` skill from the harness, or hand-edit). Example settings.json fragment:

  ```jsonc
  {
    "hooks": {
      "PreToolUse": [{"command": "curl --unix-socket /run/openclaw/claude-hooks.sock http://localhost/hook -d @-"}],
      "PostToolUse": [{"command": "curl --unix-socket /run/openclaw/claude-hooks.sock http://localhost/hook -d @-"}],
      "Stop": [{"command": "curl --unix-socket /run/openclaw/claude-hooks.sock http://localhost/hook -d @-"}],
      "SubagentStop": [{"command": "curl --unix-socket /run/openclaw/claude-hooks.sock http://localhost/hook -d @-"}],
      "SessionStart": [{"command": "curl --unix-socket /run/openclaw/claude-hooks.sock http://localhost/hook -d @-"}],
      "Notification": [{"command": "curl --unix-socket /run/openclaw/claude-hooks.sock http://localhost/hook -d @-"}]
    }
  }
  ```

- The receiver augments the hook payload with: `session_id`, `cwd`, current git HEAD, and tool args/results (the hook provides these on stdin).
- Publishes `oc.event.claude.<hook-name>` to NATS.
- Also writes a per-session manifest file at `/home/asher/openclaw/sessions/<date>/<session_id>.md` on `SessionStart`, appending on each subsequent event. (This bridges into Phase 12 session-scoping.)
- The receiver runs as a dedicated `oc-claude-hooks` user with write access to `/home/asher/openclaw/sessions/` only.

**Verify:**
- Open a Claude Code session in a project. `oc.event.claude.SessionStart` lands on NATS with the cwd and session_id.
- Run a tool call. `oc.event.claude.PreToolUse` + `PostToolUse` land paired by tool-invocation id.
- The manifest file appears under `sessions/` and grows correctly with each event.

---

### 3.4 GitHub events: polling worker (ADR-004 — webhook receiver deferred)

**Why:** ADR-004 locks defer-and-poll for now. The polling worker is the interim source for GitHub events; switched off when Cloudflare Tunnel + webhook receiver is wired up (some future Phase X).

**Steps:**
- New service `ingest/gh-poller/`, Python. Pinned deps include `httpx`, `nats-py`, `pydantic`, `tenacity` (for retries/backoff).
- Auth: uses `openclaw-bot` App credentials.
  - At start, mints an App JWT (10 min TTL) by asking Vault transit to sign with `transit/sign/github-app-openclaw-bot`.
  - Exchanges App JWT for an installation access token (1 h TTL) per repo's installation.
  - Re-mints periodically.
- Polling targets and cadence (per ADR-004 D1):

  | Endpoint | Cadence |
  |---|---|
  | `GET /repos/<owner>/<repo>/pulls?state=all&sort=updated&per_page=20` | 60 s |
  | `GET /repos/<owner>/<repo>/issues?state=all&sort=updated&per_page=20` | 60 s |
  | `GET /repos/<owner>/<repo>/commits?per_page=20` | 60 s |
  | `GET /repos/<owner>/<repo>/actions/runs?per_page=20` | 30 s |
  | `GET /repos/<owner>/<repo>/check-runs?per_page=20` | 30 s |
  | `GET /repos/<owner>/<repo>/releases?per_page=10` | 5 min |
- For each repo+endpoint, the worker maintains a checkpoint of `last_seen_id` (or `last_updated_at`) in `openclaw.lookup` under `gh-poller.checkpoints.<owner>.<repo>.<endpoint>`.
- Diff logic: on each poll, compare against checkpoint, emit one NATS event per new/changed entity. Subject: `oc.event.gh.<owner>.<repo>.<entity>.<state>` (e.g., `oc.event.gh.rky-2023.oc-ash.pull_request.opened`).
- Backoff:
  - On 429 or secondary rate limit: exponential backoff, max 10 min between polls.
  - On 5xx: exponential backoff, max 5 min.
  - Persistent failures (1 h without success on a single endpoint) → publish `oc.event.gh.health.failing` + log a critical message.
- Repos to watch: read from `openclaw.lookup` key `gh-poller.repos` (a JSONB array of `{owner, repo, installation_id}`). Add `rky-2023/oc-ash` and `rky-2023/openclaw-attestations` to start.

**Verify:**
- Open a PR on `oc-ash` from your laptop. Within 60 s, `oc.event.gh.rky-2023.oc-ash.pull_request.opened` lands on NATS.
- Push a commit to that PR. Within 60 s, `oc.event.gh.rky-2023.oc-ash.push` lands.
- Add a comment on the PR. Within 60 s, `oc.event.gh.rky-2023.oc-ash.issue_comment.created` lands.
- Disable network for 2 min, re-enable. Worker catches up without duplicate emissions (checkpoint discipline).

---

### 3.5 Ingest filter policy (`policy/ingest.rego`)

**Why:** Even with the fswatch exclusion list, we don't want every event to flood the audit log. Filter out the *most* boring events at the ingester (cheap), keep the ledger noise-free.

**Steps:**
- Define rules:
  - **fswatch:** drop events for paths matching `**/dist/**`, `**/build/**`, `**/coverage/**`, `**/*.log`. Keep everything else (the audit log is meant to be a record; err on inclusion).
  - **git:** keep all hook events. Git hooks fire rarely; no filtering needed.
  - **claude:** drop `Notification` events with empty bodies. Keep everything else.
  - **gh-poller:** drop events that are no-ops (state didn't actually change since checkpoint, only timestamps moved).
- Each ingester loads its bundle slice from OPA at startup, refreshes on signal.
- Add corresponding unit tests in `policy/tests/ingest_test.rego`.

**Verify:**
- `echo 'noise' >> /home/asher/openclaw/dist/build.log` → no NATS event.
- `echo 'real' >> /home/asher/openclaw/PLAN.md` → event lands as expected.

---

### 3.6 End-to-end smoke test

**Why:** Phase 3 is done when all four ingesters work concurrently and the audit viewer correctly classifies each event.

**Test script `tests/phase-3-smoke.sh`:**

| # | Action | Expected NATS subject(s) | Expected viewer classification |
|---|---|---|---|
| 1 | `touch /home/asher/openclaw/SMOKE.md` | `oc.event.fs.openclaw.created` | "fs" category, repo=openclaw |
| 2 | `git -C /home/asher/openclaw add SMOKE.md && git -C /home/asher/openclaw commit -m smoke` | `oc.event.fs.openclaw.modified` (for .git/index changes) AND `oc.event.git.openclaw.post-commit` | "git" + "fs" both shown |
| 3 | (Manual) Open and immediately close a PR on `oc-ash` | `oc.event.gh.rky-2023.oc-ash.pull_request.opened` then `.closed` within 60s each | "gh" category |
| 4 | (Manual) Open a Claude Code session in `/home/asher/openclaw/` and run any tool | `oc.event.claude.SessionStart`, then `oc.event.claude.PreToolUse`+`.PostToolUse` | "claude" category, session_id present |
| 5 | `rm /home/asher/openclaw/SMOKE.md && git -C /home/asher/openclaw commit -am cleanup` | fs deleted + git post-commit | both events visible |

All tests must pass (allowing for the manual nature of #3 and #4).

---

### 3.7 Document the operational paths

**Why:** When something goes wrong with an ingester at 2 AM, runbook entries matter.

**Add to `docs/RUNBOOK.md`:**

- **fswatch is missing events:** Check the exclusion list in `ingest/fswatch/exclude.toml`. Check inotify watch limit (`sysctl fs.inotify.max_user_watches`); raise to 524288 if needed.
- **git hooks not firing:** Check `core.hooksPath` is set correctly in the affected repo. Some commands (e.g., `git push` with `--no-verify`) skip hooks by design.
- **Claude hooks not firing:** Check `~/.claude/settings.json` hasn't been clobbered by a Claude Code update. Re-run the installer if needed.
- **gh-poller stuck on 429:** Look at the `oc.event.gh.health.failing` subject and the poller's logs. Lower the cadence for high-activity endpoints if needed (edit `openclaw.lookup` `gh-poller.cadences`).
- **Switching from poller to webhook (future):** When ADR-004's trigger fires, follow the procedure documented in `docs/phases/phase-X-public-ingress.md` (to be drafted in the future). The poller can keep running as a backup or be retired.

---

## Phase 3 exit criteria (all must be true)

- [ ] fswatch is running as `oc-fswatch`; CPU usage during a representative workload stays under 5 %.
- [ ] git hooks installed in every `.git/` under `/home/asher/*` and firing on commit/merge/checkout/rewrite/push.
- [ ] Claude Code hooks installed in `~/.claude/settings.json` and emitting on session lifecycle + tool calls.
- [ ] gh-poller polling at least `rky-2023/oc-ash` and `rky-2023/openclaw-attestations` at the cadences in 3.4.
- [ ] `policy/ingest.rego` loaded; noise filters working.
- [ ] All five tests in `tests/phase-3-smoke.sh` green.
- [ ] Audit viewer correctly classifies and renders all four ingester sources.
- [ ] `docs/RUNBOOK.md` has the operational entries from 3.7.

---

## Rollback / panic procedures

- **A noisy ingester is flooding the audit log:** `systemctl stop <ingester>`. The audit log keeps everything it already accepted; nothing rolls back, but new events stop arriving. Fix the filter, restart.
- **Git hooks blocking a `git commit`:** The hook should be non-blocking by design (`exit 0` after best-effort post). If it's somehow blocking, set `core.hooksPath=` (empty) in the affected repo temporarily to disable, fix the hook, restore.
- **Claude hooks slow down a session:** Hooks are synchronous in Claude Code. If a hook is consistently > 50 ms, slim it down or buffer to a local file and have a separate flusher post to NATS asynchronously.
- **gh-poller exhausted GitHub rate limit:** Pause the poller (`systemctl stop oc-gh-poller`). Wait for the rate window to reset (1 h primary, varies for secondary). Lower the cadences, restart.

---

## What goes into git, what doesn't

| Goes in git | Stays out of git |
|---|---|
| `ingest/*/` source code | NATS user nkey seeds |
| `policy/ingest.rego` + tests | Installation access tokens (short-lived; only in memory) |
| Bootstrap and installer scripts | Modified user `~/.claude/settings.json` (lives in user home, not project) |
| `tests/phase-3-smoke.sh` | Real GitHub App private key (in Vault transit) |
| systemd unit templates | Rendered systemd units with secret paths filled in |
| This runbook | The `openclaw.lookup` Postgres rows (in Postgres, not git) |

---

## What Phase 3 deliberately does *not* do

- **Public ingress for webhooks.** Deferred per ADR-004; polling worker takes its place.
- **PR creation (Phase 7).** Phase 3 only *reads* GitHub events; the openclaw-bot has read-only scope here.
- **Notification fan-out.** Events land on NATS; what to deliver where is Phase 8.
- **Cross-event correlation / "the agent did X because of repo Y" analysis.** That belongs in the A2A router (Phase 4) and the viewer's narrative tab (already built in Phase 2).
- **MCP-style remote access to filesystem.** That's the `fs-asher` MCP server in Phase 4 / 5+. Phase 3 only emits events; it doesn't serve data to agents.

---

## Change log

- **2026-05-23 (v1)** — Drafted after ADR-004 acceptance. Polling worker is the interim GH event source; webhook receiver design preserved on paper for a future phase.
