# docs/

Project documentation.

**Files:**
- `THREAT_MODEL.md` — STRIDE analysis, adversary tiers, residual risks. Reviewed quarterly.
- `RUNBOOK.md` — break-glass operations: rotate YubiKey, rotate Vault root, recover from immudb corruption, regenerate FCM server key, rotate GitHub App key. (Phase 12.)
- `ADR/` — Architecture Decision Records, one per decision. ADRs are immutable once accepted; supersede via a new ADR that links the old one.
- `phases/` — per-phase implementation runbooks. Each file (`phase-N.md`) contains the concrete task list, exit criteria, and rollback procedures for that phase. ADRs say *what and why*; runbooks say *how, in what order, and how to verify*.

**ADR conventions:**
- Filename: `ADR-NNN-kebab-case-title.md` (zero-padded).
- Status: Proposed | Accepted | Superseded by ADR-XXX | Deprecated.
- Sections: Context · Decision · Consequences · Alternatives Considered.

**Convention:** every non-trivial PR links to one or more ADRs in its description. PRs that contradict an accepted ADR must first land an ADR superseding it.
