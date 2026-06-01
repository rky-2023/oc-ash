"""GitHub polling worker (Phase 3 task 3.4, ADR-004 defer-and-poll).

Polls the GitHub App API per ADR-004 D1 and emits one `oc.event.gh.*` per
new/changed entity. The diff engine (`diff`) is a pure function — given a
list of API entities and the prior checkpoint, it returns the events to emit
and the next checkpoint — so the interesting logic is unit-tested without a
network.

Checkpoints live in `openclaw.lookup` under
`gh-poller.checkpoints.<owner>.<repo>.<entity>` so polling resumes across
restarts without re-emitting (checkpoint discipline). The first poll of a
fresh endpoint is a BASELINE: it seeds the checkpoint and emits nothing, so
we don't flood the ledger with pre-existing history on startup.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.config import settings
from app.ingest import github
from app.ingest.emit import emit_event

log = structlog.get_logger(__name__)

# Keep the per-endpoint "seen" map bounded; GitHub list endpoints are paged
# at ~20, so 500 keys covers plenty of churn without unbounded growth.
_SEEN_CAP = 500


# ── Endpoint specs ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EndpointSpec:
    entity: str  # subject token, e.g. "pull_request"
    path: str  # "/repos/{owner}/{repo}/pulls"
    params: dict[str, str]
    cadence_s: int
    key_field: str  # stable id: "number" | "sha" | "id"
    updated_field: str | None  # change-detection field, or None if immutable
    state_fn: Callable[[dict[str, Any], bool], str]  # (entity, is_new) -> state


def _pr_state(e: dict[str, Any], is_new: bool) -> str:
    if e.get("merged_at"):
        return "merged"
    if e.get("state") == "open":
        return "opened" if is_new else "updated"
    if e.get("state") == "closed":
        return "closed"
    return "updated"


def _issue_state(e: dict[str, Any], is_new: bool) -> str:
    if e.get("state") == "open":
        return "opened" if is_new else "updated"
    if e.get("state") == "closed":
        return "closed"
    return "updated"


def _run_state(e: dict[str, Any], is_new: bool) -> str:
    return str(e.get("conclusion") or e.get("status") or "updated")


def _release_state(e: dict[str, Any], is_new: bool) -> str:
    return "published" if e.get("published_at") else "created"


ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec("pull_request", "/repos/{owner}/{repo}/pulls",
                 {"state": "all", "sort": "updated", "per_page": "20"}, 60, "number", "updated_at", _pr_state),
    EndpointSpec("issue", "/repos/{owner}/{repo}/issues",
                 {"state": "all", "sort": "updated", "per_page": "20"}, 60, "number", "updated_at", _issue_state),
    EndpointSpec("commit", "/repos/{owner}/{repo}/commits",
                 {"per_page": "20"}, 60, "sha", None, lambda e, n: "pushed"),
    EndpointSpec("workflow_run", "/repos/{owner}/{repo}/actions/runs",
                 {"per_page": "20"}, 30, "id", "updated_at", _run_state),
    EndpointSpec("release", "/repos/{owner}/{repo}/releases",
                 {"per_page": "10"}, 300, "id", "published_at", _release_state),
]


@dataclass
class Event:
    entity: str
    state: str
    data: dict[str, Any]


# ── Pure diff engine (unit-tested) ────────────────────────────────────────────


def _trim(seen: dict[str, str], cap: int) -> dict[str, str]:
    if len(seen) <= cap:
        return seen
    # Keep the lexicographically-largest keys (ULID/number-ish): good enough
    # to retain the most recent without timestamps.
    keep = sorted(seen.keys())[-cap:]
    return {k: seen[k] for k in keep}


def diff(
    spec: EndpointSpec,
    entities: list[dict[str, Any]],
    checkpoint: dict[str, Any] | None,
) -> tuple[list[Event], dict[str, Any]]:
    """Return (events_to_emit, next_checkpoint).

    checkpoint is None on the very first poll → BASELINE: seed, emit nothing.
    Otherwise emit for entities unseen (new) or whose updated_field changed.
    """
    first_run = checkpoint is None
    seen: dict[str, str] = dict((checkpoint or {}).get("seen", {}))
    events: list[Event] = []

    for e in entities:
        raw_key = e.get(spec.key_field)
        if raw_key is None or raw_key == "":
            continue
        key = str(raw_key)
        cur = str(e.get(spec.updated_field)) if spec.updated_field else "1"
        is_new = key not in seen
        changed = (not is_new) and spec.updated_field is not None and seen.get(key) != cur
        if not first_run and (is_new or changed):
            events.append(Event(entity=spec.entity, state=spec.state_fn(e, is_new), data=e))
        seen[key] = cur

    return events, {"seen": _trim(seen, _SEEN_CAP)}


def subject_for(owner: str, repo: str, entity: str, state: str) -> str:
    """oc.event.gh.<owner>.<repo>.<entity>.<state>, sanitised for NATS tokens."""
    def tok(s: str) -> str:
        return s.replace(".", "_").replace(" ", "_").replace("/", "_")

    return f"oc.event.gh.{tok(owner)}.{tok(repo)}.{tok(entity)}.{tok(state)}"


# ── Async polling loop ─────────────────────────────────────────────────────────


@dataclass
class GitHubPoller:
    _stop: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def _repos(self) -> list[dict[str, str]]:
        if settings.ingest_gh_repos:
            return json.loads(settings.ingest_gh_repos)
        return [
            {"owner": "rky-2023", "repo": "oc-ash"},
            {"owner": "rky-2023", "repo": "openclaw-attestations"},
        ]

    async def start(self) -> None:
        self._stop.clear()
        for repo in self._repos():
            for spec in ENDPOINTS:
                self._tasks.append(
                    asyncio.create_task(
                        self._poll_forever(repo["owner"], repo["repo"], spec),
                        name=f"gh-poll-{repo['repo']}-{spec.entity}",
                    )
                )
        log.info("gh_poller.started", repos=[r["repo"] for r in self._repos()], endpoints=len(ENDPOINTS))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()
        log.info("gh_poller.stopped")

    async def _poll_forever(self, owner: str, repo: str, spec: EndpointSpec) -> None:
        from app.ingest.checkpoint import load_checkpoint, save_checkpoint

        ckey = f"gh-poller.checkpoints.{owner}.{repo}.{spec.entity}"
        backoff = 0.0
        last_success = asyncio.get_event_loop().time()
        while not self._stop.is_set():
            try:
                token = await github.installation_token()
                entities = await self._fetch(owner, repo, spec, token)
                checkpoint = await load_checkpoint(ckey)
                events, next_cp = diff(spec, entities, checkpoint)
                for ev in events:
                    await emit_event(
                        subject=subject_for(owner, repo, ev.entity, ev.state),
                        source="gh-poller",
                        actor_id=f"gh-poller:{owner}/{repo}",
                        payload=_event_payload(owner, repo, ev),
                        filter_fields={"changed": True},
                    )
                await save_checkpoint(ckey, next_cp)
                last_success = asyncio.get_event_loop().time()
                backoff = 0.0
                await self._sleep(spec.cadence_s)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as e:
                backoff = _next_backoff(backoff, e.response.status_code)
                log.warning("gh_poller.http_error", repo=repo, entity=spec.entity,
                            status=e.response.status_code, backoff=backoff)
                await self._maybe_health(owner, repo, spec, last_success)
                await self._sleep(backoff)
            except Exception as e:  # noqa: BLE001
                backoff = _next_backoff(backoff, 500)
                log.warning("gh_poller.error", repo=repo, entity=spec.entity, err=str(e), backoff=backoff)
                await self._maybe_health(owner, repo, spec, last_success)
                await self._sleep(backoff)

    async def _fetch(self, owner: str, repo: str, spec: EndpointSpec, token: str) -> list[dict[str, Any]]:
        url = settings.github_api_url.rstrip("/") + spec.path.format(owner=owner, repo=repo)
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers, params=spec.params)
            resp.raise_for_status()
            data = resp.json()
        # actions/runs nests the list under "workflow_runs"
        if isinstance(data, dict):
            for k in ("workflow_runs", "check_runs", "items"):
                if k in data:
                    return list(data[k])
            return []
        return list(data)

    async def _maybe_health(self, owner: str, repo: str, spec: EndpointSpec, last_success: float) -> None:
        # 1h without a success on this endpoint → publish a health-failing event.
        if asyncio.get_event_loop().time() - last_success > 3600:
            try:
                await emit_event(
                    subject="oc.event.gh.health.failing",
                    source="gh-poller",
                    actor_id="gh-poller",
                    payload={"owner": owner, "repo": repo, "entity": spec.entity},
                    filter_fields={"changed": True},
                )
            except Exception:  # noqa: BLE001
                log.error("gh_poller.health_emit_failed", repo=repo, entity=spec.entity)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(1.0, seconds))
        except asyncio.TimeoutError:
            pass


def _event_payload(owner: str, repo: str, ev: Event) -> dict[str, Any]:
    """A compact payload — full bodies stay in GitHub; we record identity +
    the fields useful for the audit timeline (redaction still applies)."""
    d = ev.data
    return {
        "owner": owner,
        "repo": repo,
        "entity": ev.entity,
        "state": ev.state,
        "id": d.get("number") or d.get("sha") or d.get("id"),
        "title": d.get("title") or d.get("name") or d.get("display_title"),
        "url": d.get("html_url") or d.get("url"),
        "actor": (d.get("user") or d.get("author") or {}).get("login") if isinstance(d.get("user") or d.get("author"), dict) else None,
        "updated_at": d.get("updated_at") or d.get("published_at"),
    }


def _next_backoff(current: float, status: int) -> float:
    """Exponential backoff: rate-limit (429) caps at 10 min, 5xx at 5 min."""
    cap = 600.0 if status == 429 else 300.0
    nxt = 30.0 if current == 0 else current * 2
    return min(nxt, cap)
