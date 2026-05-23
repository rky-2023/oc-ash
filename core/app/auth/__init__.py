"""WebAuthn relying-party endpoints + session management.

Phase 1 task 1.8 scope: registration + authentication via WebAuthn,
SQLite-backed credential store, process-local HS256 session tokens.

Out of scope (Phase 2 task 2.5+):
- mTLS on the core listener (currently HTTP-only on localhost or tailnet)
- Vault transit for session-token signing (currently a per-process random key)
- Refresh-token rotation (currently single 15-min access token, must re-auth)
- jti deny-list (current revocation = process restart wipes all sessions)
- Postgres migration of the credential store (currently SQLite at
  /var/lib/openclaw-core/auth.db)
"""
