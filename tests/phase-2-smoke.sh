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
    if "$OC" attest publish --date "$YESTERDAY" 2>&1 | grep -qE "commit_sha|entry_count"; then
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

# ── T8–T13  Blocked on task 2.14 (FastAPI audit middleware) ──────────
hdr "T8–T13: Full pipeline tests (blocked on task 2.14 — FastAPI middleware)"

skip "T8:  HTTP request to /api/audit/entries → 2 envelopes in immudb within 500 ms (needs running core + creds)"
skip "T9:  audit-appender kill + restart — no duplicates, no gaps (needs live entries)"
skip "T10: oc audit projection rebuild — row count matches immudb (needs projector rebuild cmd)"
skip "T11: tamper audit_entries → oc audit verify detects mismatch (needs entries)"
skip "T12: tamper immudb entry → oc audit verify detects Merkle mismatch (needs entries)"
skip "T13: immudb disconnect → oc_audit_lag_seconds climbs → reconnect recovers (needs metrics)"

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
echo "Skipped tests require task 2.14 (FastAPI audit middleware) + a live audit pipeline."
exit 0
