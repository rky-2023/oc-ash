#!/usr/bin/env bash
# One-time migration script
# ----------------------------------------------------------------------
# Use only if your 00-create-luks-volumes.sh was run BEFORE the
# /mnt-paths fix landed (i.e., your volumes are mounted at
# /var/lib/openclaw/{vault,immudb,nats,minio}).
#
# Safe to run if the volumes contain NO DATA YET. If you've already
# brought up Vault and stored anything, STOP — this script unmounts
# the volumes; data persists in the LUKS images (no data loss), but
# any running container that's mid-write will get a hard error.
#
# What it does:
#   - For each of vault / immudb / nats / minio:
#       - Stops the corresponding docker container if running.
#       - Unmounts /var/lib/openclaw/<svc> if mounted.
#       - Creates /mnt/openclaw/<svc> (perms 700).
#       - Mounts the dm-crypt device at the new path.
#       - Removes the now-empty old mount-point directory.
#
# Run as root: sudo ./migrate-mount-paths-to-mnt.sh

set -euo pipefail

log()  { printf '\033[1;36m[oc-migrate]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-migrate WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-migrate ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Must run as root. Try: sudo $0"

OLD_BASE="/var/lib/openclaw"
NEW_BASE="/mnt/openclaw"
SVCS=(vault immudb nats minio)

# ── Confirmation ─────────────────────────────────────────────────
cat <<EOF

  =========================================================
  ABOUT TO MIGRATE OPENCLAW MOUNT POINTS
  =========================================================

  From: $OLD_BASE/{vault,immudb,nats,minio}
  To:   $NEW_BASE/{vault,immudb,nats,minio}

  This unmounts each volume from its OLD path and remounts it at
  the NEW path. The LUKS image files (the actual encrypted data)
  stay at /var/lib/openclaw/luks/.

  If you have any running openclaw containers, they will be stopped
  first.

  PRECONDITION: the dm-crypt mappings (/dev/mapper/oc-*-luks) must
  already be open. Run 00-create-luks-volumes.sh first if not.

EOF
read -rp "Type MIGRATE to proceed: " confirm
[[ "$confirm" == "MIGRATE" ]] || die "Aborted by operator"

# ── Stop any running openclaw containers ─────────────────────────
log "Stopping any running openclaw containers..."
for svc in "${SVCS[@]}"; do
  container="oc-${svc}"
  if docker ps -a --format '{{.Names}}' | grep -qx "$container"; then
    log "  Stopping $container..."
    docker stop "$container" 2>/dev/null || true
    docker rm "$container" 2>/dev/null || true
  fi
done

# ── For each volume: unmount old, mount new ──────────────────────
mkdir -p "$NEW_BASE"
chmod 755 "$NEW_BASE"

for svc in "${SVCS[@]}"; do
  old_path="$OLD_BASE/$svc"
  new_path="$NEW_BASE/$svc"
  mapper="oc-${svc}-luks"

  log "Migrating $svc..."

  # Unmount old path if mounted
  if mountpoint -q "$old_path" 2>/dev/null; then
    log "  Unmounting $old_path..."
    umount "$old_path" \
      || die "Failed to unmount $old_path. Is something still using it?"
  else
    log "  $old_path was not mounted (skipping unmount)"
  fi

  # Ensure dm-crypt mapping is still open
  if [[ ! -e "/dev/mapper/$mapper" ]]; then
    die "/dev/mapper/$mapper is not open. Re-run 00-create-luks-volumes.sh to open it, then retry."
  fi

  # Create new mount point
  mkdir -p "$new_path"
  chmod 700 "$new_path"

  # Mount at new path
  log "  Mounting /dev/mapper/$mapper at $new_path..."
  mount "/dev/mapper/$mapper" "$new_path"

  # Cleanup old empty directory
  if [[ -d "$old_path" && -z "$(ls -A "$old_path")" ]]; then
    rmdir "$old_path"
    log "  Removed empty $old_path"
  fi

  log "  ✓ $svc now at $new_path"
done

log ""
log "✓ Migration complete."
log ""
log "Verify:"
log "  mount | grep openclaw"
log ""
log "Next:"
log "  sudo ./01-bring-up-vault.sh"
log ""
log "Don't forget to update any /etc/crypttab / /etc/fstab entries you"
log "wrote earlier from the OLD paths to the NEW paths."
