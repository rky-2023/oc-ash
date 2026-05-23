# .github/

GitHub-side configuration.

**Files (planned):**
- `workflows/ci.yml` — lint, type-check, unit tests, rego tests (`opa test`), Rust tests for fswatch, RN build for the Android app.
- `workflows/audit-anchor.yml` — verifies each PR's `audit-conversation-id` label resolves to a real immudb entry (Phase 7 gate).
- `workflows/sbom.yml` — generates and signs an SBOM with syft + cosign on every release.
- `workflows/renovate.yml` — Renovate config invocation.
- `CODEOWNERS` — rky on everything until further notice; the PR bot is never an owner.
- `branch-protection.yaml` — declarative branch protection for `main` (signed commits required, 1 review, no force-push).

**Bot accounts referenced:**
- `openclaw-bot` (GitHub App) — opens PRs; never has main push.
- `renovate[bot]` — dependency PRs.
