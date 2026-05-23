"""Audit pipeline — Phase 2 task 2.5+.

Modules:
  envelope.py    Pydantic model for the audit envelope (ADR-003 D2 schema)
  signer.py      Sign envelopes (process-local HMAC for now; Vault transit later)
  middleware.py  FastAPI middleware that emits one envelope per request

NOT yet implemented in this MVP (subsequent PRs):
  appender.py    audit-appender service (NATS → immudb writer)
  projector.py   audit-projector service (immudb → Postgres cache)
  redaction.py   OPA pre-append redaction pipeline
"""
