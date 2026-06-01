"""Tests for the OPA ingest filter (fail-open semantics)."""

from __future__ import annotations

import pytest

from app.ingest import filter as flt


class _Resp:
    def __init__(self, result: object) -> None:
        self._payload = {"result": result}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, *, result: object = "keep", exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def post(self, url: str, json: dict) -> _Resp:  # noqa: A002
        if self._exc is not None:
            raise self._exc
        return _Resp(self._result)


def _patch(monkeypatch, **kw) -> None:
    monkeypatch.setattr(flt.httpx, "AsyncClient", lambda *a, **k: _Client(**kw))


@pytest.mark.asyncio
async def test_keep_decision(monkeypatch) -> None:
    _patch(monkeypatch, result="keep")
    assert await flt.should_keep("git", hook="post-commit") is True


@pytest.mark.asyncio
async def test_drop_decision(monkeypatch) -> None:
    _patch(monkeypatch, result="drop")
    assert await flt.should_keep("fswatch", path="/x/dist/y.js") is False


@pytest.mark.asyncio
async def test_fail_open_when_opa_unreachable(monkeypatch) -> None:
    # OPA down → keep (noise filtering must never drop real events).
    _patch(monkeypatch, exc=RuntimeError("connection refused"))
    assert await flt.should_keep("gh-poller", changed=True) is True


@pytest.mark.asyncio
async def test_null_result_keeps(monkeypatch) -> None:
    # Undefined policy result (None) is not "drop" → keep.
    _patch(monkeypatch, result=None)
    assert await flt.should_keep("claude", hook="PreToolUse") is True
