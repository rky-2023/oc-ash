"""Runtime configuration for openclaw-core.

Loaded from environment variables prefixed `OC_*`. Non-secret values
(URLs, hostnames, ports, env name) come from the env directly. Secret
values (Vault tokens, DB passwords, OAuth refresh tokens, signing keys)
are NOT in env; vault-agent renders them as files into `secrets_dir`
and the code reads them at use-time, never caching.

See ADR-002 D8 (Vault AppRole + vault-agent sidecar) and D12 (the
secret-vs-config split) for the policy this code implements.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration."""

    model_config = SettingsConfigDict(
        env_prefix="OC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Deployment ────────────────────────────────────────────────────
    env: str = Field(default="development", description="development | staging | production")
    log_level: str = Field(default="info", description="debug | info | warning | error")

    # ── Vault ─────────────────────────────────────────────────────────
    vault_addr: str = Field(default="http://127.0.0.1:8200")
    # Token populated by run-with-vault-creds.sh (AppRole login).
    # Vault-agent sidecar (task 1.12) will replace this with a
    # short-lived file-rendered token in production.
    vault_token: str = Field(default="")
    # TLS verification for the Vault client. In the containerised path
    # core reaches Vault over the plain-HTTP internal listener; on the
    # host MVP path it must use the TLS listener on 127.0.0.1, whose cert
    # is signed by the internal CA. Set OC_VAULT_CACERT to that CA bundle
    # to verify properly, or OC_VAULT_VERIFY=false to skip verification
    # for the loopback connection (acceptable: same-host, no MITM surface).
    vault_cacert: str = Field(default="")
    vault_verify: bool = Field(default=True)

    # ── Vault transit signing (ADR-002 D12) ───────────────────────────
    vault_transit_key_service: str = Field(default="audit-service")
    vault_transit_key_appender: str = Field(default="audit-appender")
    # Set to false in unit tests so they don't need a live Vault.
    vault_signing_enabled: bool = Field(default=True)

    # ── Event bus ─────────────────────────────────────────────────────
    nats_url: str = Field(default="nats://nats:4222")

    # ── Audit ledger ──────────────────────────────────────────────────
    immudb_host: str = Field(default="immudb")
    immudb_port: int = Field(default=3322)

    # ── Policy ────────────────────────────────────────────────────────
    opa_url: str = Field(default="http://opa:8181")

    # ── Audit redaction (ADR-003 D6, task 2.7) ───────────────────────
    # Redaction runs in the appender before the WORM write. Disable in unit
    # tests that don't have a live OPA/Vault (the version is still pinned).
    redaction_enabled: bool = Field(default=True)
    # Vault transit key that wraps per-message PII data keys (created by
    # 04-vault-bootstrap.sh as `audit-pii-v3`, aes256-gcm96).
    vault_transit_key_pii: str = Field(default="audit-pii-v3")
    # Path to redaction.rego, hashed into each envelope's redaction_version.
    # Container default is the compose mount; the host MVP path overrides via
    # OC_REDACTION_POLICY_PATH in run-with-vault-creds.sh.
    redaction_policy_path: Path = Field(default=Path("/policy/redaction.rego"))

    # ── Postgres ──────────────────────────────────────────────────────
    # Preferred: set OC_POSTGRES_DSN directly (run-with-vault-creds.sh
    # fetches it from Vault and exports it). Fallback: vault-agent renders
    # the DSN to a file and core reads it at startup.
    postgres_dsn: str = Field(default="")
    postgres_dsn_file: Path = Field(default=Path("/run/secrets/openclaw/postgres.dsn"))

    # ── Audit projector ───────────────────────────────────────────────
    enable_audit_projector: bool = Field(
        default=True,
        description="Run the audit-projector background task inside core.",
    )
    projector_poll_seconds: int = Field(
        default=5,
        description="Seconds between immudb scan cycles.",
    )

    @property
    def effective_postgres_dsn(self) -> str:
        if self.postgres_dsn:
            return self.postgres_dsn
        if self.postgres_dsn_file.exists():
            return self.postgres_dsn_file.read_text().strip()
        return ""

    # ── Where vault-agent renders secrets ─────────────────────────────
    secrets_dir: Path = Field(default=Path("/run/secrets/openclaw"))

    # ── WebAuthn / Phase 1 task 1.8 ───────────────────────────────────
    # RP_ID is the public-effective-suffix domain the browser sees.
    # For local-only development use "localhost" (WebAuthn spec exempts
    # localhost from HTTPS). For tailnet use e.g. "oc.<tailnet>.ts.net".
    # Phone-via-QR enrollment requires a real HTTPS endpoint (Phase 1+
    # follow-up: Tailscale HTTPS).
    webauthn_rp_id: str = Field(default="localhost")
    webauthn_rp_name: str = Field(default="openclaw")
    # CSV of origins the browser may legitimately come from.
    webauthn_expected_origins_csv: str = Field(default="http://localhost:8000")

    # SQLite-backed auth store. Phase 2 task 2.1 migrates to Postgres.
    auth_db_path: Path = Field(default=Path("/var/lib/openclaw-core/auth.db"))

    @property
    def webauthn_expected_origins(self) -> list[str]:
        return [s.strip() for s in self.webauthn_expected_origins_csv.split(",") if s.strip()]

    # ── immudb — populated by core/scripts/run-with-vault-creds.sh ────
    # (or by hand: export OC_IMMUDB_PASSWORD=<from-vault>).
    # Phase 2 task 2.5b will fetch these via a vault-agent sidecar.
    immudb_user: str = Field(default="appender")
    immudb_password: str = Field(default="")
    immudb_database: str = Field(default="openclaw_audit")
    enable_audit_appender: bool = Field(
        default=True,
        description="Run the audit-appender background task inside core. Disable in tests.",
    )

    # ── GitHub App (tasks 2.12 / 2.13) ───────────────────────────────
    # App ID and installation ID are stored in Vault KV by the bootstrap
    # script (11-github-app-bootstrap.sh) and exported by oc-with-vault-creds.sh.
    github_app_id: str = Field(default="")
    github_installation_id: str = Field(default="")
    # PEM rendered by vault-agent or exported by oc-with-vault-creds.sh.
    github_app_private_key_file: Path = Field(
        default=Path("/run/secrets/openclaw/github-app.pem")
    )
    # String override (PEM verbatim) — for manual invocations and tests.
    github_app_private_key: str = Field(default="")
    github_attestations_repo: str = Field(default="rky-2023/openclaw-attestations")
    github_api_url: str = Field(default="https://api.github.com")

    # ── Phase 3 event ingestion (oc-ingest worker) ───────────────────
    # MVP: the gh-poller + claude-hook receiver run in-process inside core,
    # reusing the audit envelope/signer/bus primitives (like the appender).
    # Standalone-service split (own AppRole/mTLS) is Phase 11 (ADR-003 D3).
    enable_ingest_worker: bool = Field(
        default=False,
        description="Run the oc-ingest worker (gh-poller + claude-hook receiver) inside core. Off by default; enable on the host.",
    )
    ingest_socket_path: Path = Field(default=Path("/run/openclaw/ingest.sock"))
    ingest_sessions_dir: Path = Field(default=Path("/home/asher/openclaw/sessions"))
    # JSON array of {"owner","repo"} the gh-poller watches; empty → defaults.
    ingest_gh_repos: str = Field(default="")

    @property
    def effective_github_app_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_file.exists():
            return self.github_app_private_key_file.read_text().strip()
        return ""


# Singleton — instantiated once on import. Don't recreate; mutate the env
# instead and restart the process if you need to change config (intentional).
settings = Settings()
