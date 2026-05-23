"""WebAuthn registration + authentication logic.

The endpoints in router.py call into these helpers. The split keeps
HTTP details (request parsing, status codes, cookies) separate from
the protocol logic (challenges, verification, persistence).

Anchors: ADR-002 D1 (resident keys + UV=required + direct attestation),
         ADR-002 D13 (interim platform-authenticator mode).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
from dataclasses import dataclass

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.auth.db import get_conn, sweep_expired_challenges
from app.config import settings

CHALLENGE_TTL_SECONDS = 300  # 5 minutes


# ============================================================================
# Registration
# ============================================================================


@dataclass
class RegistrationStart:
    """Returned to the browser to drive navigator.credentials.create()."""

    challenge_id: str
    options_json: str        # webauthn-lib's options_to_json output


def start_registration(username: str, display_name: str) -> RegistrationStart:
    """Begin a registration ceremony. Creates the user if not exists, stores
    the challenge keyed by a fresh challenge_id, returns the options JSON
    for the browser."""

    sweep_expired_challenges()

    with get_conn() as conn:
        # Find-or-create user
        row = conn.execute(
            "SELECT user_id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if row is None:
            user_id = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO users (user_id, username, display_name, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    username,
                    display_name,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )
        else:
            user_id = row["user_id"]

        # Existing credentials for this user — pass them as exclude_credentials
        # so the browser refuses to re-register the same authenticator.
        existing = conn.execute(
            "SELECT credential_id FROM webauthn_credentials "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (user_id,),
        ).fetchall()
        exclude_descriptors = [
            PublicKeyCredentialDescriptor(id=row["credential_id"])
            for row in existing
        ]

        options = generate_registration_options(
            rp_id=settings.webauthn_rp_id,
            rp_name=settings.webauthn_rp_name,
            user_id=bytes.fromhex(user_id),
            user_name=username,
            user_display_name=display_name,
            attestation=AttestationConveyancePreference.DIRECT,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            supported_pub_key_algs=[
                COSEAlgorithmIdentifier.EDDSA,           # -8 (Ed25519, preferred per ADR-002 D1)
                COSEAlgorithmIdentifier.ECDSA_SHA_256,   # -7 (ES256, broadly compatible)
            ],
            exclude_credentials=exclude_descriptors,
        )

        challenge_id = secrets.token_urlsafe(24)
        expires_at = (
            dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=CHALLENGE_TTL_SECONDS)
        ).isoformat()

        conn.execute(
            "INSERT INTO pending_challenges "
            "(challenge_id, kind, user_id, username, challenge, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (challenge_id, "register", user_id, username, options.challenge, expires_at),
        )

        return RegistrationStart(
            challenge_id=challenge_id,
            options_json=options_to_json(options),
        )


def complete_registration(
    challenge_id: str,
    credential_payload: dict,
    nickname: str | None = None,
) -> str:
    """Verify a registration response and persist the credential.

    Returns the user_id of the now-enrolled user. Raises ValueError on
    any verification failure.
    """
    sweep_expired_challenges()

    with get_conn() as conn:
        chal_row = conn.execute(
            "SELECT user_id, username, challenge, expires_at FROM pending_challenges "
            "WHERE challenge_id = ? AND kind = 'register'",
            (challenge_id,),
        ).fetchone()

        if chal_row is None:
            raise ValueError("Challenge not found or already consumed")
        # Sweep ran, so an expired row would be gone; check defensively anyway.
        if dt.datetime.fromisoformat(chal_row["expires_at"]) < dt.datetime.now(dt.timezone.utc):
            raise ValueError("Challenge expired")

        user_id = chal_row["user_id"]
        challenge_bytes = chal_row["challenge"]

        # Verify the attestation
        verification = verify_registration_response(
            credential=credential_payload,
            expected_challenge=challenge_bytes,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_expected_origins,
            require_user_verification=True,
        )

        # Store the credential
        conn.execute(
            "INSERT INTO webauthn_credentials ("
            "credential_id, user_id, public_key, sign_count, transports, aaguid, "
            "nickname, backed_up, backup_eligible, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                verification.credential_id,
                user_id,
                verification.credential_public_key,
                verification.sign_count,
                ",".join(credential_payload.get("response", {}).get("transports", []) or []),
                verification.aaguid,
                nickname,
                int(verification.credential_backed_up),
                int(verification.credential_device_type == "multi_device"),
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
        )

        # Consume the challenge
        conn.execute(
            "DELETE FROM pending_challenges WHERE challenge_id = ?", (challenge_id,)
        )

    return user_id


# ============================================================================
# Authentication
# ============================================================================


@dataclass
class AuthenticationStart:
    challenge_id: str
    options_json: str


def start_authentication(username: str | None = None) -> AuthenticationStart:
    """Begin an authentication ceremony.

    If `username` is None we ask the browser for any resident-key credential
    on the authenticator (usernameless flow). If `username` is given we
    restrict the allowed credentials to that user's enrolled set.
    """
    sweep_expired_challenges()

    with get_conn() as conn:
        allow_descriptors: list[PublicKeyCredentialDescriptor] = []
        user_id: str | None = None
        if username:
            row = conn.execute(
                "SELECT user_id FROM users WHERE username = ?", (username,)
            ).fetchone()
            if row is not None:
                user_id = row["user_id"]
                creds = conn.execute(
                    "SELECT credential_id FROM webauthn_credentials "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (user_id,),
                ).fetchall()
                allow_descriptors = [
                    PublicKeyCredentialDescriptor(id=c["credential_id"]) for c in creds
                ]
            # If the user doesn't exist we deliberately don't leak — the
            # browser will still surface a generic auth failure later.

        options = generate_authentication_options(
            rp_id=settings.webauthn_rp_id,
            user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=allow_descriptors,
        )

        challenge_id = secrets.token_urlsafe(24)
        expires_at = (
            dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=CHALLENGE_TTL_SECONDS)
        ).isoformat()
        conn.execute(
            "INSERT INTO pending_challenges "
            "(challenge_id, kind, user_id, username, challenge, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (challenge_id, "login", user_id, username, options.challenge, expires_at),
        )

        return AuthenticationStart(
            challenge_id=challenge_id,
            options_json=options_to_json(options),
        )


@dataclass
class AuthenticationResult:
    user_id: str
    username: str
    credential_id_hex: str


def complete_authentication(challenge_id: str, credential_payload: dict) -> AuthenticationResult:
    """Verify an authentication response and update the sign_count.

    Returns the user_id + username + credential_id_hex on success.
    Raises ValueError on any verification failure.
    """
    sweep_expired_challenges()

    with get_conn() as conn:
        chal_row = conn.execute(
            "SELECT user_id, challenge, expires_at FROM pending_challenges "
            "WHERE challenge_id = ? AND kind = 'login'",
            (challenge_id,),
        ).fetchone()
        if chal_row is None:
            raise ValueError("Challenge not found or already consumed")
        if dt.datetime.fromisoformat(chal_row["expires_at"]) < dt.datetime.now(dt.timezone.utc):
            raise ValueError("Challenge expired")

        # The browser sends the credential id in `rawId` (b64url). Decode.
        raw_id_b64 = credential_payload.get("rawId") or credential_payload.get("id")
        if not raw_id_b64:
            raise ValueError("Missing credential id in response")
        # Base64url decode (no padding-friendly)
        padding = "=" * (-len(raw_id_b64) % 4)
        credential_id = base64.urlsafe_b64decode(raw_id_b64 + padding)

        cred_row = conn.execute(
            "SELECT user_id, public_key, sign_count FROM webauthn_credentials "
            "WHERE credential_id = ? AND revoked_at IS NULL",
            (credential_id,),
        ).fetchone()
        if cred_row is None:
            raise ValueError("Credential not enrolled")

        verification = verify_authentication_response(
            credential=credential_payload,
            expected_challenge=chal_row["challenge"],
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=settings.webauthn_expected_origins,
            credential_public_key=cred_row["public_key"],
            credential_current_sign_count=cred_row["sign_count"],
            require_user_verification=True,
        )

        user_id = cred_row["user_id"]

        # Bump sign_count + last_used_at
        conn.execute(
            "UPDATE webauthn_credentials "
            "SET sign_count = ?, last_used_at = ? "
            "WHERE credential_id = ?",
            (
                verification.new_sign_count,
                dt.datetime.now(dt.timezone.utc).isoformat(),
                credential_id,
            ),
        )

        # Consume the challenge
        conn.execute(
            "DELETE FROM pending_challenges WHERE challenge_id = ?", (challenge_id,)
        )

        user_row = conn.execute(
            "SELECT username FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        username = user_row["username"] if user_row else "<unknown>"

        return AuthenticationResult(
            user_id=user_id,
            username=username,
            credential_id_hex=credential_id.hex(),
        )


# ============================================================================
# Credential listing (for the "registered devices" page later)
# ============================================================================


def list_credentials(user_id: str) -> list[dict]:
    """Return non-revoked credentials for a user."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT credential_id, nickname, aaguid, created_at, last_used_at, "
            "backed_up, backup_eligible "
            "FROM webauthn_credentials "
            "WHERE user_id = ? AND revoked_at IS NULL "
            "ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return [
            {
                "credential_id_hex": row["credential_id"].hex(),
                "nickname": row["nickname"],
                "aaguid": row["aaguid"],
                "created_at": row["created_at"],
                "last_used_at": row["last_used_at"],
                "backed_up": bool(row["backed_up"]),
                "backup_eligible": bool(row["backup_eligible"]),
            }
            for row in rows
        ]
