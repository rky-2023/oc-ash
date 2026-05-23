#!/usr/bin/env bash
# Phase 1 task 1.8 — Tailscale HTTPS for core (one-time setup)
# ----------------------------------------------------------------------
# Generates a Let's Encrypt cert via Tailscale's DNS-01 ACME for this
# server's tailnet hostname, places it at /var/lib/openclaw-core/tls/
# with mode 600 on the key. Re-run to renew (cert is good ~90 days).
#
# Prerequisites:
#   - Tailscale running and the device on a tailnet.
#   - HTTPS Certificates enabled on the tailnet in the admin console:
#     https://login.tailscale.com/admin/dns → HTTPS Certificates → Enable.
#
# Output:
#   /var/lib/openclaw-core/tls/<hostname>.crt   (mode 644)
#   /var/lib/openclaw-core/tls/<hostname>.key   (mode 600, owned by $SUDO_USER)

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo $0" >&2; exit 1; }
command -v tailscale >/dev/null || { echo "tailscale not in PATH" >&2; exit 1; }

# Resolve our tailnet hostname automatically.
HOSTNAME=$(tailscale status --json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
[[ -n "$HOSTNAME" ]] || { echo "Could not resolve tailnet hostname" >&2; exit 1; }

OWNER="${SUDO_USER:-$(stat -c %U /var/lib/openclaw-core 2>/dev/null || echo root)}"
TLS_DIR="/var/lib/openclaw-core/tls"

echo "[oc-tls-setup] tailnet hostname: $HOSTNAME"
echo "[oc-tls-setup] cert owner:       $OWNER"
echo "[oc-tls-setup] tls dir:          $TLS_DIR"

install -d -o "$OWNER" -g "$OWNER" -m 0700 "$TLS_DIR"

cd "$TLS_DIR"
echo "[oc-tls-setup] requesting cert via tailscale (may take 10–30s)..."
tailscale cert "$HOSTNAME"

chown "$OWNER:$OWNER" "$HOSTNAME.crt" "$HOSTNAME.key"
chmod 644 "$HOSTNAME.crt"
chmod 600 "$HOSTNAME.key"

echo "[oc-tls-setup] ✓ TLS cert + key placed at $TLS_DIR/"
ls -la "$TLS_DIR/"
echo
echo "Next:"
echo "  cd /home/asher/openclaw/core"
echo "  ./scripts/run-tls.sh"
