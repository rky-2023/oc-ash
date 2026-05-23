#!/usr/bin/env bash
# Phase 1 task 1.1 + Phase 2 task 2.2
# ----------------------------------------------------------------------
# Create dm-crypt-backed volumes for openclaw services.
#
# Creates 4 LUKS-on-file containers and mounts them at the paths the
# Phase 2 docker-compose expects:
#
#   /var/lib/openclaw/luks/vault.img    →  /var/lib/openclaw/vault    (8 GiB)
#   /var/lib/openclaw/luks/immudb.img   →  /var/lib/openclaw/immudb   (50 GiB)
#   /var/lib/openclaw/luks/nats.img     →  /var/lib/openclaw/nats     (20 GiB)
#   /var/lib/openclaw/luks/minio.img    →  /var/lib/openclaw/minio    (50 GiB)
#
# Idempotent: a volume that's already mounted is skipped.
# Single passphrase used for all 4 volumes during bootstrap. You can
# later change individual ones with `cryptsetup luksChangeKey` or add
# additional key slots with `luksAddKey`.
#
# Anchors: ADR-001 R1 (Shamir + dm-crypt for sensitive volumes),
#          Phase 1 runbook task 1.1, Phase 2 runbook task 2.2.
#
# Requires: root, cryptsetup, mkfs.ext4, fallocate.
# Tested on: Ubuntu 24.04+ / Debian 12+.

set -euo pipefail

# ───────────────────────────────────────────────────────────────────
# Config (override via env)
# ───────────────────────────────────────────────────────────────────
LUKS_DIR="${LUKS_DIR:-/var/lib/openclaw/luks}"
MOUNT_BASE="${MOUNT_BASE:-/var/lib/openclaw}"

# Volume name → size (GiB). Order matters for output readability.
declare -A SIZES=(
  [vault]=8
  [immudb]=50
  [nats]=20
  [minio]=50
)
ORDER=(vault immudb nats minio)

# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────
log()  { printf '\033[1;36m[oc-luks]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-luks WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-luks ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# Tmpfs-backed key file for cryptsetup. Lives in RAM only; shredded on
# script exit regardless of how we exit. cryptsetup's `--key-file -`
# (read from stdin) is finicky with piped input in some versions —
# using a real file path is more robust and never persists to disk.
KEY_TMP=""
cleanup_key_tmp() {
  if [[ -n "$KEY_TMP" && -e "$KEY_TMP" ]]; then
    shred -u "$KEY_TMP" 2>/dev/null || rm -f "$KEY_TMP"
  fi
}
trap cleanup_key_tmp EXIT INT TERM

require_root() {
  if [[ $EUID -ne 0 ]]; then
    die "Must run as root. Try: sudo $0"
  fi
}

require_tools() {
  local missing=()
  for t in cryptsetup mkfs.ext4 fallocate blockdev; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
  done
  if (( ${#missing[@]} )); then
    die "Missing required tools: ${missing[*]}. Install with: apt-get install cryptsetup e2fsprogs util-linux"
  fi
}

is_already_mounted() {
  local target="$1"
  mountpoint -q "$target" 2>/dev/null
}

# ───────────────────────────────────────────────────────────────────
# Volume creation
# ───────────────────────────────────────────────────────────────────
create_volume() {
  local name="$1"
  local size_gib="$2"
  local img="$LUKS_DIR/${name}.img"
  local mapper="oc-${name}-luks"
  local mount_point="$MOUNT_BASE/${name}"

  if is_already_mounted "$mount_point"; then
    log "  Skip ${name}: already mounted at ${mount_point}"
    return 0
  fi

  log "  Creating ${name} (${size_gib} GiB)..."

  # 1. Allocate sparse image file
  if [[ ! -f "$img" ]]; then
    fallocate -l "${size_gib}G" "$img"
    chmod 600 "$img"
  else
    log "    Image file ${img} already exists; reusing"
  fi

  # 2. luksFormat (idempotent: skip if already LUKS-formatted)
  if ! cryptsetup isLuks "$img" 2>/dev/null; then
    log "    Running cryptsetup luksFormat..."
    cryptsetup luksFormat \
      --type luks2 \
      --cipher aes-xts-plain64 \
      --key-size 512 \
      --hash sha512 \
      --pbkdf argon2id \
      --batch-mode \
      --key-file "$KEY_TMP" \
      "$img"
  else
    log "    Already luksFormat-ed; skipping"
  fi

  # 3. Open (only if not already open)
  if [[ ! -e "/dev/mapper/$mapper" ]]; then
    log "    Opening LUKS container as /dev/mapper/$mapper..."
    cryptsetup open --key-file "$KEY_TMP" "$img" "$mapper"
  else
    log "    /dev/mapper/$mapper already open; skipping"
  fi

  # 4. Format with ext4 (only if not already formatted)
  if ! blkid "/dev/mapper/$mapper" >/dev/null 2>&1; then
    log "    Formatting as ext4..."
    mkfs.ext4 -q -L "oc-$name" "/dev/mapper/$mapper"
  else
    log "    Filesystem already exists; skipping"
  fi

  # 5. Mount
  mkdir -p "$mount_point"
  mount "/dev/mapper/$mapper" "$mount_point"
  chmod 700 "$mount_point"
  log "  ✓ ${name} mounted at ${mount_point}"
}

# ───────────────────────────────────────────────────────────────────
# Systemd mount unit suggestions
# ───────────────────────────────────────────────────────────────────
print_systemd_suggestions() {
  log ""
  log "=========================================================================="
  log "PERSISTENCE: the volumes are NOT auto-mounted on reboot yet."
  log ""
  log "For boot-time prompt mounting, create systemd units at:"
  log "  /etc/systemd/system/var-lib-openclaw-{vault,immudb,nats,minio}.mount"
  log ""
  log "Example for the vault volume:"
  log ""
  cat <<'EOF'
# /etc/crypttab
oc-vault-luks   /var/lib/openclaw/luks/vault.img   none   luks,discard
# (prompts for the passphrase at boot)

# /etc/fstab
/dev/mapper/oc-vault-luks   /var/lib/openclaw/vault   ext4   defaults,noexec,nosuid,nodev   0   2
EOF
  log ""
  log "Repeat for immudb / nats / minio."
  log "Then run: systemctl daemon-reload"
  log "=========================================================================="
}

# ───────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────
main() {
  require_root
  require_tools

  log "openclaw LUKS volume setup"
  log "  LUKS images dir: $LUKS_DIR"
  log "  Mount base dir:  $MOUNT_BASE"
  log ""

  mkdir -p "$LUKS_DIR"
  chmod 700 "$LUKS_DIR"
  mkdir -p "$MOUNT_BASE"

  # Prompt for the shared bootstrap passphrase (one for all 4 volumes).
  # Store it in your password manager AFTER this run — see Phase 1 runbook.
  local passphrase passphrase_confirm
  if [[ -z "${LUKS_PASSPHRASE:-}" ]]; then
    echo
    read -srp "Bootstrap LUKS passphrase (will be used for all 4 volumes): " passphrase
    echo
    read -srp "Confirm passphrase: " passphrase_confirm
    echo
    if [[ "$passphrase" != "$passphrase_confirm" ]]; then
      die "Passphrases do not match"
    fi
    if (( ${#passphrase} < 20 )); then
      warn "Passphrase is shorter than 20 characters. STRONGLY recommend >= 32."
      read -rp "Continue anyway? [y/N] " yn
      [[ "$yn" =~ ^[Yy]$ ]] || die "Aborted by operator"
    fi
  else
    passphrase="$LUKS_PASSPHRASE"
  fi
  unset passphrase_confirm

  # Write the passphrase to a tmpfs file that cryptsetup will read from.
  # /dev/shm is RAM-only, so the bytes never touch disk.
  KEY_TMP=$(mktemp -p /dev/shm oc-luks-key.XXXXXX)
  chmod 600 "$KEY_TMP"
  # printf without newline — cryptsetup reads the whole file as the key,
  # so any trailing newline would change the key material.
  printf '%s' "$passphrase" > "$KEY_TMP"
  # Forget the variable copy immediately.
  unset passphrase
  unset LUKS_PASSPHRASE

  log ""
  log "Creating volumes..."
  for name in "${ORDER[@]}"; do
    create_volume "$name" "${SIZES[$name]}"
  done

  # KEY_TMP shredded by EXIT trap.

  log ""
  log "✓ All openclaw volumes created and mounted."
  log ""
  print_systemd_suggestions
  log ""
  log "Next: ./01-bring-up-vault.sh"
}

main "$@"
