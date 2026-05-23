# Security

**Status:** Stub. The full policy lands in Phase 10. This file exists so the threat envelope is on the record from day one.

## Honest scope

This project is a **self-hosted personal system** on commodity hardware. It does not claim, and never will claim, resistance to a determined nation-state targeting the operator individually. Any such claim on a single home server is dishonest.

What it does aim for, by design, is to make compromise **expensive** for:

- T1: unauthenticated internet randos and commodity botnets
- T2: phished session tokens / stolen browser cookies
- T3: a lost or stolen YubiKey (one of two)
- T4: a single compromised non-root user on the host

It does **not** aim to fully resist:

- T5: a targeted, well-resourced, persistent adversary with physical access, supply-chain leverage, or 0-day capability against the host kernel.

The full STRIDE table and per-tier mitigations are in [`docs/THREAT_MODEL.md`](./docs/THREAT_MODEL.md).

## Reporting

Until a public disclosure channel exists, security issues should be reported privately to the repo owner.

## Hardening summary (built in, not bolted on)

- Hardware-key gated identity (WebAuthn + YubiKey FIDO2). No password fallbacks.
- mTLS between every internal service; certs auto-rotate every 24 h via Vault.
- Default-deny OPA policy at the MCP proxy. Per-call authorization.
- WORM, hash-chained audit log (immudb) with nightly externally-published Merkle root.
- gVisor sandboxes around every MCP server. seccomp + read-only root + no-new-privileges + dropped caps.
- LUKS full-disk; secrets volume auto-locks after 5 minutes idle.
- Supply chain: pinned hashes, signed images (cosign), SBOMs per build.
- Runtime IDS (Falco) with alerts routed to the Android app.

## What is explicitly NOT in scope

- Hiding the *fact* of openclaw's existence from a network observer.
- Resisting a coerced operator (rubber-hose). The recovery path always exists.
- Defending against compromise of upstream Google / GitHub / Firebase. If those are compromised, we lose. Mitigation is scope minimization (smallest possible OAuth scopes) and audit visibility.
