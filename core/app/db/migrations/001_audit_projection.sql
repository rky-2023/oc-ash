-- Phase 2 task 2.9 — audit projection tables.
--
-- Applied by infra/scripts/10-apply-audit-projection-schema.sh (idempotent).
-- The projector (core/app/audit/projector.py) reads from immudb and
-- writes here so that oc audit replay / verify are O(matches) instead
-- of O(all entries).
--
-- All tables live in the openclaw schema (created by 001_schema.sql in
-- task 2.1). The openclaw_app role already has full access to that schema.

-- ---------------------------------------------------------------------
-- openclaw.audit_entries — one row per immudb envelope
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS openclaw.audit_entries (
    ulid            TEXT        PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    subject         TEXT        NOT NULL,
    conv_id         TEXT,
    actor_kind      TEXT        NOT NULL,
    actor_id        TEXT        NOT NULL,
    action          TEXT        NOT NULL,
    direction       TEXT        NOT NULL,
    request_hash    TEXT,
    response_hash   TEXT,
    redacted_payload JSONB,
    prev_hash       TEXT,
    -- Signature verification results as recorded by the projector.
    -- False = bad sig; NULL = projector not yet checked (shouldn't happen).
    sig_service_valid   BOOLEAN,
    sig_appender_valid  BOOLEAN,
    chain_valid         BOOLEAN,
    -- Full envelope stored for the viewer's "raw" tab.
    raw_envelope    JSONB       NOT NULL,
    projected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_entries_conv_id
    ON openclaw.audit_entries (conv_id)
    WHERE conv_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_entries_ts
    ON openclaw.audit_entries (ts DESC);

CREATE INDEX IF NOT EXISTS idx_audit_entries_subject
    ON openclaw.audit_entries (subject);

-- ---------------------------------------------------------------------
-- openclaw.audit_conversations — one row per unique conv_id
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS openclaw.audit_conversations (
    conv_id         TEXT        PRIMARY KEY,
    first_ulid      TEXT        NOT NULL,
    last_ulid       TEXT        NOT NULL,
    first_ts        TIMESTAMPTZ NOT NULL,
    last_ts         TIMESTAMPTZ NOT NULL,
    subject_prefix  TEXT        NOT NULL,
    entry_count     INT         NOT NULL DEFAULT 1,
    has_integrity_issues BOOLEAN NOT NULL DEFAULT false,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- openclaw.audit_policy_decisions — one row per envelope with a policy
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS openclaw.audit_policy_decisions (
    id          BIGSERIAL   PRIMARY KEY,
    ulid        TEXT        NOT NULL REFERENCES openclaw.audit_entries (ulid),
    decision    TEXT        NOT NULL,
    rule        TEXT,
    ts          TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_policy_decisions_ulid
    ON openclaw.audit_policy_decisions (ulid);

-- ---------------------------------------------------------------------
-- openclaw.audit_checkpoint — singleton row tracking immudb scan head
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS openclaw.audit_checkpoint (
    id          INT         PRIMARY KEY DEFAULT 1,  -- singleton
    last_ulid   TEXT,           -- NULL means: start from the beginning
    last_ts     TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ensure the singleton row exists.
INSERT INTO openclaw.audit_checkpoint (id) VALUES (1)
    ON CONFLICT (id) DO NOTHING;
