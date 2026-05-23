# `infra/vault-agent/`

vault-agent sidecar base image + per-service config template — Phase 1 task 1.12.

Every openclaw service that needs Vault-managed secrets or auto-rotated mTLS leaf certs runs `vault-agent` as a sidecar container next to it. The sidecar:

1. Authenticates to Vault via the service's **AppRole** (role_id + secret_id files mounted from the host).
2. Caches the resulting Vault token at `/run/openclaw/token` for the service to use for any direct Vault API calls.
3. Renders a fresh mTLS leaf cert + key (24h TTL, rotated every 12h) into `/run/openclaw/tls.{crt,key}`.
4. `SIGHUP`s the service process on rotation so it reloads.

This implements ADR-002 D6 (mTLS via Vault PKI) and D8 (AppRole + vault-agent sidecar) for every downstream openclaw service.

---

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Base image: `hashicorp/vault:1.18.0`, runs `vault agent -config=/etc/openclaw/vault-agent.hcl`. Non-root (uid 100). |
| `vault-agent.hcl.template` | Vault Agent config template with `@@SERVICE_NAME@@` and `@@NAMESPACE@@` placeholders. Renders per service with `sed`. |

---

## Per-service usage pattern (illustrative — wired up in Phase 2+)

1. **Create an AppRole for the service** (run from `openclaw-admin` AppRole token):

   ```sh
   # Inside the running oc-vault container, with VAULT_TOKEN set to the
   # 15-min admin token from `auth/approle/login`:
   vault write auth/approle/role/<service-name> \
     token_policies="<service-name>-policy" \
     token_ttl=15m \
     token_max_ttl=1h \
     bind_secret_id=true
   ```

2. **Generate one-time role_id + secret_id**:

   ```sh
   ROLE_ID=$(vault read -field=role_id auth/approle/role/<service-name>/role-id)
   SECRET_ID=$(vault write -force -field=secret_id auth/approle/role/<service-name>/secret-id)
   ```

3. **Place credentials at the service's host bind-mount source**:

   ```sh
   sudo install -d -m 0700 -o 100 -g 1000 /mnt/openclaw/<service-name>/credentials
   echo -n "$ROLE_ID"   | sudo tee /mnt/openclaw/<service-name>/credentials/role-id    >/dev/null
   echo -n "$SECRET_ID" | sudo tee /mnt/openclaw/<service-name>/credentials/secret-id  >/dev/null
   sudo chmod 0600 /mnt/openclaw/<service-name>/credentials/{role-id,secret-id}
   sudo chown 100:1000 /mnt/openclaw/<service-name>/credentials/*
   ```

4. **Render the agent config from the template**:

   ```sh
   sed \
     -e "s/@@SERVICE_NAME@@/<service-name>/g" \
     -e "s/@@NAMESPACE@@/<namespace>/g" \
     infra/vault-agent/vault-agent.hcl.template \
     > infra/vault-agent/configs/<service-name>.hcl
   ```

5. **Add the sidecar to the service's compose entry**:

   ```yaml
   services:
     <service-name>:
       # ... main service config ...
       volumes:
         - service-runtime:/run/openclaw   # tmpfs shared with sidecar

     <service-name>-vault-agent:
       build:
         context: ../infra/vault-agent
         dockerfile: Dockerfile
       depends_on:
         vault:
           condition: service_healthy
       networks:
         - oc-internal
       volumes:
         - service-runtime:/run/openclaw
         - ../infra/vault-agent/configs/<service-name>.hcl:/etc/openclaw/vault-agent.hcl:ro
         - /mnt/openclaw/shared/ca.crt:/etc/openclaw/ca.crt:ro
         - /mnt/openclaw/<service-name>/credentials/role-id:/etc/openclaw/role-id:ro
         - /mnt/openclaw/<service-name>/credentials/secret-id:/etc/openclaw/secret-id:ro
   ```

6. **Service consumes**:
   - `/run/openclaw/tls.crt` and `/run/openclaw/tls.key` for its own mTLS listener.
   - `/run/openclaw/ca.crt` to verify the certs of OTHER services it dials.
   - `/run/openclaw/token` for direct Vault API calls (KV reads, transit signs, etc.).

---

## Design notes

- **The agent runs as a separate container, not in-process with the service.** Process isolation: a compromised service can read the rendered files but not the AppRole secret_id (only the sidecar reads that). The sidecar has no application logic, so its attack surface is tiny.
- **`secret_id` is left on disk after reading** (`remove_secret_id_file_after_reading = false`). This is deliberate so the sidecar can re-auth across container restarts. Phase 11 hardening can flip this on once a separate sidecar-restart workflow exists (re-injection from a bootstrap token-on-TPM).
- **All shared paths are tmpfs (`/run/openclaw/`).** Cert/key/token never touch disk. Across reboots they re-render on agent start.
- **24h cert TTL, 12h render cadence.** That gives 12h of headroom before the cert would expire — services don't experience a hard cut-off.
- **`uri_sans` uses SPIFFE format** so future SPIRE integration is a drop-in (ADR-002 alternatives-considered A5).
- **`pki_int/issue/server` is the issuing endpoint.** Per ADR-002 D14 this rotates every annual CA rotation; the leaf certs keep their 24h cadence.

---

## What's NOT done in this PR

- No service is *using* the template yet. `core/` is still scaffold. Phase 2 task 2.5 wires `core` up with its own AppRole + agent sidecar.
- The deployment helper script that automates steps 1–4 above lands in the same Phase 2 PR.
- mTLS *enforcement* on the openclaw-core listener is Phase 2 task 2.5.
