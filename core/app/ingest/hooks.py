"""Claude Code hook receiver (Phase 3 task 3.3).

Claude Code fires a tiny script on each hook event (PreToolUse, PostToolUse,
SessionStart, Stop, Notification, …); the script POSTs the hook's stdin JSON
to a unix socket this receiver listens on. We map it to
`oc.event.claude.<hook>`, augment it, run it through the ingest filter, and
publish via the shared emit path. SessionStart also opens a per-session
manifest under sessions/ (bridges to Phase 12 session-scoping).

The HTTP handling is intentionally minimal (a unix-socket line/Content-Length
parse) to avoid pulling in a web framework for a localhost-only endpoint.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import structlog

from app.config import settings
from app.ingest.emit import emit_event

log = structlog.get_logger(__name__)


# ── Pure payload mapping (unit-tested) ─────────────────────────────────────────


def parse_hook(body: dict[str, Any]) -> dict[str, Any]:
    """Map a Claude Code hook payload to subject + envelope payload + filter
    inputs. Pure — no IO."""
    hook = body.get("hook_event_name") or body.get("hook") or "Unknown"
    session_id = body.get("session_id")
    message = body.get("message") or body.get("notification") or ""
    payload: dict[str, Any] = {
        "hook": hook,
        "session_id": session_id,
        "cwd": body.get("cwd"),
        "tool_name": body.get("tool_name"),
        "message": message,
    }
    if body.get("tool_input") is not None:
        payload["tool_input"] = body["tool_input"]
    if body.get("tool_response") is not None:
        payload["tool_response"] = body["tool_response"]
    return {
        "subject": f"oc.event.claude.{hook}",
        "hook": hook,
        "session_id": session_id,
        "payload": payload,
        "filter_fields": {"hook": hook, "body": message},
    }


def _git_head(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _append_manifest(parsed: dict[str, Any]) -> None:
    session_id = parsed.get("session_id")
    if not session_id:
        return
    day = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    path = settings.ingest_sessions_dir / day / f"{session_id}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now(dt.UTC).isoformat()
        if parsed["hook"] == "SessionStart" and not path.exists():
            path.write_text(f"# Session {session_id}\n\nstarted: {ts}\ncwd: {parsed['payload'].get('cwd')}\n\n")
        with path.open("a") as f:
            tool = parsed["payload"].get("tool_name")
            f.write(f"- {ts} **{parsed['hook']}**" + (f" `{tool}`" if tool else "") + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("ingest.hooks.manifest_failed", session_id=session_id, err=str(e))


async def handle_hook(body: dict[str, Any]) -> bool:
    """Process one hook payload end-to-end. Returns True if published."""
    parsed = parse_hook(body)
    head = _git_head(parsed["payload"].get("cwd"))
    if head:
        parsed["payload"]["git_head"] = head
    _append_manifest(parsed)
    return await emit_event(
        subject=parsed["subject"],
        source="claude",
        actor_id="claude-code",
        payload=parsed["payload"],
        filter_fields=parsed["filter_fields"],
        conv_id=parsed.get("session_id"),
    )


# ── Minimal unix-socket HTTP server ────────────────────────────────────────────


class ClaudeHookReceiver:
    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        sock_path = Path(settings.ingest_socket)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_conn, path=str(sock_path))
        log.info("ingest.hooks.listening", socket=str(sock_path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        log.info("ingest.hooks.stopped")

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            raw = await reader.readexactly(length) if length else b""
            status = b"200 OK"
            try:
                if raw:
                    await handle_hook(json.loads(raw))
            except Exception as e:  # noqa: BLE001
                log.warning("ingest.hooks.handle_failed", err=str(e))
                status = b"500 Internal Server Error"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            log.debug("ingest.hooks.conn_error", err=str(e))
        finally:
            writer.close()
