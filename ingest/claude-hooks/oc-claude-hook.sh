#!/usr/bin/env bash
# Thin Claude Code hook forwarder → oc-ingest unix socket (Phase 3 task 3.3).
#
# Claude Code pipes the hook event JSON on stdin. We POST it to the local
# oc-ingest worker, which classifies + filters + signs + publishes it.
#
# Design rule: a hook must NEVER block or fail Claude Code. We use a short
# timeout and always exit 0, even if the worker is down.
SOCK="${OC_INGEST_SOCKET:-/run/openclaw/ingest.sock}"
curl -sS --max-time 3 --unix-socket "$SOCK" \
  -H 'Content-Type: application/json' \
  --data-binary @- \
  http://localhost/hook >/dev/null 2>&1 || true
exit 0
