#!/usr/bin/env bash
# Phase 1 task 1.8 — start core with Tailscale HTTPS
# ----------------------------------------------------------------------
# Runs uvicorn with the cert from setup-tailscale-tls.sh, binding to
# 0.0.0.0 so the tailnet can reach it, with RP_ID set to this server's
# tailnet hostname so WebAuthn ceremonies validate.
#
# Run from anywhere — script resolves its own paths.
# Re-run any time. Reload triggers on file changes (uvicorn --reload).

set -euo pipefail

# Resolve paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TLS_DIR="/var/lib/openclaw-core/tls"

# Resolve our tailnet hostname (matches what setup-tailscale-tls.sh used)
command -v tailscale >/dev/null || { echo "tailscale not in PATH" >&2; exit 1; }
HOSTNAME=$(tailscale status --json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
[[ -n "$HOSTNAME" ]] || { echo "Could not resolve tailnet hostname" >&2; exit 1; }

CERT="$TLS_DIR/$HOSTNAME.crt"
KEY="$TLS_DIR/$HOSTNAME.key"

[[ -f "$CERT" && -f "$KEY" ]] || {
  echo "[oc-tls-run] cert or key not found in $TLS_DIR." >&2
  echo "[oc-tls-run] Run: sudo ./scripts/setup-tailscale-tls.sh" >&2
  exit 1
}

# Activate venv if not already active
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if [[ -f "$CORE_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$CORE_DIR/.venv/bin/activate"
  else
    echo "[oc-tls-run] .venv not found at $CORE_DIR/.venv. Create with:" >&2
    echo "    python3.13 -m venv .venv && source .venv/bin/activate && pip install -e .[dev]" >&2
    exit 1
  fi
fi

# Set WebAuthn RP_ID + expected origins to match the tailnet hostname
export OC_WEBAUTHN_RP_ID="$HOSTNAME"
export OC_WEBAUTHN_EXPECTED_ORIGINS_CSV="https://$HOSTNAME:8000"

echo "[oc-tls-run] RP_ID:    $OC_WEBAUTHN_RP_ID"
echo "[oc-tls-run] Origin:   $OC_WEBAUTHN_EXPECTED_ORIGINS_CSV"
echo "[oc-tls-run] URL:      https://$HOSTNAME:8000"
echo "[oc-tls-run] Launching uvicorn (Ctrl+C to stop)..."
echo

cd "$CORE_DIR"
exec uvicorn app.main:app \
  --host 0.0.0.0 --port 8000 \
  --ssl-certfile="$CERT" \
  --ssl-keyfile="$KEY" \
  --reload
