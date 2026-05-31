#!/usr/bin/env bash
# Phase 2 spine smoke test.
#
# Run AFTER oc-with-vault-creds.sh has exported credentials into the env,
# or invoke directly for infra-only checks (groups T1-T3 work without creds).
#
# Usage (full run with creds):
#   source core/scripts/oc-with-vault-creds.sh
#   bash tests/phase-2-smoke.sh
#
# Usage (infra only, no Vault needed):
#   bash tests/phase-2-smoke.sh --infra-only
#
# Exit code: 0 if all implemented tests pass, 1 otherwise.
# SKIP is not a failure — it means a required prerequisite is absent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── colour helpers ────────────────────────────────────────────────────
GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'
CYAN='\033[1;36m'; RESET='\033[0m'

pass()  { echo -e "${GREEN}  PASS${RESET}  $*"; PASSED=$((PASSED+1)); }
fail()  { echo -e "${RED}  FAIL${RESET}  $*"; FAILED=$((FAILED+1)); }
skip()  { echo -e "${YELLOW}  SKIP${RESET}  $*"; SKIPPED=$((SKIPPED+1)); }
hdr()   { echo -e "\n${CYAN}── $* ──${RESET}"; }

PASSED=0; FAILED=0; SKIPPED=0
INFRA_ONLY=false
[[ "${1:-}" == "--infra-only" ]] && INFRA_ONLY=true

YESTERDAY=$(date -u -d "yesterday" '+%Y-%m-%d' 2>/dev/null \
  || date -u -v -1d '+%Y-%m-%d')   # macOS fallback

# ── T1  Infrastructure containers ────────────────────────────────────
hdr "T1: Infrastructure container health"

for svc in oc-vault oc-immudb oc-nats; do
  status=$(docker inspect --format '{{.State.Status}}' "$svc" 2>/dev/null || echo "not_found")
  if [[ "$status" == "running" ]]; then
    pass "$svc is running"
  else
    fail "$svc — expected 'running', got '$status'"
  fi
done

# ── T2  Vault sealed/initialized ─────────────────────────────────────
hdr "T2: Vault status"

vault_json=$(docker exec oc-vault vault status -format=json 2>/dev/null || echo "{}")
initialized=$(echo "$vault_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('initialized','?'))" 2>/dev/null || echo "?")
sealed=$(echo "$vault_json" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('sealed','?'))" 2>/dev/null || echo "?")

if [[ "$initialized" == "True" ]]; then
  pass "Vault initialized"
else
  fail "Vault not initialized (initialized=$initialized)"
fi

if [[ "$sealed" == "False" ]]; then
  pass "Vault unsealed"
else
  fail "Vault is sealed — run infra/scripts/02-unseal-vault.sh"
fi

# ── T3  Postgres reachable ────────────────────────────────────────────
hdr "T3: Postgres connectivity"

if psql -U "$(whoami)" -d postgres -c "SELECT 1" -q --tuples-only 2>/dev/null | grep -q 1; then
  pass "Postgres reachable (peer auth)"
else
  fail "Postgres not reachable via peer auth"
fi

if psql -U "$(whoami)" -d postgres -c \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='openclaw'" \
  -q --tuples-only 2>/dev/null | grep -qE "[0-9]+"; then
  tbl_count=$(psql -U "$(whoami)" -d postgres -c \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='openclaw'" \
    -q --tuples-only 2>/dev/null | tr -d ' \n')
  if [[ "$tbl_count" -ge 5 ]]; then
    pass "openclaw schema has $tbl_count tables (audit projection present)"
  else
    fail "openclaw schema has only $tbl_count tables — expected ≥ 5 (audit projection missing?)"
  fi
else
  fail "Could not query information_schema for openclaw tables"
fi

# ── T4  NATS reachable ────────────────────────────────────────────────
hdr "T4: NATS JetStream health"

# Use docker exec so we don't need an external nats tool on the host.
nats_ok=$(docker exec oc-nats nats server check --server nats://localhost:4222 2>/dev/null \
  && echo ok || echo fail)
if [[ "$nats_ok" == "ok" ]]; then
  pass "NATS server health check OK"
else
  # Fallback: just verify the port is open from inside the container
  if docker exec oc-nats sh -c 'echo "" | nc -z -w2 localhost 4222' 2>/dev/null; then
    pass "NATS port 4222 open (nats CLI not available for full check)"
  else
    fail "NATS not reachable on port 4222"
  fi
fi

# ── T5  oc CLI + Postgres path ────────────────────────────────────────
hdr "T5: oc CLI — audit verify --fast"

if $INFRA_ONLY; then
  skip "infra-only mode — skipping oc CLI tests"
elif [[ -z "${OC_POSTGRES_DSN:-}" ]]; then
  skip "OC_POSTGRES_DSN not set — run via oc-with-vault-creds.sh"
else
  OC="$REPO_DIR/core/.venv/bin/oc"
  if [[ ! -x "$OC" ]]; then
    fail "oc not found at $OC — run: cd core && .venv/bin/pip install -e ."
  else
    # Exit 0 with "no envelopes" message is correct for an empty ledger
    if "$OC" audit verify --fast --date "$YESTERDAY" 2>&1 | grep -qE "Entries:|Status:"; then
      pass "oc audit verify --fast --date $YESTERDAY completed (0 entries expected)"
    else
      fail "oc audit verify --fast --date $YESTERDAY produced unexpected output"
    fi
  fi
fi

# ── T6  Attestation publish ───────────────────────────────────────────
hdr "T6: Attestation publisher"

if $INFRA_ONLY; then
  skip "infra-only mode — skipping attestation test"
elif [[ -z "${OC_POSTGRES_DSN:-}" ]]; then
  skip "OC_POSTGRES_DSN not set — run via oc-with-vault-creds.sh"
elif [[ -z "${OC_GITHUB_APP_ID:-}" ]]; then
  skip "OC_GITHUB_APP_ID not set — run via oc-with-vault-creds.sh"
else
  OC="$REPO_DIR/core/.venv/bin/oc"
  if [[ ! -x "$OC" ]]; then
    fail "oc not found at $OC"
  else
    if "$OC" attest publish --date "$YESTERDAY" --json 2>&1 | grep -qE "commit_sha|entry_count"; then
      pass "oc attest publish --date $YESTERDAY succeeded"
    else
      fail "oc attest publish --date $YESTERDAY failed"
    fi
  fi
fi

# ── T7  verify.py against existing attestation ───────────────────────
hdr "T7: Standalone Merkle verifier (verify.py)"

VERIFY_PY="$REPO_DIR/../openclaw-attestations/verify.py"
FIRST_ATTEST="$REPO_DIR/../openclaw-attestations/2026/05/25.json"

if [[ -f "$VERIFY_PY" && -f "$FIRST_ATTEST" ]]; then
  if python3 "$VERIFY_PY" "$FIRST_ATTEST" 2>&1 | grep -q "PASS"; then
    pass "verify.py PASS on 2026/05/25.json"
  else
    fail "verify.py did not pass on 2026/05/25.json"
  fi
else
  # Fetch from GitHub as a fallback
  if command -v gh >/dev/null 2>&1; then
    tmpdir=$(mktemp -d)
    gh api repos/rky-2023/openclaw-attestations/contents/2026/05/25.json \
      --jq '.content' | base64 -d > "$tmpdir/25.json" 2>/dev/null || true
    gh api repos/rky-2023/openclaw-attestations/contents/verify.py \
      --jq '.content' | base64 -d > "$tmpdir/verify.py" 2>/dev/null || true
    if [[ -f "$tmpdir/25.json" && -f "$tmpdir/verify.py" ]]; then
      if python3 "$tmpdir/verify.py" "$tmpdir/25.json" 2>&1 | grep -q "PASS"; then
        pass "verify.py PASS on 2026/05/25.json (fetched via gh api)"
      else
        fail "verify.py did not pass on 2026/05/25.json"
      fi
    else
      fail "Could not fetch attestation or verify.py from GitHub"
    fi
    rm -rf "$tmpdir"
  else
    skip "verify.py or attestation file not found locally and gh not available"
  fi
fi

# ── T8–T13  Full-pipeline tests (need a live core + creds) ───────────
hdr "T8–T13: Full pipeline (live core + audit pipeline)"

OC="$REPO_DIR/core/.venv/bin/oc"
PYBIN="$REPO_DIR/core/.venv/bin/python"
CORE_URL="${OC_CORE_URL:-https://${HOSTNAME}:8000}"
PGUSER="$(whoami)"
pg() { psql -U "$PGUSER" -d postgres -tA -c "$1" 2>/dev/null || true; }

# Shared preconditions for the live tests.
LIVE_OK=true
LIVE_MSG=""
if $INFRA_ONLY; then LIVE_OK=false; LIVE_MSG="infra-only mode"
elif [[ -z "${OC_POSTGRES_DSN:-}" ]]; then LIVE_OK=false; LIVE_MSG="OC_POSTGRES_DSN not set — run via oc-with-vault-creds.sh"
fi

# Is core actually serving? (self-signed → -k)
CORE_UP=false
if $LIVE_OK && curl -sk -o /dev/null --max-time 3 "$CORE_URL/health" 2>/dev/null; then
  CORE_UP=true
fi

# ── T8  HTTP request → request+response envelopes reach Postgres ─────
if ! $LIVE_OK; then
  skip "T8:  HTTP→pipeline ($LIVE_MSG)"
elif ! $CORE_UP; then
  skip "T8:  HTTP→pipeline (core not reachable at $CORE_URL — start via run-with-vault-creds.sh)"
else
  # The middleware stamps X-OC-Conv-Id on the response; both the request and
  # response envelopes carry that conv_id. Poll the projection for both.
  cid=$(curl -sk -D - -o /dev/null --max-time 5 "$CORE_URL/" \
        | tr -d '\r' | awk -F': ' 'tolower($1)=="x-oc-conv-id"{print $2}')
  if [[ -z "$cid" ]]; then
    fail "T8:  no X-OC-Conv-Id header on response (middleware not active?)"
  else
    n=0
    for _ in $(seq 1 40); do   # up to ~20s (projector polls every OC_PROJECTOR_POLL_SECONDS, default 5s)
      n=$(pg "SELECT count(*) FROM openclaw.audit_entries WHERE conv_id='$cid'")
      [[ "${n:-0}" -ge 2 ]] && break
      sleep 0.5
    done
    if [[ "${n:-0}" -ge 2 ]]; then
      pass "T8:  request+response envelopes for conv $cid projected to Postgres ($n rows)"
    else
      fail "T8:  expected ≥2 projected rows for conv $cid, got ${n:-0} (projector lag or pipeline break)"
    fi
  fi
fi

# ── T9  No duplicate ULIDs (appender restart / redelivery safety) ────
# NATS dedups by msg_id (=ULID) and immudb keys by ULID, so a kill-restart
# of the appender cannot create duplicates. (The physical kill-restart is an
# operator step — see docs/RUNBOOK.md; here we assert the invariant it relies on.)
if ! $LIVE_OK; then
  skip "T9:  no-duplicate invariant ($LIVE_MSG)"
else
  dups=$(pg "SELECT count(*) FROM (SELECT ulid FROM openclaw.audit_entries GROUP BY ulid HAVING count(*)>1) d")
  total=$(pg "SELECT count(*) FROM openclaw.audit_entries")
  if [[ "${dups:-0}" == "0" ]]; then
    pass "T9:  zero duplicate ULIDs across ${total:-0} projected entries (restart-safe invariant holds)"
  else
    fail "T9:  ${dups} duplicate ULID(s) in projection — appender/projector idempotency broken"
  fi
fi

# ── T10  oc audit projection rebuild — rows match immudb ─────────────
if ! $LIVE_OK; then
  skip "T10: projection rebuild ($LIVE_MSG)"
elif [[ ! -x "$OC" ]]; then
  skip "T10: projection rebuild (oc not built at $OC)"
elif [[ -z "${OC_IMMUDB_PASSWORD:-}" ]]; then
  skip "T10: projection rebuild (OC_IMMUDB_PASSWORD not set — run via oc-with-vault-creds.sh)"
else
  if "$OC" audit projection rebuild --yes 2>&1 | grep -q "rows match immudb"; then
    pass "T10: projection rebuild — Postgres row count matches immudb"
  else
    fail "T10: projection rebuild did not confirm row-count match (see output above)"
  fi
fi

# ── T11  Tamper a projection row → verify --fast detects it; rebuild heals ─
if ! $LIVE_OK; then
  skip "T11: projection-tamper detection ($LIVE_MSG)"
elif [[ ! -x "$OC" ]]; then
  skip "T11: projection-tamper detection (oc not built)"
else
  # Pick the newest entry and its UTC date; flip its sig_service_valid flag.
  victim=$(pg "SELECT ulid FROM openclaw.audit_entries ORDER BY ulid DESC LIMIT 1")
  vdate=$(pg "SELECT to_char(ts,'YYYY-MM-DD') FROM openclaw.audit_entries WHERE ulid='$victim'")
  if [[ -z "$victim" ]]; then
    skip "T11: projection-tamper detection (no entries to tamper — generate traffic first)"
  else
    pg "UPDATE openclaw.audit_entries SET sig_service_valid = NOT sig_service_valid WHERE ulid='$victim'" >/dev/null
    if "$OC" audit verify --fast --date "$vdate" 2>&1 | grep -q "❌ FAIL"; then
      tamper_detected=true
    else
      tamper_detected=false
    fi
    # Heal: rebuild from immudb (source of truth) restores the true flag.
    if [[ -n "${OC_IMMUDB_PASSWORD:-}" ]]; then
      "$OC" audit projection rebuild --yes >/dev/null 2>&1 || true
    else
      pg "UPDATE openclaw.audit_entries SET sig_service_valid = NOT sig_service_valid WHERE ulid='$victim'" >/dev/null
    fi
    if $tamper_detected; then
      pass "T11: tampered projection row detected by 'oc audit verify --fast' (then healed)"
    else
      fail "T11: tampered projection row was NOT detected by verify --fast"
    fi
  fi
fi

# ── T12  Tamper detection on a real signed envelope (sig invalidation) ─
# A physical immudb tamper is infeasible by design (WORM + Merkle). We instead
# prove the detection primitive: mutating a signed field flips sig_service to
# invalid. Needs Vault (entries are ed25519-transit signed).
if ! $LIVE_OK; then
  skip "T12: tamper-detection primitive ($LIVE_MSG)"
elif [[ -z "${OC_VAULT_TOKEN:-}" ]]; then
  skip "T12: tamper-detection primitive (OC_VAULT_TOKEN not set — needed to verify ed25519-transit sigs)"
else
  raw=$(pg "SELECT raw_envelope FROM openclaw.audit_entries ORDER BY ulid DESC LIMIT 1")
  if [[ -z "$raw" ]]; then
    skip "T12: tamper-detection primitive (no entries — generate traffic first)"
  else
    result=$(cd "$REPO_DIR/core" && printf '%s' "$raw" | "$PYBIN" - <<'PY' 2>/dev/null
import sys, asyncio
from app.audit.envelope import AuditEnvelope
from app.audit.signer import verify_envelope
env = AuditEnvelope.model_validate_json(sys.stdin.read().strip())
ok = asyncio.run(verify_envelope(env, "service"))
env.subject = env.subject + ".TAMPERED"
bad = asyncio.run(verify_envelope(env, "service"))
print("OK" if (ok and not bad) else "FAIL")
PY
)
    if [[ "$result" == "OK" ]]; then
      pass "T12: signature verifies on the real entry and FAILS after a 1-field tamper"
    else
      fail "T12: tamper-detection primitive returned '$result' (expected OK)"
    fi
  fi
fi

# ── T13  Audit-lag metric (deferred to observability phase) ──────────
# DEFERRED, not silently skipped: oc_audit_lag_seconds is not wired yet.
# Tracked for the observability/metrics work (see docs/phases/phase-2.md
# exit criteria). The immudb-disconnect NACK path itself is exercised by the
# appender on every reconnect (see appender.py _process NACK branch).
skip "T13: immudb-disconnect → oc_audit_lag_seconds (DEFERRED → observability phase; metric not yet wired)"

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}═══════════════════════════════════${RESET}"
echo -e "  PASSED:  ${GREEN}${PASSED}${RESET}"
echo -e "  FAILED:  ${RED}${FAILED}${RESET}"
echo -e "  SKIPPED: ${YELLOW}${SKIPPED}${RESET}"
echo -e "${CYAN}═══════════════════════════════════${RESET}"

if [[ $FAILED -gt 0 ]]; then
  echo -e "\n${RED}Smoke test FAILED — see FAIL lines above.${RESET}"
  exit 1
fi

echo -e "\n${GREEN}All implemented checks passed.${RESET}"
echo "T8–T12 need a live core (run-with-vault-creds.sh) + creds; they SKIP cleanly otherwise."
echo "T13 (audit-lag metric) is a tracked deferral to the observability phase."
exit 0
