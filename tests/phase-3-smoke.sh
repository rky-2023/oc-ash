#!/usr/bin/env bash
# Phase 3 event-ingestion smoke test.
#
# Exercises the oc-ingest worker end-to-end: a synthetic Claude hook and a
# real git commit (in a throwaway repo) become signed oc.event.* envelopes,
# get anchored in immudb, and project to Postgres with their telemetry intact
# (validating the redaction tuning). fswatch (task 3.1) and gh-poller live
# polling are explicit, documented SKIPs.
#
# Run AFTER sourcing credentials, with core running + the worker enabled:
#   export OC_ENABLE_INGEST_WORKER=true OC_INGEST_SOCKET=/tmp/oc-ingest.sock
#   bash core/scripts/run-with-vault-creds.sh          # in another terminal
#   source core/scripts/oc-with-vault-creds.sh
#   bash tests/phase-3-smoke.sh
#
# Infra-only (no creds/worker): bash tests/phase-3-smoke.sh --infra-only
# Exit code: 0 if all implemented tests pass, 1 otherwise. SKIP is not failure.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'; CYAN='\033[1;36m'; RESET='\033[0m'
pass()  { echo -e "${GREEN}  PASS${RESET}  $*"; PASSED=$((PASSED+1)); }
fail()  { echo -e "${RED}  FAIL${RESET}  $*"; FAILED=$((FAILED+1)); }
skip()  { echo -e "${YELLOW}  SKIP${RESET}  $*"; SKIPPED=$((SKIPPED+1)); }
hdr()   { echo -e "\n${CYAN}── $* ──${RESET}"; }
PASSED=0; FAILED=0; SKIPPED=0

INFRA_ONLY=false
[[ "${1:-}" == "--infra-only" ]] && INFRA_ONLY=true

PGUSER_LOCAL="$(whoami)"
OC="$REPO_DIR/core/.venv/bin/oc"
SOCK="${OC_INGEST_SOCKET:-/run/openclaw/ingest.sock}"
pg() { psql -U "$PGUSER_LOCAL" -d postgres -tA -c "$1" 2>/dev/null || true; }

# ── T1  ingest worker socket ─────────────────────────────────────────
hdr "T1: oc-ingest worker socket"
WORKER_UP=false
if $INFRA_ONLY; then
  skip "infra-only mode — skipping worker checks"
elif [[ -S "$SOCK" ]]; then
  pass "worker socket present at $SOCK"
  WORKER_UP=true
else
  skip "no worker socket at $SOCK (start core with OC_ENABLE_INGEST_WORKER=true; set OC_INGEST_SOCKET)"
fi

# Live gating: need creds (for the rebuild) + a reachable worker.
LIVE_OK=true; LIVE_MSG=""
if $INFRA_ONLY; then LIVE_OK=false; LIVE_MSG="infra-only mode"
elif ! $WORKER_UP; then LIVE_OK=false; LIVE_MSG="worker socket absent"
elif [[ -z "${OC_POSTGRES_DSN:-}" ]]; then LIVE_OK=false; LIVE_MSG="OC_POSTGRES_DSN not set — source oc-with-vault-creds.sh"
elif [[ ! -x "$OC" ]]; then LIVE_OK=false; LIVE_MSG="oc CLI not found at $OC"
fi

CLAUDE_CONV="phase3-smoke-claude-$$"
GIT_REPO=""
GIT_SUBJECT=""
FS_BIN="$REPO_DIR/ingest/fswatch/target/release/oc-fswatch"
FS_SUBJECT=""

# ── Generate events (Claude hook + git commit + fswatch) ──────────────
if $LIVE_OK; then
  hdr "Generating events"
  # Claude hook → oc.event.claude.Notification (non-empty body so it's kept).
  printf '{"hook_event_name":"Notification","message":"phase3-smoke","session_id":"%s","cwd":"%s"}' \
    "$CLAUDE_CONV" "$REPO_DIR" \
    | curl -sS --max-time 5 --unix-socket "$SOCK" --data-binary @- http://localhost/hook >/dev/null 2>&1 \
    && echo "  posted claude hook (conv=$CLAUDE_CONV)" || echo "  (claude hook post failed)"

  # Git commit in a throwaway repo wired to the shared hooks.
  TMPREPO="$(mktemp -d /tmp/oc-smoke-git-XXXXXX)"
  GIT_REPO="$(basename "$TMPREPO")"
  GIT_SUBJECT="oc.event.git.${GIT_REPO}.post-commit"
  (
    git -C "$TMPREPO" init -q
    git -C "$TMPREPO" config user.email smoke@oc.local
    git -C "$TMPREPO" config user.name oc-smoke
    git -C "$TMPREPO" config core.hooksPath "$REPO_DIR/ingest/githooks/hooks"
    OC_INGEST_SOCKET="$SOCK" git -C "$TMPREPO" commit -q --allow-empty -m "phase3-smoke commit"
  ) && echo "  committed in throwaway repo ($GIT_REPO)" || echo "  (git commit failed)"

  # fswatch (task 3.1): run oc-fswatch over a throwaway root pointed at the
  # worker socket; create a file inside a "repo" subdir → oc.event.fs.<repo>.*.
  # Only if the release binary has been built (needs a C toolchain).
  if [[ -x "$FS_BIN" ]]; then
    FSROOT="$(mktemp -d /tmp/oc-smoke-fs-XXXXXX)"
    FS_REPO="fsmoke"
    mkdir -p "$FSROOT/$FS_REPO"
    OC_INGEST_SOCKET="$SOCK" OC_FSWATCH_ROOT="$FSROOT" \
      OC_FSWATCH_EXCLUDE="$REPO_DIR/ingest/fswatch/exclude.toml" \
      OC_FSWATCH_DEBOUNCE_MS=300 "$FS_BIN" >/dev/null 2>&1 &
    FS_PID=$!
    sleep 1                                  # let the watcher arm
    echo "smoke" > "$FSROOT/$FS_REPO/SMOKE.md"
    sleep 1                                  # > debounce → flush + POST
    kill "$FS_PID" 2>/dev/null; wait "$FS_PID" 2>/dev/null
    # File events map to created OR modified depending on coalescing; assert on the prefix.
    FS_SUBJECT="oc.event.fs.${FS_REPO}."
    echo "  generated fswatch event (root=$FSROOT repo=$FS_REPO)"
  else
    echo "  (oc-fswatch not built — skipping fswatch generation; build: cd ingest/fswatch && cargo build --release)"
  fi

  # Force projection deterministically (avoids the in-process projector's
  # --reload poll lag — see BOOTSTRAP_LESSONS §20).
  echo "  forcing projection rebuild…"
  "$OC" audit projection rebuild --yes >/dev/null 2>&1 || echo "  (projection rebuild failed)"
fi

# ── T2  Claude hook ingested ─────────────────────────────────────────
hdr "T2: Claude hook → oc.event.claude.*"
if ! $LIVE_OK; then
  skip "T2: $LIVE_MSG"
else
  n=$(pg "SELECT count(*) FROM openclaw.audit_entries WHERE conv_id='$CLAUDE_CONV' AND subject='oc.event.claude.Notification'")
  if [[ "$n" == "1" ]]; then
    pass "claude Notification projected (conv=$CLAUDE_CONV)"
  else
    fail "claude event not projected (got count=$n for conv=$CLAUDE_CONV)"
  fi
fi

# ── T3  git hook ingested ────────────────────────────────────────────
hdr "T3: git commit → oc.event.git.<repo>.post-commit"
if ! $LIVE_OK; then
  skip "T3: $LIVE_MSG"
else
  n=$(pg "SELECT count(*) FROM openclaw.audit_entries WHERE subject='$GIT_SUBJECT'")
  if [[ "$n" == "1" ]]; then
    pass "git post-commit projected ($GIT_SUBJECT)"
  else
    fail "git event not projected (got count=$n for $GIT_SUBJECT)"
  fi
fi

# ── T4  signatures + chain on an ingested event ──────────────────────
hdr "T4: ingested events are signed + chained"
if ! $LIVE_OK; then
  skip "T4: $LIVE_MSG"
else
  ok=$(pg "SELECT count(*) FROM openclaw.audit_entries WHERE conv_id='$CLAUDE_CONV' AND sig_service_valid AND sig_appender_valid AND chain_valid")
  if [[ "$ok" == "1" ]]; then
    pass "claude event has sig_service + sig_appender + chain all valid"
  else
    fail "claude event integrity flags not all valid (matched=$ok)"
  fi
fi

# ── T5  redaction keeps telemetry (validates the tuning) ─────────────
hdr "T5: redaction keeps ingest telemetry (not <redacted:secret>)"
if ! $LIVE_OK; then
  skip "T5: $LIVE_MSG"
else
  # session_id used to be dropped (matched 'session'); cwd used to be entropy-
  # dropped. Both must now survive on a claude telemetry subject.
  sid=$(pg "SELECT raw_envelope::jsonb->'redacted_payload'->>'session_id' FROM openclaw.audit_entries WHERE conv_id='$CLAUDE_CONV'")
  cwd=$(pg "SELECT raw_envelope::jsonb->'redacted_payload'->>'cwd' FROM openclaw.audit_entries WHERE conv_id='$CLAUDE_CONV'")
  if [[ "$sid" == "$CLAUDE_CONV" && "$cwd" == "$REPO_DIR" ]]; then
    pass "session_id + cwd preserved verbatim (redaction tuning works)"
  else
    fail "telemetry redacted (session_id='$sid' cwd='$cwd' — expected '$CLAUDE_CONV' / '$REPO_DIR')"
  fi
fi

# ── T6  fswatch (task 3.1) → oc.event.fs.<repo>.* ────────────────────
hdr "T6: fswatch → oc.event.fs.<repo>.*"
if ! $LIVE_OK; then
  skip "T6: $LIVE_MSG"
elif [[ ! -x "$FS_BIN" ]]; then
  skip "T6: oc-fswatch not built (cd ingest/fswatch && cargo build --release — needs a C toolchain)"
else
  n=$(pg "SELECT count(*) FROM openclaw.audit_entries WHERE subject LIKE '${FS_SUBJECT}%'")
  if [[ "${n:-0}" -ge 1 ]]; then
    pass "fswatch event projected (${FS_SUBJECT}* count=$n)"
  else
    fail "fswatch event not projected (got count=$n for ${FS_SUBJECT}*)"
  fi
fi

# ── T7  gh-poller (task 3.4) ─────────────────────────────────────────
hdr "T7: GitHub poller"
if [[ -z "${OC_GITHUB_APP_ID:-}" ]]; then
  skip "T7: gh-poller live check needs GitHub App creds + a manual PR on a watched repo (see phase-3 §3.4 verify)"
else
  skip "T7: gh-poller is running — open/close a PR on a watched repo and look for oc.event.gh.<owner>.<repo>.pull_request.* (manual)"
fi

# ── cleanup ──────────────────────────────────────────────────────────
[[ -n "${TMPREPO:-}" && -d "${TMPREPO:-}" ]] && rm -rf "$TMPREPO"
[[ -n "${FSROOT:-}" && -d "${FSROOT:-}" ]] && rm -rf "$FSROOT"

echo -e "\n═══════════════════════════════════"
echo -e "  PASSED:  $PASSED"
echo -e "  FAILED:  $FAILED"
echo -e "  SKIPPED: $SKIPPED"
echo -e "═══════════════════════════════════"
echo "T1–T6 exercise the live worker (Claude + git + fswatch ingesters → sign → immudb →"
echo "projection, with telemetry preserved). T6 runs when oc-fswatch is built; T7 (gh-poller"
echo "live) needs GitHub App creds + a manual PR and stays a documented SKIP."
[[ "$FAILED" -eq 0 ]]
