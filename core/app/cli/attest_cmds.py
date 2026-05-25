"""`oc attest` subcommands: publish."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys

import click


@click.group()
def attest() -> None:
    """Publish and verify daily Merkle root attestations."""


@attest.command()
@click.option(
    "--date",
    "date_str",
    default=None,
    help="UTC date to publish (YYYY-MM-DD). Default: yesterday.",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    help="Print the attestation document as JSON after publishing.",
)
def publish(date_str: str | None, json_out: bool) -> None:
    """Compute the Merkle root for a day and commit to openclaw-attestations.

    Reads audit_entries from the Postgres projection (OC_POSTGRES_DSN).
    Authenticates via GitHub App JWT (OC_GITHUB_APP_ID, OC_GITHUB_INSTALLATION_ID,
    OC_GITHUB_APP_PRIVATE_KEY or OC_GITHUB_APP_PRIVATE_KEY_FILE).
    """
    if date_str is None:
        target = dt.date.today() - dt.timedelta(days=1)
    else:
        try:
            target = dt.date.fromisoformat(date_str)
        except ValueError:
            click.echo(f"Invalid date: {date_str!r} (expected YYYY-MM-DD)", err=True)
            sys.exit(2)

    result = asyncio.run(_run_publish(target))
    if json_out:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Date:        {result['date']}")
        click.echo(f"Entries:     {result['entry_count']}")
        click.echo(f"Merkle root: {result['merkle_root']}")
        click.echo(f"Commit:      {result.get('commit_sha', 'n/a')}")
        click.echo("Status:      ✅ published")


async def _run_publish(date: dt.date) -> dict:
    from app.tasks.attestation_publisher import publish_attestation

    try:
        return await publish_attestation(date)
    except Exception as e:
        click.echo(f"[oc attest publish] failed: {e}", err=True)
        sys.exit(1)
