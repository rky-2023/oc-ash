#!/usr/bin/env bash
# Phase 2 task 2.4 — bring up NATS + declare streams from streams.yaml
# ----------------------------------------------------------------------
# Brings up the NATS container, then creates the five openclaw streams
# (OC_EVENT, OC_A2A, OC_MCP, OC_NOTIFY, OC_HEALTH) declared in
# infra/nats/streams.yaml.
#
# Idempotent: re-running updates existing streams (NATS `stream add`
# upserts when the spec matches).
#
# NATS authentication via JWT operator accounts is deferred to a
# follow-up — for now the container is internal-network-only and the
# bootstrap uses no-auth on the loopback inside the container.
#
# Prerequisites:
#   - dm-crypt-backed /mnt/openclaw/nats already mounted (from 00)

set -euo pipefail

log()  { printf '\033[1;36m[oc-nats]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-nats WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-nats ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
STREAMS_FILE="$INFRA_DIR/nats/streams.yaml"
[[ -f "$STREAMS_FILE" ]] || die "streams config not found: $STREAMS_FILE"
mountpoint -q /mnt/openclaw/nats || die "/mnt/openclaw/nats not mounted. Run 00 first."

# ── Chown bind target BEFORE container start (uid 1000 = nats user) ─
log "Setting /mnt/openclaw/nats ownership to 1000:1000"
chown -R 1000:1000 /mnt/openclaw/nats
chmod 700 /mnt/openclaw/nats

# ── Bring up NATS if not already running ────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -qx oc-nats; then
  log "Starting oc-nats..."
  docker compose --env-file "$INFRA_DIR/.env.openclaw" \
    -f "$INFRA_DIR/docker-compose.openclaw.yml" up -d nats
  sleep 3
fi

# Wait for NATS to be reachable via docker inspect (image is minimal — no wget/sh wrapper)
log "Waiting for oc-nats to be running + healthy..."
ready=false
for i in $(seq 1 30); do
  state=$(docker inspect oc-nats --format '{{.State.Status}}' 2>/dev/null || echo missing)
  [[ "$state" == "running" ]] || { sleep 1; continue; }
  health=$(docker inspect oc-nats --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
  if [[ "$health" == "healthy" || "$health" == "none" ]]; then
    ready=true; break
  fi
  sleep 1
done
$ready || die "NATS didn't reach healthy state in 30s — check 'docker logs oc-nats'."
log "NATS is ready."

# ── Parse streams.yaml and create each stream ───────────────────────
# We use python3 (always present on Ubuntu) to parse YAML — avoids
# needing yq installed. The output is a series of `nats stream add` args
# per stream that we then exec inside the container.

log "Parsing $STREAMS_FILE..."

python3 <<EOF
import json
import re
import subprocess
import sys

# Tiny YAML parser — only handles our specific shape (one top-level list
# of mappings under "streams:"). Avoids the PyYAML dependency.

text = open("$STREAMS_FILE").read()
streams = []
cur = None
for line in text.splitlines():
    s = line.rstrip("\n")
    if not s.strip() or s.lstrip().startswith("#"):
        continue
    if s.startswith("streams:"):
        continue
    m = re.match(r"^  - name:\s*(\S+)", s)
    if m:
        if cur:
            streams.append(cur)
        cur = {"name": m.group(1)}
        continue
    m = re.match(r"^    (\w+):\s*(.+?)\s*$", s)
    if m and cur is not None:
        k, v = m.group(1), m.group(2)
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1]
            cur[k] = [x.strip().strip('"') for x in inner.split(",") if x.strip()]
        else:
            v = v.strip().strip('"')
            try:
                cur[k] = int(v)
            except ValueError:
                cur[k] = v
if cur:
    streams.append(cur)

for st in streams:
    name = st["name"]
    subjects = ",".join(st.get("subjects", []))
    max_age = st.get("max_age", 0)
    dup = st.get("duplicate_window", 0)
    storage = st.get("storage", "file")
    retention = st.get("retention", "limits")
    descr = st.get("description", "")

    print(f"[oc-nats] Creating/updating stream {name}", flush=True)
    # The nats:2.10-alpine server image does NOT include the `nats` CLI tool.
    # We run nats-box (which has it) on the same docker network as oc-nats.
    cmd = [
        "docker", "run", "--rm", "--network=oc-internal",
        "natsio/nats-box",
        "nats", "-s", "nats://oc-nats:4222",
        "stream", "add", name,
        f"--subjects={subjects}",
        f"--storage={storage}",
        f"--retention={retention}",
        f"--max-age={max_age}s" if max_age else "--max-age=-1",
        f"--dupe-window={dup}s" if dup else "--dupe-window=0",
        f"--description={descr}",
        "--max-msgs=-1",
        "--max-bytes=-1",
        "--max-msg-size=-1",
        "--discard=old",
        "--replicas=1",
        "--max-consumers=-1",
        "--no-allow-rollup",
        "--no-deny-delete",
        "--no-deny-purge",
        "--defaults",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"[oc-nats]   ✓ {name}", flush=True)
    elif "already in use" in r.stderr or "already exists" in r.stderr or "stream name already" in r.stderr.lower():
        # Update existing stream to match
        update_cmd = list(cmd)
        # Replace "add" with "update" in the args
        idx = update_cmd.index("add")
        update_cmd[idx] = "update"
        r2 = subprocess.run(update_cmd, capture_output=True, text=True)
        if r2.returncode == 0:
            print(f"[oc-nats]   ✓ {name} (updated)", flush=True)
        else:
            print(f"[oc-nats]   ! {name}: update failed: {r2.stderr.strip()}", file=sys.stderr, flush=True)
    else:
        print(f"[oc-nats]   ! {name}: {r.stderr.strip()}", file=sys.stderr, flush=True)
EOF

log ""
log "Stream list:"
docker run --rm --network=oc-internal natsio/nats-box \
  nats -s nats://oc-nats:4222 stream list 2>&1 | head -20 || true

log ""
log "✓ Phase 2 task 2.4 complete."
log "  Streams: OC_EVENT, OC_A2A, OC_MCP, OC_NOTIFY, OC_HEALTH"
