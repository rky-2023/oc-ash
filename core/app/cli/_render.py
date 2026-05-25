"""Pretty-printing helpers for audit envelopes."""

from __future__ import annotations

import json
import sys

import click

from app.audit.envelope import AuditEnvelope


def render_tail_line(env: AuditEnvelope) -> str:
    """One-line summary for `oc audit tail`."""
    ts = env.ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    conv = (env.conv_id or "-")[:8]
    actor = f"{env.actor.kind.value}:{env.actor.id}"
    if len(actor) > 28:
        actor = actor[:27] + "…"
    extra = ""
    pl = env.redacted_payload or {}
    if env.action.value == "request":
        method = pl.get("method", "?")
        path = pl.get("path", "?")
        extra = f"{method} {path}"
    elif env.action.value == "response":
        status = pl.get("status_code", "?")
        elapsed = pl.get("elapsed_ms")
        extra = f"→ {status}" + (f" ({elapsed}ms)" if elapsed is not None else "")
    return f"{ts:<28} {env.subject:<40} conv:{conv:<8} {actor:<30} {env.action.value:<8} {extra}"


def render_replay_ladder(envs: list[AuditEnvelope]) -> str:
    """Multi-line ladder-diagram view for `oc audit replay`."""
    if not envs:
        return "(no envelopes found)"
    out: list[str] = []
    out.append("═" * 70)
    out.append(f"Conversation: {envs[0].conv_id}")
    out.append(f"Started:      {envs[0].ts.isoformat(timespec='milliseconds').replace('+00:00','Z')}")
    out.append(f"Entries:      {len(envs)}")
    out.append("═" * 70)
    out.append("")
    for i, env in enumerate(envs):
        is_last = i == len(envs) - 1
        prefix = "└─" if is_last else "├─"
        ts = env.ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        actor_id = f"{env.actor.kind.value}:{env.actor.id}"
        out.append(f"{prefix} {ts}  {actor_id}")
        out.append(f"│  {env.action.value.upper():<8}  {env.subject}")
        if env.redacted_payload:
            for k, v in env.redacted_payload.items():
                out.append(f"│    {k}: {v}")
        if env.policy:
            out.append(f"│  policy:   {env.policy.decision} ({env.policy.rule or 'no-rule'})")
        if env.encrypted_blobs:
            out.append(f"│  encrypted blobs: {len(env.encrypted_blobs)} (revealable with YubiKey touch)")
        if not is_last:
            out.append("│")
    return "\n".join(out)


def echo_envelope_json(env: AuditEnvelope) -> None:
    """Structured JSON output mode."""
    click.echo(json.dumps(json.loads(env.to_wire_bytes()), indent=2, sort_keys=True))


def stderr(msg: str, **kwargs) -> None:
    """For status / error lines that shouldn't pollute piped JSON output."""
    click.echo(msg, err=True, **kwargs)
