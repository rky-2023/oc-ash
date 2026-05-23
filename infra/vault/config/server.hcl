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
  address     = "0.0.0.0:8200"

  # Bootstrap listener: TLS disabled because we mTLS at the docker-compose
  # network level and only localhost-bind the port. Phase 1 task 1.7 issues
  # an internal-CA cert and the listener config is amended to enable TLS
  # with that cert.
  tls_disable = true
}

api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"

# Audit device enabled programmatically in Phase 1 task 1.4 (we can't enable
# audit before init, and init happens inside the running container).
