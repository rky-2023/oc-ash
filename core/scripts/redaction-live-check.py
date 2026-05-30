#!/usr/bin/env python3
"""Live end-to-end redaction check (ADR-003 D6 / task 2.7).

Publishes ONE synthetic `oc.event.mail.received` envelope carrying a fake
secret and fake PII, lets the running audit-appender redact it (OPA → drop/
encrypt) and persist to immudb, waits for the projector to materialise it in
Postgres, then asserts:

  - the fake secret string appears NOWHERE in the stored envelope (dropped);
  - api_key      → "<redacted:secret>";
  - body         → "<encrypted:...>" with a matching encrypted_blobs entry,
                   and the PII plaintext is absent;
  - a `from`/`method` field is kept verbatim;
  - policy.redaction_version is stamped;
  - sig_service / sig_appender / chain all verify on the new entry.

Run in a credentialed shell with core RUNNING and OPA up:

    source core/scripts/oc-with-vault-creds.sh      # or run-with-vault-creds.sh's env
    core/.venv/bin/python core/scripts/redaction-live-check.py

Needs: OC_VAULT_TOKEN (sign sig_service), OC_NATS_URL, OC_POSTGRES_DSN.
Exit 0 = all assertions pass; 1 = a redaction invariant failed; 2 = setup/timeout.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import asyncpg

from app.audit.envelope import Action, Actor, ActorKind, AuditEnvelope, Direction
from app.audit.signer import sign_envelope
from app.bus.nats_client import get_bus
from app.config import settings

# Distinctive sentinels we can string-search for afterwards.
FAKE_SECRET = "sk_live_REDACTION_SMOKE_" + uuid.uuid4().hex
PII_BODY = "Hi Bob — wifi pw is hunter2 and SSN 123-45-6789. -Alice"
PII_MARKERS = ("hunter2", "123-45-6789")
SUBJECT = "oc.event.mail.received"
POLL_SECONDS = 30


def _ok(label: str) -> None:
    print(f"  \033[1;32mPASS\033[0m  {label}")


def _bad(label: str) -> None:
    print(f"  \033[1;31mFAIL\033[0m  {label}")


async def _publish() -> str:
    bus = get_bus()
    await bus.connect()
    conv = "redaction-smoke-" + uuid.uuid4().hex[:12]
    env = AuditEnvelope.new(
        subject=SUBJECT,
        actor=Actor(kind=ActorKind.SERVICE, id="redaction-live-check"),
        action=Action.EVENT,
        direction=Direction.INGRESS,
        conv_id=conv,
        redacted_payload={
            "api_key": FAKE_SECRET,
            "body": PII_BODY,
            "from": "alice@example.com",
            "method": "GET",
        },
    )
    await sign_envelope(env, slot="service")
    await bus.publish(SUBJECT, env.to_wire_bytes(), msg_id=env.ulid)
    await bus.close()
    print(f"[publish] ulid={env.ulid} conv={conv}")
    return conv


async def _await_projection(pool: asyncpg.Pool, conv: str) -> dict | None:
    for _ in range(POLL_SECONDS * 2):  # 0.5s steps
        row = await pool.fetchrow(
            "SELECT raw_envelope, sig_service_valid, sig_appender_valid, chain_valid "
            "FROM openclaw.audit_entries WHERE conv_id = $1 ORDER BY ulid DESC LIMIT 1",
            conv,
        )
        if row is not None:
            return dict(row)
        await asyncio.sleep(0.5)
    return None


def _assert(row: dict) -> bool:
    raw = row["raw_envelope"]
    env = json.loads(raw)
    payload = env.get("redacted_payload") or {}
    blobs = env.get("encrypted_blobs") or []
    ok = True

    # 1. The fake secret must be gone everywhere.
    if FAKE_SECRET not in raw:
        _ok("dropped secret is absent from the stored envelope")
    else:
        _bad("SECRET LEAKED — fake api_key still present in raw_envelope"); ok = False

    # 2. api_key dropped to placeholder.
    if payload.get("api_key") == "<redacted:secret>":
        _ok('api_key → "<redacted:secret>"')
    else:
        _bad(f"api_key not dropped (got {payload.get('api_key')!r})"); ok = False

    # 3. body encrypted, plaintext gone, blob present.
    body = payload.get("body", "")
    if isinstance(body, str) and body.startswith("<encrypted:") and any(b.get("field") == "body" for b in blobs):
        _ok('body → "<encrypted:...>" with an encrypted_blobs entry')
    else:
        _bad(f"body not encrypted (got {body!r}; blobs={[b.get('field') for b in blobs]})"); ok = False
    if not any(m in raw for m in PII_MARKERS):
        _ok("PII plaintext (wifi pw / SSN) is absent from the stored envelope")
    else:
        _bad("PII LEAKED — body plaintext still present in raw_envelope"); ok = False

    # 4. ordinary fields kept.
    if payload.get("method") == "GET" and payload.get("from") == "alice@example.com":
        _ok("keep-fields (method, from) preserved verbatim")
    else:
        _bad(f"keep-fields altered (method={payload.get('method')!r} from={payload.get('from')!r})"); ok = False

    # 5. version pin.
    rv = (env.get("policy") or {}).get("redaction_version")
    if rv and rv.startswith("sha256:") and rv != "sha256:unknown":
        _ok(f"policy.redaction_version pinned ({rv[:18]}…)")
    else:
        _bad(f"redaction_version not pinned (got {rv!r})"); ok = False

    # 6. integrity flags from the projector.
    if row["sig_service_valid"] and row["sig_appender_valid"] and row["chain_valid"]:
        _ok("sig_service + sig_appender + chain all valid on the new entry")
    else:
        _bad(
            f"integrity flags: svc={row['sig_service_valid']} "
            f"app={row['sig_appender_valid']} chain={row['chain_valid']}"
        ); ok = False

    return ok


async def main() -> int:
    dsn = settings.effective_postgres_dsn
    if not dsn:
        print("OC_POSTGRES_DSN not set — run via oc-with-vault-creds.sh", file=sys.stderr)
        return 2
    if not settings.vault_token:
        print("OC_VAULT_TOKEN not set — needed to sign sig_service", file=sys.stderr)
        return 2

    conv = await _publish()
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        row = await _await_projection(pool, conv)
    finally:
        await pool.close()

    if row is None:
        print(
            f"\nTIMEOUT: no projected entry for conv {conv} within {POLL_SECONDS}s.\n"
            "Is core running (appender+projector) and OPA up? Check core logs.",
            file=sys.stderr,
        )
        return 2

    print(f"\nProjected entry found for conv {conv}; checking redaction:\n")
    ok = _assert(row)
    print("\n\033[1;32mREDACTION LIVE CHECK PASSED\033[0m" if ok else "\n\033[1;31mREDACTION LIVE CHECK FAILED\033[0m")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
