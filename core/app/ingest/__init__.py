"""oc-ingest — Phase 3 event ingestion worker.

A single in-process worker (MVP; standalone-service split is Phase 11 per
ADR-003 D3) that turns external activity into signed `oc.event.*` envelopes
on the NATS bus, where the Phase 2 audit pipeline (appender → redaction →
immudb → projector) anchors them.

Two sources share one publish path (`emit.emit_event`):
  - gh-poller  (poller.py)  — polls the GitHub App API per ADR-004 D1.
  - claude hooks (hooks.py) — a unix-socket receiver the Claude Code hook
    script POSTs to.

Every event passes the OPA ingest filter (`policy/ingest.rego`,
`data.openclaw.ingest.decision`) before publishing; kept events are built
into an AuditEnvelope, signed with sig_service, and published with the ULID
as the JetStream msg_id (dedupe).
"""
