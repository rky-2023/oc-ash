#!/usr/bin/env bash
# Phase 1 task 1.3 (part 2 of 2)
# ----------------------------------------------------------------------
# Interactively unseal Vault using 3 of the 5 Shamir shares.
#
# Run this AFTER ./02-init-vault.sh (one time), and again after EVERY
# Vault reboot (Vault always starts sealed by design — ADR-001 R1).
#
# Each share prompt does not echo to the screen.

set -euo pipefail

log()  { printf '\033[1;36m[oc-vault-unseal]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[oc-vault-unseal ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────
docker exec oc-vault true 2>/dev/null \
  || die "oc-vault container is not running. Run ./01-bring-up-vault.sh first."

STATUS_JSON=$(docker exec oc-vault vault status -format=json 2>/dev/null || true)

if ! echo "$STATUS_JSON" | grep -q '"initialized":true'; then
  die "Vault is not initialized yet. Run ./02-init-vault.sh first."
fi

if echo "$STATUS_JSON" | grep -q '"sealed":false'; then
  log "Vault is ALREADY UNSEALED. Nothing to do."
  docker exec oc-vault vault status
  exit 0
fi

# ── Collect 3 shares ──────────────────────────────────────────────
log "Vault is sealed. Provide any 3 of the 5 Shamir shares."
log "(Input is hidden — paste each share and press ENTER.)"
echo

for i in 1 2 3; do
  while true; do
    read -srp "  Share $i: " share
    echo
    if [[ -z "$share" ]]; then
      echo "  empty input — try again"
      continue
    fi
    # Pipe share to vault operator unseal via stdin to avoid putting it
    # in the process argv (visible in /proc).
    if echo "$share" | docker exec -i oc-vault vault operator unseal - >/dev/null; then
      log "  ✓ share $i accepted"
      break
    else
      echo "  ✗ share rejected; try again"
    fi
  done
done

# ── Final status ──────────────────────────────────────────────────
echo
log "Final Vault status:"
docker exec oc-vault vault status

if echo "$STATUS_JSON" | grep -q '"sealed":false'; then
  log "Vault is unsealed."
fi

log ""
log "Next (one-time, only after first init):"
log "  Phase 1 task 1.4 — enable audit logging"
log "  Phase 1 task 1.5 — enable secret engines (kv-v2, pki_root, pki_int, transit, approle)"
log "  Phase 1 task 1.6 — create admin AppRole + destroy root token"
log "  (PR #4 — script not yet written)"
