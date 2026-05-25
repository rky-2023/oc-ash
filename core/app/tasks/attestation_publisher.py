"""Daily Merkle root attestation publisher (Phase 2 task 2.13).

Reads audit_entries from the Postgres projection for a given UTC date,
computes a binary Merkle tree root over the raw_envelope column, then
commits an attestation JSON document to rky-2023/openclaw-attestations
via the GitHub App API.

Auth flow:
  1. Read GitHub App private key PEM from Vault KV (via oc-with-vault-creds.sh
     or OC_GITHUB_APP_PRIVATE_KEY env var for tests).
  2. Mint a 10-minute RS256 JWT (GitHub App JWT spec).
  3. POST /app/installations/{id}/access_tokens → short-lived install token.
  4. Use that token for all Git Data API calls (blob → tree → commit → ref).

The attestation document (YYYY/MM/DD.json) schema:
  {
    "version": "1",
    "date": "YYYY-MM-DD",
    "entry_count": <int>,
    "first_ulid": <str | null>,
    "last_ulid": <str | null>,
    "merkle_root": "sha256:<hex>",
    "leaf_hashes": ["sha256:<hex>", ...],
    "generated_at": "<ISO8601>",
    "sig_attestation": "<alg>:<hex>"   (HMAC-SHA256 fallback; Vault transit in Phase 3)
  }
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac as _hmac_mod
import json
import secrets
import time
from typing import Any

import asyncpg
import httpx
import structlog

log = structlog.get_logger(__name__)

_HMAC_KEY: bytes = secrets.token_bytes(32)


# ── Merkle tree ──────────────────────────────────────────────────────────────


def _merkle_root(leaves: list[bytes]) -> bytes:
    """Binary Merkle root over SHA-256 leaf hashes. Returns 32 zero bytes for empty input."""
    if not leaves:
        return b"\x00" * 32
    layer = [hashlib.sha256(leaf).digest() for leaf in leaves]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        layer = [
            hashlib.sha256(layer[i] + layer[i + 1]).digest()
            for i in range(0, len(layer), 2)
        ]
    return layer[0]


def compute_merkle(raw_envelopes: list[bytes]) -> tuple[str, list[str]]:
    """Return (merkle_root_hex, leaf_hashes_hex) for a list of raw envelope bytes."""
    leaf_digests = [hashlib.sha256(b).digest() for b in raw_envelopes]
    root = _merkle_root(raw_envelopes)
    root_hex = "sha256:" + root.hex()
    leaf_hexes = ["sha256:" + d.hex() for d in leaf_digests]
    return root_hex, leaf_hexes


# ── Postgres query ───────────────────────────────────────────────────────────


async def fetch_day_envelopes(
    dsn: str, date: dt.date
) -> tuple[list[bytes], str | None, str | None]:
    """Fetch raw_envelope bytes for all entries on `date` (UTC), ordered by ulid.

    Returns (raw_envelopes, first_ulid, last_ulid).
    """
    start = dt.datetime(date.year, date.month, date.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=30)
    try:
        rows = await pool.fetch(
            """
            SELECT ulid, raw_envelope
            FROM openclaw.audit_entries
            WHERE ts >= $1 AND ts < $2
            ORDER BY ulid ASC
            """,
            start,
            end,
        )
    finally:
        await pool.close()

    if not rows:
        return [], None, None

    raw = [r["raw_envelope"] if isinstance(r["raw_envelope"], bytes)
           else r["raw_envelope"].encode() for r in rows]
    return raw, rows[0]["ulid"], rows[-1]["ulid"]


# ── Attestation document ─────────────────────────────────────────────────────


def build_attestation(
    date: dt.date,
    raw_envelopes: list[bytes],
    first_ulid: str | None,
    last_ulid: str | None,
) -> dict[str, Any]:
    """Compute the Merkle root and build the unsigned attestation document."""
    merkle_root_hex, leaf_hashes = compute_merkle(raw_envelopes)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    doc: dict[str, Any] = {
        "version": "1",
        "date": date.isoformat(),
        "entry_count": len(raw_envelopes),
        "first_ulid": first_ulid,
        "last_ulid": last_ulid,
        "merkle_root": merkle_root_hex,
        "leaf_hashes": leaf_hashes,
        "generated_at": now_iso,
    }

    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    sig = "hmac-sha256:" + _hmac_mod.new(_HMAC_KEY, payload, hashlib.sha256).hexdigest()
    doc["sig_attestation"] = sig
    return doc


# ── GitHub App auth ──────────────────────────────────────────────────────────


def _make_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Mint a 10-minute GitHub App JWT (RS256) using PyJWT."""
    try:
        import jwt as _jwt
    except ImportError as exc:
        raise RuntimeError("PyJWT[crypto] is required for attestation publishing") from exc

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (9 * 60),
        "iss": app_id,
    }
    return _jwt.encode(payload, private_key_pem, algorithm="RS256")


async def get_installation_token(
    app_id: str,
    installation_id: str,
    private_key_pem: str,
    api_url: str,
) -> str:
    """Exchange a GitHub App JWT for an installation access token."""
    app_jwt = _make_app_jwt(app_id, private_key_pem)
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"{api_url}/app/installations/{installation_id}/access_tokens"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers)
        if resp.status_code != 201:
            raise RuntimeError(
                f"GitHub installation token request failed: {resp.status_code} {resp.text[:200]}"
            )
        return str(resp.json()["token"])


# ── GitHub commit ────────────────────────────────────────────────────────────


async def _gh(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    token: str,
    api_url: str,
    **kwargs: Any,
) -> Any:
    """Make an authenticated GitHub API request; raise on non-2xx."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    resp = await client.request(method, f"{api_url}{path}", headers=headers, **kwargs)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"GitHub API {method} {path} → {resp.status_code}: {resp.text[:300]}")
    return resp.json()


async def commit_attestation(
    doc: dict[str, Any],
    date: dt.date,
    repo: str,
    token: str,
    api_url: str,
) -> str:
    """Create a commit in `repo` that adds/updates YYYY/MM/DD.json. Returns the new commit SHA."""
    file_path = f"{date.year}/{date.month:02d}/{date.day:02d}.json"
    content_bytes = json.dumps(doc, sort_keys=True, indent=2).encode()
    content_b64 = base64.b64encode(content_bytes).decode()
    short_root = doc["merkle_root"].replace("sha256:", "")[:16]
    commit_message = (
        f"attest({date.isoformat()}): Merkle root {short_root} ({doc['entry_count']} entries)"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get current HEAD SHA for main branch
        ref_data = await _gh(client, "GET", f"/repos/{repo}/git/refs/heads/main", token, api_url)
        head_sha = ref_data["object"]["sha"]

        # Get the current tree SHA from the commit
        commit_data = await _gh(
            client, "GET", f"/repos/{repo}/git/commits/{head_sha}", token, api_url
        )
        base_tree_sha = commit_data["tree"]["sha"]

        # Create blob
        blob_data = await _gh(
            client,
            "POST",
            f"/repos/{repo}/git/blobs",
            token,
            api_url,
            json={"content": content_b64, "encoding": "base64"},
        )
        blob_sha = blob_data["sha"]

        # Create tree
        tree_data = await _gh(
            client,
            "POST",
            f"/repos/{repo}/git/trees",
            token,
            api_url,
            json={
                "base_tree": base_tree_sha,
                "tree": [{"path": file_path, "mode": "100644", "type": "blob", "sha": blob_sha}],
            },
        )
        new_tree_sha = tree_data["sha"]

        # Create commit
        new_commit = await _gh(
            client,
            "POST",
            f"/repos/{repo}/git/commits",
            token,
            api_url,
            json={
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [head_sha],
            },
        )
        new_commit_sha = new_commit["sha"]

        # Advance ref
        await _gh(
            client,
            "PATCH",
            f"/repos/{repo}/git/refs/heads/main",
            token,
            api_url,
            json={"sha": new_commit_sha},
        )

    log.info(
        "attestation.committed",
        repo=repo,
        file=file_path,
        commit=new_commit_sha,
        merkle_root=doc["merkle_root"],
        entries=doc["entry_count"],
    )
    return new_commit_sha


# ── Top-level publish ────────────────────────────────────────────────────────


async def publish_attestation(date: dt.date) -> dict[str, Any]:
    """Full publish flow for one day. Returns the attestation document + commit SHA.

    Raises on any unrecoverable error (caller decides on retry / logging).
    """
    from app.config import settings

    dsn = settings.effective_postgres_dsn
    if not dsn:
        raise RuntimeError("OC_POSTGRES_DSN is not set — cannot read audit_entries")

    pem = settings.effective_github_app_private_key
    if not pem:
        raise RuntimeError(
            "GitHub App private key not found. "
            "Set OC_GITHUB_APP_PRIVATE_KEY or OC_GITHUB_APP_PRIVATE_KEY_FILE."
        )

    if not settings.github_app_id or not settings.github_installation_id:
        raise RuntimeError("OC_GITHUB_APP_ID and OC_GITHUB_INSTALLATION_ID must be set")

    log.info("attestation.publish.start", date=date.isoformat())

    raw_envelopes, first_ulid, last_ulid = await fetch_day_envelopes(dsn, date)
    log.info("attestation.fetch_done", date=date.isoformat(), count=len(raw_envelopes))

    doc = build_attestation(date, raw_envelopes, first_ulid, last_ulid)

    install_token = await get_installation_token(
        settings.github_app_id,
        settings.github_installation_id,
        pem,
        settings.github_api_url,
    )

    commit_sha = await commit_attestation(
        doc,
        date,
        settings.github_attestations_repo,
        install_token,
        settings.github_api_url,
    )

    return {**doc, "commit_sha": commit_sha}
