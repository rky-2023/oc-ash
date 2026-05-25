#!/usr/bin/env bash
# Apply the audit projection migration (Phase 2 task 2.9).
# Creates openclaw.audit_entries, audit_conversations,
# audit_policy_decisions, audit_checkpoint tables + indexes.
#
# Idempotent — all DDL uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
# Run AFTER 06-apply-postgres-schema.sh (which creates the openclaw
# schema and openclaw_app role).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
MIGRATION="$REPO_DIR/core/app/db/migrations/001_audit_projection.sql"

log() { printf '\033[1;36m[10-audit-schema]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[10-audit-schema ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -f "$MIGRATION" ]] || die "Migration file not found: $MIGRATION"

# ── Pick Postgres connection mode (mirrors 06-apply-postgres-schema.sh)
PG_MODE="${OC_PG_MODE:-auto}"
PG_CONTAINER="${OC_PG_CONTAINER:-}"

if [[ "$PG_MODE" == "auto" ]]; then
  DETECTED=$(docker ps --format '{{.Names}}' 2>/dev/null \
    | grep -Ei 'postgres|ashboard.?db|pg' | head -1 || true)
  if [[ -n "$DETECTED" ]]; then
    PG_MODE=docker; PG_CONTAINER="$DETECTED"
  else
    PG_MODE=system
  fi
fi

psql_super() {
  case "$PG_MODE" in
    docker) docker exec -i "$PG_CONTAINER" psql -U postgres ;;
    system) sudo -u postgres psql ;;
    *) die "Unknown PG_MODE: $PG_MODE" ;;
  esac
}

log "Detected PG_MODE=$PG_MODE${PG_CONTAINER:+, container=$PG_CONTAINER}"
log "Applying audit projection migration..."

psql_super <<SQL
\c openclaw
\i /dev/stdin
SQL
# Pass migration via stdin so the container doesn't need a mounted file
psql_super < "$MIGRATION" \
  | grep -vE '^(CREATE|INSERT|ALTER|GRANT|DO|SET|COMMENT)$' || true

log "Done."
log ""
log "Verify: connect to Postgres and run:"
log "  SELECT table_name FROM information_schema.tables"
log "  WHERE table_schema = 'openclaw' AND table_name LIKE 'audit_%';"
log "Expected: audit_entries, audit_conversations, audit_policy_decisions, audit_checkpoint"
