-- Phase 2 task 2.1 — openclaw schema in the Ashboard Postgres instance.
--
-- Apply to the same Postgres that already serves Ashboard (per ADR-001 D1
-- — shared data plane, isolated application). Run as the Postgres superuser
-- (apply with the wrapper script infra/scripts/06-apply-postgres-schema.sh,
-- or manually: `docker exec -i <ashboard-postgres> psql -U postgres < this.sql`).
--
-- Idempotent: all CREATE statements are `IF NOT EXISTS` or DO blocks that
-- check existence. Safe to re-apply.

-- ---------------------------------------------------------------------
-- Role for the openclaw application
-- ---------------------------------------------------------------------
-- We create the role with NOLOGIN initially; the password is set in a
-- separate step by infra/scripts/06-apply-postgres-schema.sh (because
-- the password is generated and stored in Vault, not committed here).
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'openclaw_app') THEN
    CREATE ROLE openclaw_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT;
  END IF;
END
$$;

-- ---------------------------------------------------------------------
-- The schema itself
-- ---------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS openclaw AUTHORIZATION openclaw_app;

-- The app role can do anything inside its schema.
GRANT USAGE ON SCHEMA openclaw TO openclaw_app;
GRANT ALL ON SCHEMA openclaw TO openclaw_app;

-- Make the schema the default for the app role so unqualified table
-- names resolve here.
ALTER ROLE openclaw_app SET search_path = openclaw, public;

-- ---------------------------------------------------------------------
-- openclaw.lookup — low-value, public-by-design config (per ADR-002 D12)
-- ---------------------------------------------------------------------
-- Values that are NOT secrets but ARE configuration: OAuth client IDs,
-- GitHub App IDs / installation IDs, FCM project IDs, WebAuthn RP IDs,
-- redaction policy hashes, repo configs, etc.
--
-- Real secrets (transit signing keys, OAuth refresh tokens, DB passwords)
-- live in Vault under kv/openclaw/* per ADR-002 D8.
CREATE TABLE IF NOT EXISTS openclaw.lookup (
  key         TEXT PRIMARY KEY,
  value       JSONB NOT NULL,
  description TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by  TEXT NOT NULL DEFAULT current_user
);

COMMENT ON TABLE openclaw.lookup IS
  'Public-by-design config (per ADR-002 D12). NOT secrets. '
  'OAuth client IDs, GitHub App IDs, FCM project IDs, WebAuthn RP config, '
  'redaction policy hashes, repo configs, notification rules, feature flags.';

-- ---------------------------------------------------------------------
-- Seed: the keys subsequent phases will populate
-- ---------------------------------------------------------------------
-- Insert placeholder rows so the keys are discoverable. Values will be
-- updated by later phases (Phase 5 wiring Google Calendar OAuth, Phase 7
-- wiring the GitHub App, etc.). Each row uses ON CONFLICT DO NOTHING so
-- re-running this file doesn't overwrite operator-edited values.
INSERT INTO openclaw.lookup (key, value, description) VALUES
  ('webauthn.rp_id',
   '"localhost"'::jsonb,
   'WebAuthn relying-party ID — overridden per-environment via OC_WEBAUTHN_RP_ID env. localhost for dev; tailnet hostname when run via core/scripts/run-tls.sh.'),
  ('webauthn.rp_name',
   '"openclaw"'::jsonb,
   'WebAuthn relying-party display name.'),
  ('oauth.google.calendar.client_id',
   '""'::jsonb,
   'Google OAuth client ID for Calendar — populated in Phase 5.'),
  ('oauth.google.gmail.client_id',
   '""'::jsonb,
   'Google OAuth client ID for Gmail — populated in Phase 6.'),
  ('github_app.openclaw_bot.app_id',
   '""'::jsonb,
   'GitHub App ID for openclaw-bot — populated in Phase 2 task 2.12 / Phase 7.'),
  ('github_app.openclaw_bot.installation_ids',
   '[]'::jsonb,
   'GitHub App installation IDs per repo — populated as App is installed.'),
  ('fcm.project_id',
   '""'::jsonb,
   'Firebase Cloud Messaging project ID — populated in Phase 8.'),
  ('redaction_policy.version',
   '"v1"'::jsonb,
   'Current audit redaction policy version — bumped when policy/redaction.rego changes.'),
  ('redaction_policy.sha256',
   '""'::jsonb,
   'SHA-256 of policy/redaction.rego at audit-appender startup.')
ON CONFLICT (key) DO NOTHING;

-- Default privileges so any FUTURE objects in openclaw.* are accessible
-- to openclaw_app without requiring a re-grant per table.
ALTER DEFAULT PRIVILEGES IN SCHEMA openclaw
  GRANT ALL ON TABLES TO openclaw_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA openclaw
  GRANT ALL ON SEQUENCES TO openclaw_app;

-- ---------------------------------------------------------------------
-- Note: the audit projection tables (openclaw.audit_entries,
-- openclaw.audit_conversations, openclaw.audit_policy_decisions) land
-- in Phase 2 task 2.9 (audit-projector). They're NOT created here so
-- that this DDL stays small + focused on the lookup table.
-- ---------------------------------------------------------------------
