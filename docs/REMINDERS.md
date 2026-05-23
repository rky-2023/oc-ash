# Reminders (operator-pending items)

These are operator actions that have been intentionally deferred. They are *not* tasks for code; they are things rky must do, but at a later moment than "now."

Format: each item names **when** to re-surface it, **what** to do, and **why** it exists.

---

## Active reminders

### R-001 — Invite external read-only witnesses to `openclaw-attestations`

- **When to act:** After Phase 2 task 2.13 ships (attestation publisher writes its first daily root to the private attestations repo).
- **What to do:** Invite 1–2 trusted external GitHub accounts as **read-only** collaborators on `rky-2023/openclaw-attestations`. Candidates: a second personal account in a different geography, a trusted family member, a friend who runs their own home server. Alternatively (or additionally): set up a daily cron on a separate machine that clones the repo via deploy key and notifies rky if today's published root diverges from local immudb's root.
- **Why:** ADR-001 R2 chose a private attestations repo. The private choice trades the "globally observable tampering" property for reduced metadata exposure. Inviting external witnesses restores most of the global-witness property to a small, named audience — making silent ledger tampering observable to someone other than the operator.
- **Status:** Deferred. The system is fully functional without this; it's a hardening step.

### R-002 — Migrate from platform authenticator to YubiKeys

- **When to act:** When 2× YubiKey 5C NFC are in hand (no specific date — operator-budgeted).
- **What to do:** Follow Phase 1 runbook task 1.9b and 1.10b. Enroll YubiKey #1 first, then #2. Test login from each YubiKey independently. Optionally revoke the platform authenticators or leave one as a third backup.
- **Why:** ADR-002 D13 (interim auth mode) is documented as a temporary deviation from D2 (two YubiKeys). The platform authenticator is hardware-backed (Secure Enclave / TPM) and phishing-resistant via origin binding, but lacks the portability and geographic-separation properties of two YubiKeys. Migration restores full D2/D3 compliance.
- **Status:** Active interim — ADR-002 D13 covers this state.

### R-003 — Enable "Require status checks to pass" on `oc-ash` branch protection

- **When to act:** After Phase 2 task 2.15 ships and CI workflows produce named checks (e.g., `ci/lint`, `ci/typecheck`, `ci/test`, `ci/opa-test`).
- **What to do:** Edit the `main-protected` ruleset on `oc-ash`, check "Require status checks to pass before merging," add the published check names.
- **Why:** Branch protection is configured but the status-check rule is currently disabled because no checks exist yet. Re-enabling it once CI lands closes the "merge a broken build" gap.
- **Status:** Deferred until CI workflows exist.

---

## Completed reminders

(Move items here once they've been actioned. Keeps a record of what we said we'd do vs. what happened.)

*(none yet)*

---

## Change log

- **2026-05-23** — File created. R-001 (attestations witnesses), R-002 (YubiKey migration), R-003 (CI status checks) added.
