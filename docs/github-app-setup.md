# GitHub App setup — `openclaw-bot`

One-time manual steps required before running `oc attest publish`.
Everything after the App is created is scripted (`11-github-app-bootstrap.sh`).

---

## 1. Create the GitHub App

1. Go to **github.com → Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Fill in:
   - **App name:** `openclaw-bot`
   - **Homepage URL:** `https://github.com/rky-2023/openclaw-attestations`
   - **Webhooks — Active:** ☐ (unchecked — Phase 2 doesn't consume events)
3. Set permissions:
   - **Repository permissions → Contents:** `Read and write`
   - All other permissions: `No access`
4. Set **Where can this GitHub App be installed?** to **Only on this account**.
5. Click **Create GitHub App**.

Note the **App ID** shown at the top of the settings page. You'll need it.

---

## 2. Generate a private key

On the App settings page, scroll to **Private keys** → **Generate a private key**.

A `.pem` file is downloaded automatically. Do **not** put it in git.

---

## 3. Install the App on the attestations repo

1. On the App settings page, go to **Install App** (left sidebar).
2. Click **Install** next to your account.
3. Choose **Only select repositories** → select `openclaw-attestations`.
4. Click **Install**.

After installing, the URL will be:
```
https://github.com/settings/installations/<installation_id>
```
The integer in the URL is the **Installation ID**.

---

## 4. Store credentials in Vault

Run the bootstrap script from the repo root:

```bash
./infra/scripts/11-github-app-bootstrap.sh
```

You'll be prompted for:
- **GitHub App ID** — the integer from step 1
- **GitHub Installation ID** — the integer from step 3
- **Path to the downloaded .pem file** — e.g. `~/Downloads/openclaw-bot.2026-05-25.private-key.pem`

The script writes all three values to Vault KV and then **shreds the local PEM**.

---

## 5. Verify

```bash
docker exec -i oc-vault vault kv get kv/openclaw/github-app/openclaw-bot/app-id
docker exec -i oc-vault vault kv get kv/openclaw/github-app/openclaw-bot/installation-id
```

Both should return values. The `private-key-pem` path exists in Vault but is intentionally
not echoed in the verification step.

---

## 6. Test publish

```bash
./core/scripts/oc-with-vault-creds.sh attest publish --date 2026-05-24 --json
```

Expected output: an attestation JSON with `merkle_root`, `entry_count`, and `commit_sha`.
Check `github.com/rky-2023/openclaw-attestations` for the committed file.

---

## Key paths in Vault

| Path | Field | Content |
|------|-------|---------|
| `kv/openclaw/github-app/openclaw-bot/app-id` | `value` | GitHub App ID (integer) |
| `kv/openclaw/github-app/openclaw-bot/installation-id` | `value` | Installation ID |
| `kv/openclaw/github-app/openclaw-bot/private-key-pem` | `value` | RSA-2048 PEM |

The PEM is kept **only in Vault KV**. Access is gated by the openclaw-admin AppRole
(token TTL 15 min). Phase 3 will move to a vault-agent sidecar rendering it to tmpfs.
