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

    # ── Event bus ─────────────────────────────────────────────────────
    nats_url: str = Field(default="nats://nats:4222")

    # ── Audit ledger ──────────────────────────────────────────────────
    immudb_host: str = Field(default="immudb")
    immudb_port: int = Field(default=3322)

    # ── Policy ────────────────────────────────────────────────────────
    opa_url: str = Field(default="http://opa:8181")

    # ── Postgres (lookup schema only; the audit projection lives here too) ──
    # Connection string is rendered by vault-agent into a file in secrets_dir;
    # this field is the *path* to that file, not the URL itself.
    postgres_dsn_file: Path = Field(default=Path("/run/secrets/openclaw/postgres.dsn"))

    # ── Where vault-agent renders secrets ─────────────────────────────
    secrets_dir: Path = Field(default=Path("/run/secrets/openclaw"))


# Singleton — instantiated once on import. Don't recreate; mutate the env
# instead and restart the process if you need to change config (intentional).
settings = Settings()
