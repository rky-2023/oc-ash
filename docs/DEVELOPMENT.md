# Development environment

How to set up a development environment for openclaw. Independent of running the system in production — that's `docs/phases/phase-N.md`.

---

## Prerequisites

- **Git ≥ 2.34** (for SSH commit signing). Modern Ubuntu / Debian / macOS / WSL packages are fine.
- **OpenSSH ≥ 8.0** (for `ssh-keygen -Y sign`).
- **Python 3.13+** (for `core/` development).
- **Docker + docker compose v2** (for bringing up infra locally).

---

## SSH commit signing (required — branch protection rejects unsigned commits)

The `main-protected` ruleset on `oc-ash` requires every commit on `main` to have a verified signature. PRs with unsigned commits are blocked from merging.

### Why a separate signing key

We use a **dedicated, passphrase-less SSH key** for commit signing, *separate from the auth key*:

| Key | File | Passphrase? | Role |
|---|---|---|---|
| Auth key | `~/.ssh/id_ed25519_rky2023` | Yes | Identifies the pusher to GitHub; used by `git push` |
| Signing key | `~/.ssh/oc_signing_key` | **No** | Signs commits; used by `git commit` |

The auth key has a passphrase (good — agent-cached for pushes). The signing key has no passphrase because git's subprocess invocation of `ssh-keygen -Y sign` doesn't reliably propagate `SSH_ASKPASS`, so a passphrase-protected signing key fails silently. Two keys, one role each, is the standard pattern.

### Generate the signing key (one-time setup)

```sh
ssh-keygen -t ed25519 -C "openclaw signing key" -f ~/.ssh/oc_signing_key -N ""
```

### Add the signing key to GitHub

Visit https://github.com/settings/keys → **New SSH key**.

Critical: set **Key type** to **Signing Key** (NOT Authentication Key). GitHub treats these as separate roles; an auth key in the signing slot won't verify commits.

Paste:

```sh
cat ~/.ssh/oc_signing_key.pub
```

After saving, the key appears under "Signing keys" on the same page.

### Configure git (per-repo, in `openclaw/`)

```sh
git config gpg.format ssh
git config user.signingkey ~/.ssh/oc_signing_key.pub
git config commit.gpgsign true
```

For local verification of signatures (so `git log --show-signature` and `%G?` work):

```sh
mkdir -p ~/.config/git
echo "rky2023@gmail.com $(cat ~/.ssh/oc_signing_key.pub)" >> ~/.config/git/allowed_signers
git config gpg.ssh.allowedSignersFile ~/.config/git/allowed_signers
```

### Verifying

After a commit, locally:

```sh
git log -1 --pretty=format:'%G? %h %s'
```

Should print `G` (good signature) followed by the commit hash and subject.

On GitHub, the commit on the PR should show a green **Verified** badge.

**Known confusing detail.** If you skip the `allowedSignersFile` step, `git log --show-signature` prints `error: gpg.ssh.allowedSignersFile needs to be configured and exist for ssh signature verification`. This is a *local verification* error, NOT a signing failure — the commit IS signed; you just can't verify it locally without the allow-list. GitHub verifies independently using the uploaded signing key.

---

## Branch + PR workflow

Direct pushes to `main` are blocked by the `main-protected` ruleset. Workflow:

1. Branch from `main` for any change:
   ```sh
   git checkout main && git pull
   git checkout -b feat/<short-description>
   ```
2. Commit (signed, per the section above).
3. Push the branch:
   ```sh
   git push -u origin feat/<short-description>
   ```
4. Open a PR on github.com (the `git push` output prints a "Create PR" URL).
5. Merge via **squash merge** (configured as the only allowed merge style).

### Authenticated pushes — one-shot ssh-agent

Because the auth key has a passphrase, `git push` over SSH needs the agent loaded. One-shot dance from the working directory:

```sh
ASKPASS=$(mktemp /tmp/oc-askpass-XXXXXX)
printf '#!/bin/sh\nprintf %%s "your-auth-key-passphrase"\n' > "$ASKPASS"
chmod 700 "$ASKPASS"
trap 'rm -f "$ASKPASS"; [ -n "$SSH_AGENT_PID" ] && ssh-agent -k >/dev/null 2>&1' EXIT
eval "$(ssh-agent -s)" >/dev/null
SSH_ASKPASS_REQUIRE=force SSH_ASKPASS="$ASKPASS" ssh-add ~/.ssh/id_ed25519_rky2023 < /dev/null >/dev/null 2>&1
git push -u origin <branch>
```

Better long-term: configure your shell to keep ssh-agent running and add the key with `ssh-add` once per login.

---

## Python development on `core/`

```sh
cd core/
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Run the scaffold locally without docker:

```sh
uvicorn app.main:app --reload --port 8000
curl http://127.0.0.1:8000/health
```

Most endpoints return `"not-yet-wired"` placeholders until Phase 2 implementation lands. The `/health` endpoint is fully functional.

### Linting + typing

```sh
ruff check .
ruff format --check .
mypy app
```

(Ruff + mypy are dev dependencies.)

---

## Docker compose

The compose file is intentionally **wired but not auto-starting** for `core`:

```sh
cd infra/

# Bring up just Vault first (and unseal it per Phase 1 task 1.3)
docker compose -f docker-compose.openclaw.yml up vault

# Bring up the bus + ledger + policy + storage (Phase 2 tasks 2.3/2.4)
docker compose -f docker-compose.openclaw.yml up immudb nats minio opa

# Bring up core (only after Phase 1 + Phase 2 setup is complete)
docker compose -f docker-compose.openclaw.yml --profile phase2-complete up core
```

The `phase2-complete` profile is a deliberate forcing function: you should NOT bring up `core` until:
1. Vault is unsealed and has the transit keys + AppRoles (Phase 1).
2. immudb has the `openclaw_audit` database and `appender`/`projector` users (Phase 2 task 2.3).
3. NATS streams are created per `infra/nats/streams.yaml` (Phase 2 task 2.4).
4. OPA has `policy/redaction.rego` loaded (Phase 2 task 2.7).

---

## Reminders

Operator-pending actions are tracked in [`REMINDERS.md`](./REMINDERS.md). Notable items affecting development:

- **R-002:** Migration from platform authenticator → YubiKeys for WebAuthn.
- **R-003:** Re-enable "Require status checks to pass" branch-protection rule once CI workflows land in Phase 2 task 2.15.
