"""Tests for the Claude Code hook payload mapping (pure parse_hook)."""

from __future__ import annotations

import pytest

from app.ingest import hooks as hooks_mod
from app.ingest.hooks import build_fs_event, build_git_event, handle_event, parse_hook


def test_pretooluse_maps_subject_and_payload() -> None:
    p = parse_hook({
        "hook_event_name": "PreToolUse",
        "session_id": "s-123",
        "cwd": "/home/asher/openclaw",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    })
    assert p["subject"] == "oc.event.claude.PreToolUse"
    assert p["session_id"] == "s-123"
    assert p["payload"]["tool_name"] == "Bash"
    assert p["payload"]["tool_input"] == {"command": "ls"}
    assert p["filter_fields"] == {"hook": "PreToolUse", "body": ""}


def test_notification_body_feeds_filter() -> None:
    p = parse_hook({"hook_event_name": "Notification", "message": "build done", "session_id": "s"})
    assert p["filter_fields"] == {"hook": "Notification", "body": "build done"}


def test_empty_notification_body_blank() -> None:
    # Empty body → filter will drop (per ingest.rego); parse keeps body "".
    p = parse_hook({"hook_event_name": "Notification", "session_id": "s"})
    assert p["filter_fields"]["body"] == ""


def test_sessionstart_subject() -> None:
    p = parse_hook({"hook_event_name": "SessionStart", "session_id": "s", "cwd": "/x"})
    assert p["subject"] == "oc.event.claude.SessionStart"
    assert p["payload"]["cwd"] == "/x"


def test_unknown_hook_defaults() -> None:
    p = parse_hook({"session_id": "s"})
    assert p["subject"] == "oc.event.claude.Unknown"


# ── git hooks (task 3.2) ───────────────────────────────────────────────────────


def test_build_git_event_subject_and_payload() -> None:
    ev = build_git_event({
        "source": "git", "repo": "openclaw", "hook": "post-commit",
        "sha": "abc123", "ref": "main", "author": "A <a@b>", "subject": "do thing",
    })
    assert ev["subject"] == "oc.event.git.openclaw.post-commit"
    assert ev["source"] == "git"
    assert ev["payload"]["sha"] == "abc123"
    assert ev["payload"]["ref"] == "main"
    assert ev["filter_fields"] == {}


def test_build_git_event_sanitises_repo_token() -> None:
    ev = build_git_event({"repo": "repo.with.dots", "hook": "pre-push", "remote": "origin"})
    assert ".with.dots." not in ev["subject"]
    assert ev["subject"].startswith("oc.event.git.repo_with_dots.")
    assert ev["payload"]["remote"] == "origin"


def test_build_git_event_omits_missing_fields() -> None:
    ev = build_git_event({"repo": "x", "hook": "post-merge"})
    assert "sha" not in ev["payload"]  # not provided → omitted, not null


@pytest.mark.asyncio
async def test_handle_event_routes_git(monkeypatch) -> None:
    calls = {}

    async def _fake_emit(**kw):
        calls.update(kw)
        return True

    monkeypatch.setattr(hooks_mod, "emit_event", _fake_emit)
    ok = await handle_event({"source": "git", "repo": "openclaw", "hook": "post-commit", "sha": "z"})
    assert ok is True
    assert calls["source"] == "git"
    assert calls["subject"] == "oc.event.git.openclaw.post-commit"
    assert calls["actor_id"] == "git:openclaw"


# ── fswatch (task 3.1) ─────────────────────────────────────────────────────────


def test_build_fs_event_subject_and_payload() -> None:
    ev = build_fs_event({
        "source": "fswatch", "path": "/home/asher/openclaw/PLAN.md",
        "repo": "openclaw", "kind": "modified", "size_bytes": 1234,
        "mtime": "2026-06-04T00:00:00Z", "change_count": 3,
    })
    assert ev["subject"] == "oc.event.fs.openclaw.modified"
    assert ev["source"] == "fswatch"
    assert ev["payload"]["path"] == "/home/asher/openclaw/PLAN.md"
    assert ev["payload"]["change_count"] == 3
    # path is forwarded to the ingest filter so noise globs can drop it.
    assert ev["filter_fields"] == {"path": "/home/asher/openclaw/PLAN.md"}


def test_build_fs_event_defaults_root_and_modified() -> None:
    # No repo / kind supplied → subject falls back to _root / modified.
    ev = build_fs_event({"path": "/home/asher/loose.txt"})
    assert ev["subject"] == "oc.event.fs._root.modified"


def test_build_fs_event_omits_missing_fields() -> None:
    ev = build_fs_event({"repo": "openclaw", "kind": "deleted", "path": "/x"})
    assert "size_bytes" not in ev["payload"]  # not provided → omitted, not null


@pytest.mark.asyncio
async def test_handle_event_routes_fswatch(monkeypatch) -> None:
    calls = {}

    async def _fake_emit(**kw):
        calls.update(kw)
        return True

    monkeypatch.setattr(hooks_mod, "emit_event", _fake_emit)
    ok = await handle_event({
        "source": "fswatch", "repo": "openclaw", "kind": "created", "path": "/x",
    })
    assert ok is True
    assert calls["source"] == "fswatch"
    assert calls["subject"] == "oc.event.fs.openclaw.created"
    assert calls["actor_id"] == "fswatch:openclaw"
    assert calls["filter_fields"] == {"path": "/x"}
