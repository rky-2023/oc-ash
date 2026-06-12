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

### 3.1 Write `ingest/fswatch/` (Rust, `notify` crate)  ✅ implemented 2026-06-04

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

### 3.2 Bootstrap script for git hooks across all `/home/asher/*` repos  ✅ implemented 2026-06-01

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

### 3.3 Claude Code hook integration  ✅ implemented 2026-06-01

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

### 3.4 GitHub events: polling worker (ADR-004 — webhook receiver deferred)  ✅ implemented 2026-06-01

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

### 3.5 Ingest filter policy (`policy/ingest.rego`)  ✅ implemented 2026-06-01

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

### 3.6 End-to-end smoke test  ✅ implemented 2026-06-01

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

- [x] fswatch (`oc-fswatch`, Rust) built + producer verified end-to-end (watch→debounce→exclude→POST). *Running as the dedicated `oc-fswatch` user via systemd is operator/sudo-gated (unit shipped in `ingest/fswatch/oc-fswatch.service`).*
- [x] git hooks installed in every `.git/` under `/home/asher/*` (AI, ashboard-backend, openclaw, Ashboard) — `core.hooksPath` → shared `ingest/githooks/hooks`. *Live firing through the worker is operator-gated (smoke T3).* Ashboard's pre-existing `Co-Authored-By: Claude` strip was preserved by porting it into the shared `commit-msg` hook (so the rule now applies repo-wide).
- [x] Claude Code hooks installed in `~/.claude/settings.json` (all 7 lifecycle events; existing non-oc hooks preserved, `.bak` saved). *Emitting through the worker is operator-gated (smoke T2).*
- [x] gh-poller configured to poll `rky-2023/oc-ash` + `rky-2023/openclaw-attestations` (the built-in default in `poller._repos()`; installation token from Vault-backed settings). *Live polling needs the worker + creds (smoke T7).*
- [x] `policy/ingest.rego` loaded; noise filters working (merged PR #42, `opa test` green).
- [ ] All seven checks in `tests/phase-3-smoke.sh` green — **operator-gated:** `--infra-only` is clean (0 fail, 7 SKIP); the live T1–T7 need Vault creds (gpg-passphrase-gated `run-with-vault-creds.sh`) + the worker enabled.
- [ ] Audit viewer correctly classifies and renders all four ingester sources — verified during the live smoke run above.
- [x] `docs/RUNBOOK.md` has the operational entries from 3.7.

### Live verification (operator-gated) — exact procedure

The two remaining `[ ]` boxes need Vault creds (the gpg passphrase, denied to the
agent) + the in-process ingest worker. This is a **two-terminal** flow; paths are
relative to the repo root `/home/asher/openclaw`. Source of truth: the header of
`tests/phase-3-smoke.sh`.

**Prerequisite — infra must be healthy.** After any host reboot Vault auto-seals
and the stateful containers can crash-loop on lost volume ownership
(`BOOTSTRAP_LESSONS.md §2`; recovery in `RUNBOOK.md → "Unseal Vault after a host
reboot"`). Before the smoke, confirm: `docker ps` shows `oc-vault`, `oc-immudb`,
`oc-nats`, `oc-opa` healthy, and `docker exec oc-vault vault status` →
`Sealed: false`. If vault/immudb are `Restarting`, fix volume ownership + unseal
first — the live smoke can't pass until they're up.

**Terminal A — start core + the ingest worker** (prompts once for the gpg passphrase):

```sh
cd /home/asher/openclaw
export OC_ENABLE_INGEST_WORKER=true OC_INGEST_SOCKET=/tmp/oc-ingest.sock
bash core/scripts/run-with-vault-creds.sh    # leave running; do NOT pipe through tail/grep
```

**Terminal B — source creds into the shell, then run the smoke:**

```sh
cd /home/asher/openclaw
export OC_ENABLE_INGEST_WORKER=true OC_INGEST_SOCKET=/tmp/oc-ingest.sock
source core/scripts/oc-with-vault-creds.sh   # gpg-agent usually has the passphrase cached
bash tests/phase-3-smoke.sh
```

Pass condition: **exit 0 with 0 FAIL** (fswatch + gh-poller live polling are documented
SKIPs, not failures). Then eyeball the audit viewer for all four sources and flip the two
boxes above. No-creds sanity check: `bash tests/phase-3-smoke.sh --infra-only` → 0 fail, 7 SKIP.

> Note the full paths: `core/scripts/run-with-vault-creds.sh` and
> `core/scripts/oc-with-vault-creds.sh` (not `./scripts/…`, which only resolves
> after `cd core`).

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
- **2026-06-01 (v1.1)** — Phase 3 kicked off (after Phase 2 complete + PR #40/#41 merged). **Task 3.5 ingest filter policy** implemented: `policy/ingest.rego` (`data.openclaw.ingest.decision` → keep/drop; fswatch dist/build/coverage/*.log noise drop, claude empty-Notification drop, gh-poller no-op drop, default-keep elsewhere) + `policy/ingest_test.rego` (16 tests; `opa test` 27/27 incl. redaction). Tests use the flat `policy/<name>_test.rego` convention (per `policy/README.md`), not the `policy/tests/` path the task draft mentioned. **Blocker noted:** task 3.1 fswatch needs a Rust toolchain (cargo/rustc not installed) + a dedicated `oc-fswatch` user/systemd unit (sudo) — operator-gated.
- **2026-06-01 (v1.2)** — **Tasks 3.3 (Claude hooks) + 3.4 (gh-poller)** implemented as ONE in-process `oc-ingest` worker (`core/app/ingest/`), per the chosen "shared ingest service + thin hook script" architecture. Both sources funnel through `emit.emit_event` (OPA ingest-filter → build AuditEnvelope → sign sig_service → JetStream publish with ULID msg_id), so ingested payloads get the full Phase 2 redaction + anchoring. gh-poller: pure `diff()` engine (baseline-on-first-poll, change detection, bounded seen-map), endpoint specs for pulls/issues/commits/runs/releases, exp-backoff (429→10m, 5xx→5m), 1h-stall → `oc.event.gh.health.failing`, checkpoints in `openclaw.lookup`. Claude hooks: unix-socket receiver (`/run/openclaw/ingest.sock`) + thin `ingest/claude-hooks/oc-claude-hook.sh` + dry-run-by-default installer + per-session manifest under `sessions/`. Gated by `OC_ENABLE_INGEST_WORKER` (default off); wired into `app.main` lifespan. **Deviations (documented):** worker is in-core MVP reusing audit primitives (standalone-service split → Phase 11, ADR-003 D3); GitHub App JWT via KV-PEM (not Vault transit). Unit: 18 new tests, full core suite 76 passed. Live (signing+NATS+GitHub App) operator-gated. RUNBOOK updated for gh-poller/claude-hooks (partial 3.7).
- **2026-06-01 (v1.3)** — **Task 3.2 git hooks** implemented, routing through the SAME oc-ingest worker/socket as Claude hooks (the draft's separate `githooks.sock`/receiver is superseded by the unified worker). `hooks.py` now dispatches socket payloads by `source` (`git`|`claude`); pure `build_git_event` → `oc.event.git.<repo>.<hook>`. Hooks `ingest/githooks/hooks/{post-commit,post-merge,post-checkout,post-rewrite,pre-push}` exec a shared `_oc-githook` forwarder (collects repo/sha/ref/author/subject via git; JSON built with python3 for safe escaping; 3s curl timeout; always exits 0). `ingest/githooks/bootstrap.sh` sets `core.hooksPath` across `/home/asher/*/.git` (idempotent; CAVEAT documented: replaces a repo's own hooks; commits incur the 3s timeout when the worker is down). 4 new tests; full core suite 80 passed. Stacked on the PR #43 worker branch. Operator-gated: run bootstrap.sh + enable the worker. (Merged to main: #43 worker `a00a5ab`, #45 git hooks `b7a6ef9`; #44 auto-closed when its stacked base was deleted on #43 merge → recreated as #45.)
- **2026-06-01 (v1.4)** — **Redaction tuning** (fixes live finding: ingest telemetry was being gutted). The Phase 2 redaction pipeline over-redacted Phase 3 events — claude `cwd`/`git_head`/`session_id` and git `sha`/`ref`/`author`/`subject` all came back `<redacted:secret>`. Two fixes: (1) `policy/redaction.rego` — removed bare `session` from `secret_key_pattern` so `session_id` is kept (`session_token`/`session_secret` still dropped via `token`/`secret`); (2) `core/app/audit/redaction.py` — the Shannon-entropy drop net now skips operational-telemetry subjects (`oc.event.{git,claude,fs}.*`), where high-entropy values (SHAs, refs, paths) are legitimate metadata; the name-based secret regex still applies there as defense-in-depth. Added `sessions/` to `.gitignore`. Tests: `opa test` 30/30, pytest 83 passed (3 rego + 3 python added).
- **2026-06-04 (v1.6)** — **Task 3.1 fswatch** built. `ingest/fswatch/` is a thin Rust producer (`notify` 6, no async runtime — std mpsc + a debounce-flush loop): watches `OC_FSWATCH_ROOT` (default `/home/asher`) recursively, drops noise via `exclude.toml` globs (a `**/<dir>/**` entry also silences the bare directory event), coalesces per-path bursts inside `OC_FSWATCH_DEBOUNCE_MS` (default 1 s) into one event carrying `change_count`, maps Create/Modify/Remove → created/modified/deleted (+ rename → deleted/created pair sharing a `rename_pair`), and **POSTs `{"source":"fswatch", …}` to the oc-ingest unix socket** — NOT direct NATS (deviation from the v1 draft, consistent with the unified-worker architecture; standalone split → Phase 11). Worker side: `hooks.py` gained `build_fs_event` + a `source=="fswatch"` dispatch → `oc.event.fs.<repo>.<kind>`; `<repo>` = immediate child of the root (loose files → `_root`). `policy/ingest.rego` already filters fswatch noise and `oc.event.fs.*` is in the redaction telemetry-skip list (#46), so no policy change was needed. Smoke T6 promoted from a deferral SKIP to a real live check (runs `oc-fswatch` over a throwaway root → asserts `oc.event.fs.fsmoke.*` projects; SKIPs cleanly if the binary isn't built). Tests: 4 Rust unit tests (repo mapping, exclude-glob matching, bare-dir expansion) + 4 Python tests; core suite 87 passed; a standalone producer smoke (fake socket) confirmed coalescing (6 writes→1 event change_count=6), exclusion, and created/modified/deleted mapping. **Build prerequisite:** linking needs a C toolchain (`gcc`/`cc`) — `cargo build`/`check`/`test` all fail at the link step without one; `gcc` was installed on the host to build it. Running as the `oc-fswatch` systemd user remains operator/sudo-gated (unit + README shipped). 3.7 RUNBOOK fswatch entry added.
- **2026-06-01 (v1.5)** — **Task 3.6 end-to-end smoke** (`tests/phase-3-smoke.sh`), modeled on `phase-2-smoke.sh`. T1 worker socket; T2 Claude hook → `oc.event.claude.Notification`; T3 real git commit in a throwaway `/tmp` repo (wired to the shared hooks) → `oc.event.git.<repo>.post-commit`; T4 both sigs + chain valid; T5 redaction keeps telemetry (asserts `session_id`/`cwd` survive — validates v1.4). Projection is forced via `oc audit projection rebuild` after generating events, so it's deterministic despite the in-process projector's `--reload` lag (§20). T6 fswatch + T7 gh-poller-live are explicit tracked-deferral SKIPs. `--infra-only` runs cleanly with no creds (all SKIP, exit 0); full run needs the sourced creds + the worker enabled (operator-gated). Live T1–T5 were verified manually during the #43/#45 live-check; the script automates that path.
- **2026-06-13 (v1.7)** — **Host install of the ingesters (closing the un-gated Phase 3 exit criteria).** (1) **git hooks** — `core.hooksPath` → shared `ingest/githooks/hooks` set on all four `/home/asher/*` repos. Ashboard already had a `commit-msg` hook stripping `Co-Authored-By: Claude` (enforces the user's no-trailers preference); rather than clobber it, the strip was **ported into a new shared `ingest/githooks/hooks/commit-msg`** so the rule now applies repo-wide *and* Ashboard gains the telemetry hooks. Verified the shared hook strips the trailer. (2) **Claude hooks** — `install-claude-hooks.py --apply` merged the 7 lifecycle hooks into `~/.claude/settings.json` (existing `PostToolUse` hook preserved; timestamped `.bak` written). (3) **gh-poller** — confirmed `poller._repos()` already defaults to `oc-ash` + `openclaw-attestations`, so no DB seeding needed (the v1 draft's `gh-poller.repos` lookup row is superseded by env-config + default). (4) fswatch binary confirmed built (`target/release/oc-fswatch`); `ingest.rego` confirmed merged; RUNBOOK confirmed complete. **Remaining to fully close Phase 3 = one operator-gated live run:** unlock Vault creds (`run-with-vault-creds.sh`, gpg-passphrase) + enable the worker (`OC_ENABLE_INGEST_WORKER=true`), then `tests/phase-3-smoke.sh` (live T1–T7) + eyeball the audit viewer for all four sources. The gpg cache was cold this session and the decrypt is denied to the agent, so the live leg is the user's to run. Optional sudo-gated hardening (the `oc-fswatch` systemd user) stays deferred per ADR-003 D3.
