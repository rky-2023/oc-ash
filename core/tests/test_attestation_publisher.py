"""Unit tests for the attestation publisher (structural / pure-function only).

No live Postgres, no live GitHub API.  Integration tests belong in the
e2e suite that runs against the full docker-compose stack.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

from app.tasks.attestation_publisher import (
    build_attestation,
    compute_merkle,
)


# ── compute_merkle ────────────────────────────────────────────────────────────


def test_compute_merkle_empty():
    root, leaves = compute_merkle([])
    assert root == "sha256:" + ("00" * 32)
    assert leaves == []


def test_compute_merkle_single():
    blob = b"hello"
    root, leaves = compute_merkle([blob])
    leaf_hex = "sha256:" + hashlib.sha256(blob).hexdigest()
    # For a single leaf the root IS the leaf hash (no pairing needed)
    assert leaves == [leaf_hex]
    assert root.startswith("sha256:")
    assert len(root) == len("sha256:") + 64


def test_compute_merkle_two_leaves_deterministic():
    a, b = b"env-a", b"env-b"
    r1, _ = compute_merkle([a, b])
    r2, _ = compute_merkle([a, b])
    assert r1 == r2


def test_compute_merkle_order_matters():
    a, b = b"env-a", b"env-b"
    r_ab, _ = compute_merkle([a, b])
    r_ba, _ = compute_merkle([b, a])
    assert r_ab != r_ba


def test_compute_merkle_odd_count_consistent():
    leaves = [b"a", b"b", b"c"]
    r1, _ = compute_merkle(leaves)
    r2, _ = compute_merkle(leaves)
    assert r1 == r2


# ── build_attestation ─────────────────────────────────────────────────────────


def test_build_attestation_shape():
    date = dt.date(2026, 5, 25)
    raw = [b'{"ulid":"01A","ts":"2026-05-25T01:00:00Z"}']
    doc = build_attestation(date, raw, "01A", "01A")
    assert doc["version"] == "1"
    assert doc["date"] == "2026-05-25"
    assert doc["entry_count"] == 1
    assert doc["first_ulid"] == "01A"
    assert doc["last_ulid"] == "01A"
    assert doc["merkle_root"].startswith("sha256:")
    assert len(doc["leaf_hashes"]) == 1
    assert doc["sig_attestation"].startswith("hmac-sha256:")


def test_build_attestation_empty_day():
    date = dt.date(2026, 1, 1)
    doc = build_attestation(date, [], None, None)
    assert doc["entry_count"] == 0
    assert doc["first_ulid"] is None
    assert doc["last_ulid"] is None
    assert doc["merkle_root"] == "sha256:" + ("00" * 32)
    assert doc["leaf_hashes"] == []


def test_build_attestation_is_json_serializable():
    date = dt.date(2026, 5, 25)
    raw = [b"envelope-bytes"]
    doc = build_attestation(date, raw, "u1", "u1")
    dumped = json.dumps(doc)
    reparsed = json.loads(dumped)
    assert reparsed["date"] == "2026-05-25"


def test_build_attestation_merkle_matches_compute():
    date = dt.date(2026, 5, 25)
    raw = [b"a", b"b", b"c"]
    doc = build_attestation(date, raw, "u1", "u3")
    expected_root, expected_leaves = compute_merkle(raw)
    assert doc["merkle_root"] == expected_root
    assert doc["leaf_hashes"] == expected_leaves
