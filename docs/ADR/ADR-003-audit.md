# ADR-003: Audit ledger — immudb + NATS-first append + external Merkle attestation

- **Status:** Accepted
- **Date:** 2026-05-23
- **Deciders:** rky
- **Supersedes:** —
- **Superseded by:** —
- **Related:** ADR-001 (architecture, esp. R2 — `rky-2023/openclaw-attestations` as the external witness), ADR-002 (auth, esp. D7 — per-call MCP JWTs that are recorded in audit envelopes), `docs/THREAT_MODEL.md` (§3.8 immudb row, §3.3 MCP proxy row)

---

## Context

openclaw's `THREAT_MODEL.md` claims, in two places:

1. "**Every byte is logged**": every MCP request/response and every A2A message is captured.
2. "**Tampering is globally observable**": daily Merkle root published to a public, independent location.

Those claims must be backed by a concrete pipeline. This ADR specifies it.

ADR-001 R2 already fixed *where* the attestation is published (`rky-2023/openclaw-attestations`). This ADR fixes *what* is stored locally, *how* it gets there, *how* it is verified, *how* it ages out, and *what* happens when the pipeline fails.

Three constraints frame the design:

- **WORM at the storage layer**, not at the application layer. A revocable DBA / root-on-box should not silently rewrite the ledger.
- **Per-entry cryptographic verification**, not just "the log file looks intact."
- **Redaction must happen *before* append.** A redactable audit log is not WORM — if you can edit it, it isn't append-only. Secrets get redacted; everything else stays.

---

## Decision

### D1. Storage: **immudb** as the WORM audit ledger, single-node v1.

- immudb runs as a container under `openclaw-core`'s docker-compose project, on a dedicated dm-crypt volume mounted at `/var/lib/openclaw/immudb/`.
- A single database (`openclaw_audit`) with a single collection-equivalent (`entries`). Per-key cryptographic verification via immudb's intrinsic Merkle tree.
- Backups: nightly snapshot via `immuadmin database export` → MinIO (age-encrypted with a key in Vault) → weekly offsite to Backblaze B2.

Single-node is sufficient for v1 (single-user, single-host). HA / read replicas are a future ADR if openclaw ever runs multi-host.

### D2. Audit envelope schema.

Every message on `oc.*` produces exactly one audit entry. Schema (Pydantic v2, serialized as canonical JSON):

```jsonc
{
  "ulid":            "01ARZ3NDEKTSV4RRFFQ69G5FAV",   // sortable id, source of `created_at`
  "ts":              "2026-05-23T14:02:11.341Z",     // ISO8601 with ms
  "subject":         "oc.mcp.google-calendar.tools/call",
  "conv_id":         "abc123",                         // null if not part of an A2A conversation
  "actor": {
    "kind":          "agent" | "user" | "mcp" | "service" | "github" | "external",
    "id":            "agents/planner",                  // SPIFFE-style for services
    "jwt_jti":       "550e8400-e29b-41d4-a716-446655440000"
  },
  "action":          "request" | "response" | "event" | "decision",
  "direction":       "ingress" | "egress" | "internal",
  "request_hash":    "sha256:<hex>",                    // hash of the canonical request body (post-redaction)
  "response_hash":   "sha256:<hex>" | null,             // for paired entries
  "policy": {
    "decision":      "allow" | "deny" | "n/a",
    "rule":          "calendar.write.self",
    "input_hash":    "sha256:<hex>"
  },
  "redacted_payload": "<JSON, post-redaction>",         // the human-readable view; may be partial
  "encrypted_blobs": [                                   // for PII/secrets that are *kept* but gated
    { "key_id": "vault://transit/audit-pii/v3", "ciphertext": "..." }
  ],
  "prev_hash":       "sha256:<hex>",                    // hash of the immediately prior entry's full envelope
  "sig_service":     "ed25519:<hex>",                   // signature by the originating service's transit key
  "sig_appender":    "ed25519:<hex>"                    // signature by core's audit-appender service key
}
```

Two signatures (service + appender) mean a forged entry must compromise *two* signing keys, in two separate Vault namespaces.

`prev_hash` provides a per-database hash chain *independent of* immudb's own Merkle tree — a defense-in-depth check that would catch a hypothetical immudb-side rewrite.

### D3. Two-phase append: NATS first, then immudb.

Every event flows:

1. **Phase A (durability):** The originating service publishes the message to its NATS subject (e.g., `oc.mcp.gmail.tools/call`). JetStream gives at-least-once delivery and disk durability immediately.
2. **Phase B (cryptographic anchor):** The `audit-appender` service consumes from a JetStream consumer (`audit-appender` durable name), constructs the envelope per D2, signs, and writes to immudb.

Phase A is the source of truth for *liveness*. Phase B is the source of truth for *attestation*. If immudb is down, NATS queues; the appender catches up. If NATS is down, the originating service fails closed (we'd rather drop a request than emit an un-anchored event).

### D4. Lag SLO and failure response.

- **Target:** appender lag < 500 ms p95 from NATS publish to immudb commit.
- **Warning:** lag > 60 s sustained for 1 min → Grafana alert + Android notification ("audit lag high").
- **Critical:** lag > 24 h → enter **paranoid mode**:
  - All `oc.*` publishers receive a `NoResponders` from a synthetic health subject.
  - The MCP proxy refuses new tool calls.
  - The notifier refuses to send anything outbound except the alarm itself.
  - Manual intervention required to clear via `oc audit unwedge --confirm`.

The point of paranoid mode is that *silent gap* is the failure mode we cannot tolerate. A noisy halt is acceptable.

### D5. Postgres projection for fast reads.

- Source of truth: immudb. Projection: a `openclaw.audit_*` schema in the existing Postgres.
- The `audit-projector` service subscribes to immudb's CDC stream (immudb's `db.streamScan`) and writes denormalized rows into:
  - `audit_entries` (one row per envelope, indexed by `ts`, `conv_id`, `actor.id`)
  - `audit_conversations` (one row per `conv_id`, aggregating start/end/last-action)
  - `audit_policy_decisions` (one row per allow/deny, indexed by `rule`)
- The viewer (`audit/viewer/`, Next.js) reads only from Postgres. Verification commands (`oc audit verify`) bypass Postgres and go directly to immudb.

If the projection is corrupted or out of date, dropping the schema and replaying from immudb is a documented operation (Phase 11 runbook entry).

### D6. Redaction pipeline (pre-append, not post-append).

A redactable audit log is not append-only. So redaction happens **before** the envelope is signed and persisted.

- The appender, after consuming from NATS and *before* writing to immudb, runs the payload through OPA's `policy/redaction.rego`.
- The redaction policy distinguishes three outcomes per field:
  - **Drop:** field is replaced with `"<redacted:secret>"`. The original value is **not** stored anywhere. Used for: API keys, OAuth tokens, JWT bearer values, Shamir share material, FIDO2 attestation private bits, anything matching the secret-regex+entropy heuristic.
  - **Encrypt:** field's value is encrypted with a Vault transit key, ciphertext stored in `encrypted_blobs[]`, the field in `redacted_payload` shows `"<encrypted:vault://transit/audit-pii/v3>"`. Used for: mail bodies, calendar event descriptions, file contents from `fs-asher` reads. Reveal requires a fresh YubiKey touch (see ADR-002 D5).
  - **Keep:** field stored as-is. Default for: metadata, IDs, timestamps, tool names, policy decisions, hashes.
- The redaction policy is itself version-pinned per envelope (`policy.redaction_version`) so we can prove which version of the policy made the decision.

Important: **redaction decisions are reversible only via the encrypt path**. If a field is dropped, it is gone forever. The encrypt path lets us keep PII recoverable under tight auth without putting it in the clear.

### D7. Daily Merkle root + external attestation.

- At **03:00 local time**, a `attestation-publisher` job runs:
  1. Computes the Merkle root over the day's immudb entries (using immudb's own consistency proof primitives).
  2. Builds an attestation document.
  3. Signs the document with an attestation signing key (separate from the appender key; stored in Vault transit; key ID rotates quarterly).
  4. Commits and pushes the document to `rky-2023/openclaw-attestations`.

- Attestation file layout in the public repo:

  ```
  openclaw-attestations/
    README.md                          (verification instructions)
    attestations/
      2026/
        05/
          23.json
          24.json
          ...
    keys/
      attestation-2026-q2.pub          (Ed25519 public keys, ETag-rotated quarterly)
    verify/
      verify.py                        (standalone verifier, no openclaw deps)
  ```

- Attestation document (`attestations/YYYY/MM/DD.json`):

  ```jsonc
  {
    "version":     1,
    "date":        "2026-05-23",
    "entries":     12847,
    "first_ulid":  "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    "last_ulid":   "01ARZ3RJ8K9XKQX0V0F1J1XJX1",
    "merkle_root": "sha256:<hex>",
    "prev_day_merkle": "sha256:<hex>",            // chains to yesterday's attestation
    "policy_hash": "sha256:<hex of redaction.rego at publish time>",
    "signed_by":   "attestation-2026-q2",
    "signature":   "ed25519:<hex>"
  }
  ```

- The `prev_day_merkle` chains the daily attestations into a single Git-side hash chain, so a "we never published yesterday" attack is detectable: today's attestation either references yesterday's published root or is mis-formed.

### D8. Verification: `oc audit verify` and a standalone `verify.py`.

Two verification paths:

- **Operator-side (`oc audit verify <date>`):** queries local immudb, recomputes the day's root, fetches the corresponding `attestations/YYYY/MM/DD.json` from the public repo, compares roots and signatures. Returns green/red. Designed to be run on a schedule (weekly random-day verify, see PLAN.md Phase 11).
- **Third-party-side (`openclaw-attestations/verify/verify.py`):** a standalone Python script that walks the attestations repo and checks the Git-side hash chain (`prev_day_merkle`) + signatures. Has no openclaw dependencies, no Vault access, no immudb access. Anyone who clones the public repo can run it. This makes the tamper-evidence property externally checkable.

### D9. Retention and tiering.

| Layer | Hot | Cold | Archive |
|---|---|---|---|
| Roots (Merkle + attestation docs) | Forever in Git history | Forever in Git history | — |
| Envelopes (full, in immudb) | 90 days | Compacted into 7-day Merkle aggregates after 90 days; full envelopes moved to MinIO | After 2 years, MinIO objects moved to B2 archive tier (still verifiable via root chain) |
| Encrypted PII blobs | 90 days | Same lifecycle as envelopes; key version pinned so blobs remain decryptable across rotations | Same |
| Postgres projection | 30 days | Drop oldest partitions monthly; can be replayed from immudb | — |

Roots and attestation documents are tiny (a few hundred bytes each) and stay in Git forever. Full payloads age out, but the chain of roots remains, so any historical date is still verifiable against its published root even after the payloads are gone.

### D10. Replay: `oc audit replay <conv_id>` reconstructs A2A conversations.

- The CLI reads all entries with the given `conv_id` from immudb (sorted by ULID).
- Decrypts `encrypted_blobs[]` on the fly if the operator has a fresh YubiKey touch; otherwise renders placeholders.
- Outputs the human-readable "narrative" view (the ladder diagram shown in PLAN.md Phase 2). The viewer UI consumes the same underlying API.

### D11. Performance budget.

- Bus message rate (steady state, single user): ~5/s typical, ~50/s burst.
- Appender throughput: must sustain ≥200/s with p95 < 50 ms append latency.
- immudb commit p95 < 30 ms for entries up to 16 KiB.
- Postgres projection lag p95 < 2 s behind immudb.
- Daily Merkle compute (D7): < 60 s for up to 1 M entries/day.

These are well within immudb's published numbers; we are not pushing it.

---

## Consequences

### Positive

- A T4 (root-on-box) attacker who rewrites immudb must also rewrite *both* the local Postgres projection *and* the public attestations Git history. The latter is owned by GitHub and witnessed by anyone who has cloned it; rewriting it is detectable from outside.
- Per-entry verification (immudb's intrinsic Merkle tree + our `prev_hash` chain + the daily attestation) means a hypothetical immudb-side bug can't silently lose history without one of three layers complaining.
- Redaction happens before append, so we keep the WORM property *and* avoid storing secrets in clear text.
- The verification tool is third-party-runnable. No one has to trust openclaw to verify openclaw's claims — they need only the public Git repo.

### Negative

- Two-phase append means a brief window where a NATS event has fired but the immudb entry hasn't landed. The audit appender lag SLO (D4) keeps this small; paranoid mode (D4) bounds the worst case.
- The redaction policy is itself security-critical. A buggy redaction rule that *fails open* (i.e., doesn't redact a secret it should) would leak the secret into the audit log forever. We mitigate with: policy unit tests in CI, version-pinning per envelope, and a regular review.
- A daily Merkle compute over a million entries (D11) is achievable but not free. If we ever exceed ~5 M entries/day, we will need to chunk the daily compute or precompute incrementally.
- Quarterly rotation of the attestation signing key means a verifier needs to walk the `keys/` directory and pick the right key per date. The `verify.py` script handles this; humans inspecting the JSON by hand may be briefly confused.

### Neutral

- We are committing to immudb for the audit ledger. immudb is open-source, well-maintained, and has decent community traction, but it is far less commodity than Postgres. We accept the smaller ecosystem in exchange for the cryptographic verification primitives.

---

## Alternatives considered

### A1. Postgres with an `INSERT`-only role.

Rejected. "INSERT-only" is a permission, not a storage property. A role can be changed; a database can be `pg_dump`-ed and restored from a tampered dump. WORM at the application layer is not WORM at the storage layer.

### A2. S3 with Object Lock (governance mode).

Considered for the *archive* tier; not for the live ledger. Object Lock prevents deletion but provides no per-entry verification — we'd still need a per-entry hash chain on top, at which point immudb's intrinsic Merkle tree is the cheaper path. Object Lock may show up later as the archive backend for envelopes (D9).

### A3. Append-only Git repo for the full ledger (not just roots).

Rejected. Git history is hash-chained and append-only by convention, but Git is not designed for hundreds of thousands of commits per day. Storage costs and clone times become absurd quickly. The chosen design uses Git *only* for the daily roots, which are dozens of bytes each.

### A4. Cloud-hosted audit log (Datadog Audit Trail, AWS CloudTrail-equivalent).

Rejected. Adds an external dependency that sees all openclaw activity in the clear. Even with E2E-encrypted entries, the metadata exfiltration alone violates the principle "audit is owned by the operator." Out of scope per ADR-001.

### A5. Sigstore Rekor (transparency log) as the public attestation.

Considered. Rekor is purpose-built for this and gives stronger cryptographic guarantees (verifiable inclusion proofs via Trillian) than a plain Git repo. Deferred — adding sigstore now is overshoot for v1; we revisit if the threat model evolves to require a transparency log proper. The current Git-based attestation is upgradeable to Rekor without breaking the verification tool's external interface.

### A6. Single-signature audit entries (only `sig_appender`).

Rejected. Two signatures (`sig_service` + `sig_appender`) means forgery requires compromising two separate signing keys in two separate Vault namespaces. The marginal cost is one Ed25519 signature per entry, which is negligible.

### A7. Redact-after-append (i.e., keep raw, render redacted on read).

Rejected. Storing raw secrets and "promising" to redact on read is the failure mode every audit pipeline post-mortem ever has been about. Drop secrets before append; keep PII via the explicit `encrypt` path.

### A8. Synchronous append (block the originating service until immudb commits).

Rejected. Couples ledger availability to request latency. NATS-first decouples them. If immudb has a bad day, requests keep flowing; the ledger catches up; paranoid mode triggers only on sustained failure (D4).

---

## Open questions

- The attestation signing key rotation cadence is set to quarterly (D7), matching the GitHub App key (ADR-002 D12). If `verify.py` ergonomics suffer at quarter boundaries, consider moving to annual.
- Whether to publish a public `latest.json` symlink-equivalent in the attestations repo for easier "current root" lookup, or to require verifiers to walk by date. Trade-off: convenience vs. one more file to keep consistent.
- Whether to add a sigstore Rekor mirror as a secondary attestation channel once the primary Git-based channel is stable. Not blocking.
- The Postgres projection schema (D5) is intentionally underspecified here; the concrete migrations land alongside Phase 2 implementation.

---

## Change log

- **2026-05-23 (v1)** — Accepted as drafted.
