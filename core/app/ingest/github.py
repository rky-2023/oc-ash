"""GitHub App auth for the gh-poller.

Reuses the proven helper from the attestation publisher (KV-PEM → RS256 App
JWT → installation access token); the poller just caches the 1-hour token
and refreshes at ~50 min.

Note (deviation from the phase-3 draft): the draft prescribed minting the
App JWT via Vault transit (`transit/sign/github-app-openclaw-bot`). That
transit key doesn't exist; the working path everywhere else in the codebase
is the KV-PEM + PyJWT route used here. Transit-signed App JWTs are a later
hardening if desired.
"""

from __future__ import annotations

import time

import structlog

from app.config import settings
from app.tasks.attestation_publisher import get_installation_token

log = structlog.get_logger(__name__)

# Installation tokens last 1h; refresh a little early.
_REFRESH_BEFORE_S = 50 * 60

_cached_token: str | None = None
_cached_exp: float = 0.0


async def installation_token() -> str:
    """Return a valid installation access token, minting/caching as needed."""
    global _cached_token, _cached_exp
    now = time.time()
    if _cached_token is not None and now < _cached_exp:
        return _cached_token

    pem = settings.effective_github_app_private_key
    if not (settings.github_app_id and settings.github_installation_id and pem):
        raise RuntimeError(
            "GitHub App creds not configured "
            "(need OC_GITHUB_APP_ID, OC_GITHUB_INSTALLATION_ID, OC_GITHUB_APP_PRIVATE_KEY)"
        )

    token = await get_installation_token(
        settings.github_app_id,
        settings.github_installation_id,
        pem,
        settings.github_api_url,
    )
    _cached_token = token
    _cached_exp = now + _REFRESH_BEFORE_S
    log.info("gh_poller.token_minted")
    return token


def _reset_cache_for_tests() -> None:
    global _cached_token, _cached_exp
    _cached_token = None
    _cached_exp = 0.0
