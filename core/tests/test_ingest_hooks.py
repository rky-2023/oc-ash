"""Tests for the Claude Code hook payload mapping (pure parse_hook)."""

from __future__ import annotations

from app.ingest.hooks import parse_hook


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
