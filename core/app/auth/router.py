"""FastAPI routes for WebAuthn + session endpoints.

All endpoints under /auth.

Public endpoints (no session required):
  GET  /auth/health              — service liveness (no session)
  POST /auth/register/begin      — start WebAuthn registration
  POST /auth/register/complete   — finish registration, return tokens
  POST /auth/login/begin         — start WebAuthn authentication
  POST /auth/login/complete      — finish authentication, return tokens

Authenticated endpoints (Bearer token required):
  GET  /auth/me                  — current user + credentials
  POST /auth/logout              — revoke current session token (jti deny — Phase 2)

Endpoints accept and return JSON. Tokens are returned in the response
body (not Set-Cookie) for clarity in v1; cookie issuance happens in
Phase 2 task 2.5 alongside mTLS.
"""

from __future__ import annotations

import json
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.sessions import mint_access_token, verify_access_token
from app.auth.webauthn_handlers import (
    complete_authentication,
    complete_registration,
    list_credentials,
    start_authentication,
    start_registration,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ============================================================================
# Pydantic request/response models
# ============================================================================


class RegisterBeginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)


class RegisterBeginResponse(BaseModel):
    challenge_id: str
    options: dict   # PublicKeyCredentialCreationOptions JSON


class RegisterCompleteRequest(BaseModel):
    challenge_id: str
    credential: dict             # PublicKeyCredential.toJSON() shape
    nickname: str | None = None


class RegisterCompleteResponse(BaseModel):
    user_id: str
    access_token: str
    token_type: str = "Bearer"
    expires_in: int              # seconds


class LoginBeginRequest(BaseModel):
    username: str | None = None  # None → usernameless / resident-key flow


class LoginBeginResponse(BaseModel):
    challenge_id: str
    options: dict


class LoginCompleteRequest(BaseModel):
    challenge_id: str
    credential: dict


class LoginCompleteResponse(BaseModel):
    user_id: str
    username: str
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class MeResponse(BaseModel):
    user_id: str
    credential_used: str
    credentials: list[dict]


# ============================================================================
# Auth dependency: parse Bearer token
# ============================================================================


def require_session(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    claims = verify_access_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"user_id": claims.sub, "credential_id": claims.cid, "jti": claims.jti}


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/health")
async def auth_health() -> dict:
    return {"ok": True, "subsystem": "auth"}


@router.post("/register/begin", response_model=RegisterBeginResponse)
async def register_begin(body: RegisterBeginRequest) -> RegisterBeginResponse:
    start = start_registration(body.username, body.display_name)
    log.info("auth.register.begin", username=body.username, challenge_id=start.challenge_id)
    # webauthn lib's options_to_json returns a JSON string; parse for the response
    return RegisterBeginResponse(
        challenge_id=start.challenge_id,
        options=json.loads(start.options_json),
    )


@router.post("/register/complete", response_model=RegisterCompleteResponse)
async def register_complete(body: RegisterCompleteRequest) -> RegisterCompleteResponse:
    try:
        user_id = complete_registration(
            body.challenge_id, body.credential, nickname=body.nickname
        )
    except ValueError as e:
        log.warning("auth.register.complete.failed", reason=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("auth.register.complete.error", error=str(e))
        raise HTTPException(status_code=400, detail="Registration verification failed")

    # Issue a session immediately so the operator doesn't have to re-auth
    # right after enrolling a new device.
    raw_id_b64 = body.credential.get("rawId") or body.credential.get("id") or ""
    token, claims = mint_access_token(user_id, raw_id_b64)
    log.info("auth.register.complete.ok", user_id=user_id, jti=claims.jti)
    return RegisterCompleteResponse(
        user_id=user_id,
        access_token=token,
        expires_in=claims.exp - claims.iat,
    )


@router.post("/login/begin", response_model=LoginBeginResponse)
async def login_begin(body: LoginBeginRequest) -> LoginBeginResponse:
    start = start_authentication(body.username)
    log.info(
        "auth.login.begin",
        username=body.username or "<usernameless>",
        challenge_id=start.challenge_id,
    )
    return LoginBeginResponse(
        challenge_id=start.challenge_id,
        options=json.loads(start.options_json),
    )


@router.post("/login/complete", response_model=LoginCompleteResponse)
async def login_complete(body: LoginCompleteRequest) -> LoginCompleteResponse:
    try:
        result = complete_authentication(body.challenge_id, body.credential)
    except ValueError as e:
        log.warning("auth.login.complete.failed", reason=str(e))
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        log.exception("auth.login.complete.error", error=str(e))
        raise HTTPException(status_code=401, detail="Authentication verification failed")

    token, claims = mint_access_token(result.user_id, result.credential_id_hex)
    log.info("auth.login.complete.ok", user_id=result.user_id, jti=claims.jti)
    return LoginCompleteResponse(
        user_id=result.user_id,
        username=result.username,
        access_token=token,
        expires_in=claims.exp - claims.iat,
    )


@router.get("/me", response_model=MeResponse)
async def me(session=Depends(require_session)) -> MeResponse:
    creds = list_credentials(session["user_id"])
    return MeResponse(
        user_id=session["user_id"],
        credential_used=session["credential_id"],
        credentials=creds,
    )
