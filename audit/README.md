# audit/

The audit ledger and its reader UI.

**Ledger:** [immudb](https://immudb.io) — a cryptographically-verifiable WORM database. Every message on `oc.*` subjects is mirrored here with a Merkle proof.

**Nightly attestation:** the day's ledger root hash is signed with an offline key and committed to a public Git repo (`openclaw-attestations`). Tampering with the ledger becomes globally visible.

**Subdirectories:**
- `viewer/` — Next.js read-only UI. Two views per conversation:
  - Structured: JSON tree.
  - Narrative: human-readable ladder diagram (see PLAN.md Phase 2 for the format).
- `cli/` — `oc audit tail | replay <conv_id> | verify <date>`.
- `projector/` — service that materializes a Postgres read model from immudb for fast UI queries. immudb is the source of truth; Postgres is denormalized cache.

**Design rule:** the audit log is append-only and signed. Read paths can be fast/loose; write paths must go through the appender.
