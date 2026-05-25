"""`oc` command-line tool — Phase 2 task 2.11.

Subcommands:
  oc audit tail [--subject PATTERN]   live tail of audit envelopes on NATS
  oc audit replay <conv_id>            ladder-diagram view of one conversation
  oc audit verify [--date YYYY-MM-DD]  signature + chain validation

Connection env (same as core):
  OC_NATS_URL              default nats://nats:4222
  OC_IMMUDB_HOST           default immudb (use 127.0.0.1 when running on host)
  OC_IMMUDB_PORT           default 3322
  OC_IMMUDB_USER           projector (R-only) for read commands
  OC_IMMUDB_PASSWORD       from kv/openclaw/immudb/projector
  OC_IMMUDB_DATABASE       default openclaw_audit

Use the wrapper script `core/scripts/oc-with-vault-creds.sh` to fetch
the projector password from Vault and run the CLI in one shot.
"""
