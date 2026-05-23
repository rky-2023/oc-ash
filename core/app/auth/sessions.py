"""Session-token issuance + verification.

Phase 1 task 1.8 uses a **process-local** HMAC-SHA256 signing key.
On process restart the key is regenerated, which invalidates every
prior session. Equivalent to a hard logout — acceptable v1 trade-off.

Phase 2 task 2.5 replaces this with `transit/sign/core-jwt` via
Vault, so signing keys persist across restarts and rotate monthly
per ADR-002 D14.
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from jose import jwt
from jose.exceptions import JWTError

# Process-lifetime key. NEVER persisted.
_SIGNING_KEY: bytes = secrets.token_bytes(32)
_ALG = "HS256"
_ISSUER = "openclaw-core"

# 5-minute access token TTL per ADR-002 D5.
_ACCESS_TTL_SECONDS = 300


@dataclass
class SessionClaims:
    sub: str          # user_id
    cid: str          # credential_id used to authenticate (hex)
    iat: int          # issued at (epoch seconds)
    exp: int          # expires at (epoch seconds)
    jti: str          # session id (for future revocation list)


def mint_access_token(user_id: str, credential_id_hex: str) -> tuple[str, SessionClaims]:
    """Issue a new short-lived access token."""
    now = int(time.time())
    claims = SessionClaims(
        sub=user_id,
        cid=credential_id_hex,
        iat=now,
        exp=now + _ACCESS_TTL_SECONDS,
        jti=secrets.token_hex(16),
    )
    token = jwt.encode(
        {
            "iss": _ISSUER,
            "sub": claims.sub,
            "cid": claims.cid,
            "iat": claims.iat,
            "exp": claims.exp,
            "jti": claims.jti,
        },
        _SIGNING_KEY,
        algorithm=_ALG,
    )
    return token, claims


def verify_access_token(token: str) -> SessionClaims | None:
    """Verify and return claims, or None if invalid/expired."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _SIGNING_KEY,
            algorithms=[_ALG],
            issuer=_ISSUER,
        )
        return SessionClaims(
            sub=payload["sub"],
            cid=payload["cid"],
            iat=payload["iat"],
            exp=payload["exp"],
            jti=payload["jti"],
        )
    except (JWTError, KeyError):
        return None
