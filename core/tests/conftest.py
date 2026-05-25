"""Shared pytest fixtures and test-environment defaults."""

import os

# Disable Vault transit signing for the entire test suite.
# Tests run without a live Vault; the HMAC fallback is used instead.
os.environ.setdefault("OC_VAULT_SIGNING_ENABLED", "false")
os.environ.setdefault("OC_ENABLE_AUDIT_APPENDER", "false")
