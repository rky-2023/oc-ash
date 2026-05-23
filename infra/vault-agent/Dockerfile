# vault-agent sidecar base image — Phase 1 task 1.12
#
# Used as a sidecar to every openclaw service that needs Vault-managed
# secrets or auto-rotated mTLS leaf certs. The service mounts:
#   /etc/openclaw/vault-agent.hcl      (rendered per-service config)
#   /etc/openclaw/role-id              (file containing the role_id)
#   /etc/openclaw/secret-id            (file containing a secret_id)
#   /etc/openclaw/ca.crt               (Vault's CA chain — bind-mount from /mnt/openclaw/shared/)
# And shares with the parent service:
#   /run/openclaw/                     (tmpfs — rendered cert/key/token)
#
# Future hardening (Phase 10): pin the vault image by sha256 digest +
# cosign-verify on every pull.

FROM hashicorp/vault:1.18.0

# Run as non-root (the official image already creates user 'vault'
# with uid 100, gid 1000). No extra privileges needed.
USER vault

ENTRYPOINT ["vault", "agent"]
CMD ["-config=/etc/openclaw/vault-agent.hcl"]
