# Vault server config — Phase 1 task 1.2
#
# Storage: integrated Raft on the dm-crypt-mounted /vault/data volume.
# Listener: localhost only via docker port mapping; never internet-exposed.
# Auto-unseal: NONE — manual Shamir-only unseal per ADR-001 R1.

ui = false

disable_mlock = false   # keep mlock on (the default) so secrets don't swap

storage "raft" {
  path    = "/vault/data"
  node_id = "openclaw-vault-1"
}

listener "tcp" {
  address       = "0.0.0.0:8200"

  # TLS enabled by Phase 1 task 1.7 (infra/scripts/05-vault-tls-listener.sh).
  # Cert + key + CA chain live at /vault/data/tls/ inside the container,
  # backed by the dm-crypt-mounted /mnt/openclaw/vault volume on the host.
  # 30-day TTL on the cert; re-issue monthly via the same 05 script.
  tls_cert_file = "/vault/data/tls/server.crt"
  tls_key_file  = "/vault/data/tls/server.key"
  # tls_client_ca_file kept commented for now — uncomment when downstream
  # services should be required to present client certs for mTLS to Vault
  # itself. Until vault-agent is wired up across all services, leaving
  # mTLS off allows the initial bootstrap with AppRole over TLS.
  # tls_client_ca_file = "/vault/data/tls/ca.crt"
  tls_disable   = false

  # TLS 1.3 only per ADR-002 D6.
  tls_min_version = "tls13"
}

api_addr     = "https://127.0.0.1:8200"
cluster_addr = "https://127.0.0.1:8201"

# Audit device enabled programmatically in Phase 1 task 1.4 (we can't enable
# audit before init, and init happens inside the running container).
