"""`oc audit` subcommands: tail / replay / verify."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from typing import Any

import asyncpg
import click

from app.audit.envelope import AuditEnvelope
from app.audit.signer import verify_envelope
from app.cli._render import (
    echo_envelope_json,
    render_replay_ladder,
    render_tail_line,
    stderr,
)


@click.group()
def audit() -> None:
    """Inspect the audit pipeline (NATS bus + immudb ledger)."""


# ─────────────────────────────────────────────────────────────────────
# tail
# ─────────────────────────────────────────────────────────────────────


@audit.command()
@click.option(
    "--subject",
    default="oc.>",
    help="NATS subject pattern to subscribe to (default: oc.>).",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    help="Output full envelopes as JSON instead of one-line summaries.",
)
def tail(subject: str, json_out: bool) -> None:
    """Live tail of audit envelopes on the bus. Ctrl+C to stop."""
    asyncio.run(_run_tail(subject, json_out))


async def _run_tail(subject: str, json_out: bool) -> None:
    from nats.aio.client import Client as NATSClient

    from app.config import settings

    nc = NATSClient()
    try:
        await nc.connect(servers=[settings.nats_url], name="oc-cli-tail")
    except Exception as e:
        stderr(f"[oc] NATS connect failed: {e}", err=True)
        sys.exit(2)

    stderr(f"[oc] connected to {settings.nats_url}; subscribing to {subject!r}")
    stderr("[oc] Ctrl+C to stop\n")

    async def handler(msg):  # type: ignore[no-untyped-def]
        try:
            env = AuditEnvelope.model_validate_json(msg.data)
        except Exception as e:
            stderr(f"[oc] bad envelope on {msg.subject}: {e}")
            return
        if json_out:
            echo_envelope_json(env)
        else:
            click.echo(render_tail_line(env))

    sub = await nc.subscribe(subject, cb=handler)

    try:
        # Sleep forever; Ctrl+C interrupts cleanly
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await sub.unsubscribe()
        await nc.close()


# ─────────────────────────────────────────────────────────────────────
# replay <conv_id>
# ─────────────────────────────────────────────────────────────────────


@audit.command()
@click.argument("conv_id")
@click.option(
    "--json", "json_out", is_flag=True, help="Output JSON envelopes instead of the ladder diagram."
)
@click.option(
    "--fast",
    is_flag=True,
    help="Use Postgres projection index instead of full immudb scan (requires OC_POSTGRES_DSN).",
)
def replay(conv_id: str, json_out: bool, fast: bool) -> None:
    """Reconstruct one A2A conversation from immudb by conv_id."""
    if fast:
        envs = asyncio.run(_collect_by_conv_id_fast(conv_id))
    else:
        envs = asyncio.run(_collect_by_conv_id(conv_id))
    if json_out:
        for env in envs:
            echo_envelope_json(env)
    else:
        click.echo(render_replay_ladder(envs))


async def _collect_by_conv_id(conv_id: str) -> list[AuditEnvelope]:
    """Scan immudb for envelopes matching conv_id. Returns sorted by ULID (i.e. time)."""
    writer = _connect_immudb()
    envs: list[AuditEnvelope] = []
    try:
        # Iterate all entries — there's no index by conv_id yet (Phase 2 task 2.9's
        # projector adds it). For small ledgers this is fine; for big ones the
        # projector is the right path.
        keys_and_values = await _scan_all(writer)
        for key, value in keys_and_values:
            try:
                env = AuditEnvelope.model_validate_json(value)
            except Exception:
                continue
            if env.conv_id == conv_id:
                envs.append(env)
        envs.sort(key=lambda e: e.ulid)
    finally:
        await writer.close()
    return envs


async def _collect_by_conv_id_fast(conv_id: str) -> list[AuditEnvelope]:
    """Query Postgres projection for envelopes by conv_id — O(matches) via index."""
    from app.config import settings

    dsn = settings.effective_postgres_dsn
    if not dsn:
        click.echo("[oc] OC_POSTGRES_DSN not set — cannot use --fast path", err=True)
        sys.exit(2)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=30)
    try:
        rows = await pool.fetch(
            "SELECT raw_envelope FROM openclaw.audit_entries"
            " WHERE conv_id = $1 ORDER BY ulid ASC",
            conv_id,
        )
    finally:
        await pool.close()

    envs: list[AuditEnvelope] = []
    for row in rows:
        raw = row["raw_envelope"]
        try:
            env = AuditEnvelope.model_validate(raw) if isinstance(raw, dict) \
                else AuditEnvelope.model_validate_json(raw)
        except Exception:
            continue
        envs.append(env)
    return envs


# ─────────────────────────────────────────────────────────────────────
# verify [--date YYYY-MM-DD]
# ─────────────────────────────────────────────────────────────────────


@audit.command()
@click.option(
    "--date",
    "date_str",
    default=None,
    help="ISO date (YYYY-MM-DD) to verify. Default: today UTC.",
)
@click.option(
    "--fast",
    is_flag=True,
    help="Use pre-computed sig/chain results from Postgres projection (requires OC_POSTGRES_DSN).",
)
def verify(date_str: str | None, fast: bool) -> None:
    """Verify signatures + prev_hash chain over stored envelopes."""
    if date_str is None:
        target = dt.date.today()
    else:
        try:
            target = dt.date.fromisoformat(date_str)
        except ValueError:
            click.echo(f"Invalid date: {date_str!r} (expected YYYY-MM-DD)", err=True)
            sys.exit(2)

    if fast:
        asyncio.run(_run_verify_fast(target))
    else:
        asyncio.run(_run_verify(target))


async def _run_verify(target: dt.date) -> None:
    writer = _connect_immudb()
    total = ok_service = ok_appender = chain_ok = 0
    bad_service: list[str] = []
    bad_appender: list[str] = []
    chain_breaks: list[str] = []
    prev_hash: str | None = None

    try:
        keys_and_values = await _scan_all(writer)
        # Sort by key (= ULID) so timeline order matches insert order
        keys_and_values.sort(key=lambda kv: kv[0])
        for _key, value in keys_and_values:
            try:
                env = AuditEnvelope.model_validate_json(value)
            except Exception:
                continue
            if env.ts.date() != target:
                # Reset chain tracking if we skipped past entries
                prev_hash = None
                continue
            total += 1
            # Signature checks
            if await verify_envelope(env, slot="service"):
                ok_service += 1
            else:
                bad_service.append(env.ulid)
            if await verify_envelope(env, slot="appender"):
                ok_appender += 1
            else:
                bad_appender.append(env.ulid)
            # Chain check
            if prev_hash is not None and env.prev_hash != prev_hash:
                chain_breaks.append(env.ulid)
            else:
                chain_ok += 1
            prev_hash = env.canonical_sha256()
    finally:
        await writer.close()

    _print_verify_summary(target, total, ok_service, ok_appender, chain_ok,
                          bad_service, bad_appender, chain_breaks)


async def _run_verify_fast(target: dt.date) -> None:
    """Read pre-computed sig/chain validity from Postgres projection — O(day) not O(all)."""
    from app.config import settings

    dsn = settings.effective_postgres_dsn
    if not dsn:
        click.echo("[oc] OC_POSTGRES_DSN not set — cannot use --fast path", err=True)
        sys.exit(2)

    start = dt.datetime(target.year, target.month, target.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=30)
    try:
        rows = await pool.fetch(
            """
            SELECT ulid, sig_service_valid, sig_appender_valid, chain_valid
            FROM openclaw.audit_entries
            WHERE ts >= $1 AND ts < $2
            ORDER BY ulid ASC
            """,
            start,
            end,
        )
    finally:
        await pool.close()

    total = len(rows)
    ok_service = sum(1 for r in rows if r["sig_service_valid"])
    ok_appender = sum(1 for r in rows if r["sig_appender_valid"])
    chain_ok = sum(1 for r in rows if r["chain_valid"])
    bad_service = [r["ulid"] for r in rows if not r["sig_service_valid"]]
    bad_appender = [r["ulid"] for r in rows if not r["sig_appender_valid"]]
    chain_breaks = [r["ulid"] for r in rows if not r["chain_valid"]]

    _print_verify_summary(target, total, ok_service, ok_appender, chain_ok,
                          bad_service, bad_appender, chain_breaks)


def _print_verify_summary(
    target: dt.date,
    total: int,
    ok_service: int,
    ok_appender: int,
    chain_ok: int,
    bad_service: list[str],
    bad_appender: list[str],
    chain_breaks: list[str],
) -> None:
    click.echo(f"Date: {target.isoformat()}")
    click.echo(f"Entries: {total}")
    click.echo(f"Signatures: service {ok_service}/{total}, appender {ok_appender}/{total}")
    click.echo(f"Chain:      {chain_ok}/{total} prev_hash links match")
    if bad_service:
        stderr(f"  bad service sigs: {bad_service[:5]}{'...' if len(bad_service) > 5 else ''}")
    if bad_appender:
        stderr(f"  bad appender sigs: {bad_appender[:5]}{'...' if len(bad_appender) > 5 else ''}")
    if chain_breaks:
        stderr(f"  chain breaks: {chain_breaks[:5]}{'...' if len(chain_breaks) > 5 else ''}")
    bad = bad_service or bad_appender or chain_breaks
    if total == 0:
        click.echo("Status:     ⚠️  no envelopes for that date")
        sys.exit(0)
    if bad:
        click.echo("Status:     ❌ FAIL")
        sys.exit(1)
    else:
        click.echo("Status:     ✅ PASS")


# ─────────────────────────────────────────────────────────────────────
# projection rebuild
# ─────────────────────────────────────────────────────────────────────


@audit.group()
def projection() -> None:
    """Manage the Postgres projection of the immudb ledger."""


@projection.command("rebuild")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def projection_rebuild(yes: bool) -> None:
    """Truncate the Postgres projection and replay it from immudb.

    immudb is the source of truth; the projection is a rebuildable cache.
    Safe and idempotent. Requires OC_POSTGRES_DSN + immudb creds (run via
    oc-with-vault-creds.sh). Automates the manual procedure in docs/RUNBOOK.md.
    """
    asyncio.run(_run_projection_rebuild(yes))


async def _run_projection_rebuild(assume_yes: bool) -> None:
    from app.audit.immudb_writer import get_writer
    from app.audit.projector import AuditProjector
    from app.config import settings

    dsn = settings.effective_postgres_dsn
    if not dsn:
        click.echo("[oc] OC_POSTGRES_DSN not set — run via oc-with-vault-creds.sh", err=True)
        sys.exit(2)

    if not assume_yes:
        click.confirm(
            "This TRUNCATEs openclaw.audit_* and replays from immudb. Continue?",
            abort=True,
        )

    writer = get_writer()
    await writer.connect()

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, command_timeout=60)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "TRUNCATE openclaw.audit_entries, openclaw.audit_conversations, "
                    "openclaw.audit_policy_decisions"
                )
                await conn.execute(
                    "UPDATE openclaw.audit_checkpoint "
                    "SET last_ulid = NULL, last_ts = NULL, updated_at = now() WHERE id = 1"
                )
        click.echo("[oc] projection truncated; checkpoint reset")

        # Reuse the projector's verified scan/verify/insert cycle.
        proj = AuditProjector()
        proj._pool = pool
        total = 0
        while True:
            n = await proj._cycle()
            if n == 0:
                break
            total += n
            click.echo(f"[oc] projected {total} entries…")

        pg_count = await pool.fetchval("SELECT count(*) FROM openclaw.audit_entries")
        immudb_count = await _immudb_entry_count(writer)
    finally:
        await pool.close()
        await writer.close()

    click.echo(f"[oc] rebuild complete: postgres={pg_count} immudb={immudb_count}")
    if immudb_count >= 1000:
        stderr("  note: immudb scan is capped at 1000/cycle — counts above that are not yet paginated")
    if pg_count != immudb_count:
        click.echo("Status:     ⚠️  counts differ (unparseable/corrupt entries are skipped — see logs)", err=True)
        sys.exit(1)
    click.echo("Status:     ✅ rows match immudb")


async def _immudb_entry_count(writer: Any) -> int:
    def _count() -> int:
        result = writer._client.scan(b"", b"", False, 1000)
        return len(result or {})

    return await asyncio.to_thread(_count)


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


def _connect_immudb() -> Any:
    """Sync-connect via the existing writer wrapper; the projector role
    only needs read perms but the writer class works for both."""
    from app.audit.immudb_writer import get_writer

    writer = get_writer()

    async def _do() -> None:
        await writer.connect()

    asyncio.get_event_loop().run_until_complete(_do())  # we're already inside asyncio.run
    return writer


async def _scan_all(writer: Any) -> list[tuple[bytes, bytes]]:
    """Iterate all (key, value) pairs from immudb. MVP — Phase 2 task 2.9
    adds an indexed Postgres projection that beats this in performance."""
    if writer._client is None:
        await writer.connect()

    def _do_scan() -> list[tuple[bytes, bytes]]:
        pairs: list[tuple[bytes, bytes]] = []
        try:
            # immudb-py's `scan(prefix, seekKey, limit, desc)` API varies by
            # version. We attempt a few invocations defensively.
            entries = writer._client.scan(b"", 0, 10000, False)
        except TypeError:
            try:
                entries = writer._client.scan(b"", b"", 10000, False)
            except Exception:
                entries = []
        for e in entries or []:
            k = getattr(e, "key", None)
            v = getattr(e, "value", None)
            if k is not None and v is not None:
                pairs.append((k, v))
        return pairs

    return await asyncio.to_thread(_do_scan)
