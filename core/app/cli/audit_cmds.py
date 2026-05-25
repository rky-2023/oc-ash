"""`oc audit` subcommands: tail / replay / verify."""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from typing import Any

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
def replay(conv_id: str, json_out: bool) -> None:
    """Reconstruct one A2A conversation from immudb by conv_id."""
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
def verify(date_str: str | None) -> None:
    """Verify signatures + prev_hash chain over stored envelopes."""
    if date_str is None:
        target = dt.date.today()
    else:
        try:
            target = dt.date.fromisoformat(date_str)
        except ValueError:
            click.echo(f"Invalid date: {date_str!r} (expected YYYY-MM-DD)", err=True)
            sys.exit(2)

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
            if verify_envelope(env, slot="service"):
                ok_service += 1
            else:
                bad_service.append(env.ulid)
            if verify_envelope(env, slot="appender"):
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
