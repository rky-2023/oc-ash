#!/usr/bin/env bash
# Phase 1 task 1.3 (part 1 of 2)
# ----------------------------------------------------------------------
# Initialize Vault with Shamir 3-of-5.
#
# WARNING: this is destructive if Vault already has data — running it
# against an already-initialized Vault returns an error from Vault
# itself (we check; safe).
#
# Vault prints 5 unseal keys + 1 root token to your terminal ONCE.
# Distribute them to the locations from phase-1.md and shred any local
# copy before pressing ENTER at the end.
#
# Anchors: ADR-001 R1, Phase 1 runbook task 1.3.

set -euo pipefail

log()  { printf '\033[1;36m[oc-vault-init]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-vault-init WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-vault-init ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────
docker exec oc-vault true 2>/dev/null \
  || die "oc-vault container is not running. Run ./01-bring-up-vault.sh first."

INIT_STATUS=$(docker exec oc-vault vault status -address=http://127.0.0.1:8200 \
  -format=json 2>/dev/null || true)

# Vault status -format=json is pretty-printed (space after colon). Match
# both compact and pretty forms so the safety check never silently misses.
if echo "$INIT_STATUS" | grep -Eq '"initialized"[[:space:]]*:[[:space:]]*true'; then
  die "Vault is ALREADY INITIALIZED. Aborting to protect existing data. If you really want to start over, you must wipe /mnt/openclaw/vault first."
fi

# ── Big-red-button confirmation ───────────────────────────────────
cat <<'EOF'

  =============================================================
  ABOUT TO INITIALIZE VAULT WITH SHAMIR 3-OF-5
  =============================================================

  Vault will print 5 unseal keys + 1 root token to your terminal.

  This is the ONLY TIME you will ever see these values.

  Have ready:
    - 5 places to write the unseal keys (see phase-1.md cost-free
      distribution table — wallet / drawer / family / password
      manager / obscure spot)
    - A way to securely delete this terminal's scrollback
      AFTER you've recorded the keys

  Press Ctrl+C now if you are not ready.

EOF
read -rp "Type the word READY to proceed: " confirm
[[ "$confirm" == "READY" ]] || die "Aborted by operator"

# ── Initialize ────────────────────────────────────────────────────
log ""
log "Running: vault operator init -key-shares=5 -key-threshold=3"
log ""
echo "════════════════════════════════════════════════════════════════"
echo "   COPY EACH OF THE 5 UNSEAL KEYS + THE ROOT TOKEN BELOW"
echo "════════════════════════════════════════════════════════════════"
echo

docker exec -e VAULT_ADDR=http://127.0.0.1:8200 oc-vault \
  vault operator init -key-shares=5 -key-threshold=3

echo
echo "════════════════════════════════════════════════════════════════"
echo "   DONE — VAULT IS INITIALIZED AND STILL SEALED"
echo "════════════════════════════════════════════════════════════════"
echo

cat <<'EOF'
NEXT STEPS (do these IMMEDIATELY):

  1. Copy each of the 5 unseal keys to its designated location per
     phase-1.md cost-free distribution table.
  2. Copy the Initial Root Token somewhere temporary — you need it
     for Phase 1 task 1.6 (creating the admin AppRole + destroying
     the root token).
  3. Clear your terminal scrollback:
       reset; clear
       history -c
       ( or close + reopen the terminal window )
  4. Run ./03-unseal-vault.sh to unseal Vault using any 3 of the 5
     keys.

LOSING ALL 5 KEYS = TOTAL LOCKOUT. THE WHOLE THREAT MODEL DEPENDS
ON YOU NOT BEING CARELESS HERE.

EOF
