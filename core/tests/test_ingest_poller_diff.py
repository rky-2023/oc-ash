"""Tests for the gh-poller pure diff engine + subject building."""

from __future__ import annotations

from app.ingest.poller import (
    ENDPOINTS,
    EndpointSpec,
    _pr_state,
    diff,
    subject_for,
)

PULLS = next(s for s in ENDPOINTS if s.entity == "pull_request")
COMMITS = next(s for s in ENDPOINTS if s.entity == "commit")


def test_first_run_is_baseline_no_events() -> None:
    entities = [{"number": 1, "state": "open", "updated_at": "t1"}]
    events, cp = diff(PULLS, entities, None)
    assert events == []
    assert cp["seen"] == {"1": "t1"}


def test_new_entity_emits_after_baseline() -> None:
    _, cp = diff(PULLS, [{"number": 1, "state": "open", "updated_at": "t1"}], None)
    entities = [
        {"number": 2, "state": "open", "updated_at": "t2"},
        {"number": 1, "state": "open", "updated_at": "t1"},
    ]
    events, cp2 = diff(PULLS, entities, cp)
    assert [(e.entity, e.state) for e in events] == [("pull_request", "opened")]
    assert cp2["seen"]["2"] == "t2"


def test_changed_entity_emits() -> None:
    _, cp = diff(PULLS, [{"number": 1, "state": "open", "updated_at": "t1"}], None)
    events, _ = diff(PULLS, [{"number": 1, "state": "closed", "updated_at": "t2"}], cp)
    assert [(e.entity, e.state) for e in events] == [("pull_request", "closed")]


def test_unchanged_entity_no_event() -> None:
    _, cp = diff(PULLS, [{"number": 1, "state": "open", "updated_at": "t1"}], None)
    events, _ = diff(PULLS, [{"number": 1, "state": "open", "updated_at": "t1"}], cp)
    assert events == []


def test_immutable_commit_no_rechurn() -> None:
    # commits have no updated_field; same sha must not re-emit.
    _, cp = diff(COMMITS, [{"sha": "abc"}], None)
    events, _ = diff(COMMITS, [{"sha": "abc"}], cp)
    assert events == []
    events2, _ = diff(COMMITS, [{"sha": "abc"}, {"sha": "def"}], cp)
    assert [e.state for e in events2] == ["pushed"]


def test_missing_key_skipped() -> None:
    events, cp = diff(PULLS, [{"state": "open", "updated_at": "t1"}], {"seen": {}})
    assert events == []
    assert cp["seen"] == {}


def test_pr_state_merged() -> None:
    assert _pr_state({"state": "closed", "merged_at": "t"}, False) == "merged"
    assert _pr_state({"state": "open"}, True) == "opened"
    assert _pr_state({"state": "open"}, False) == "updated"
    assert _pr_state({"state": "closed"}, False) == "closed"


def test_seen_map_trims() -> None:
    spec = EndpointSpec("x", "/p", {}, 60, "number", "updated_at", lambda e, n: "s")
    big = {"seen": {str(i): "t" for i in range(600)}}
    _, cp = diff(spec, [], big)
    assert len(cp["seen"]) == 500


def test_subject_sanitises_tokens() -> None:
    assert subject_for("rky-2023", "oc-ash", "pull_request", "opened") == \
        "oc.event.gh.rky-2023.oc-ash.pull_request.opened"
    # dots in a token would split NATS subjects → replaced
    assert ".v1.2." not in subject_for("o", "repo.v1.2", "release", "published")
