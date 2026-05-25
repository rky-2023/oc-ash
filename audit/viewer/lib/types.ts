export interface EntryRow {
  ulid: string;
  ts: string;
  subject: string;
  conv_id: string | null;
  actor_kind: string;
  actor_id: string;
  action: string;
  direction: string;
  sig_service_valid: boolean | null;
  sig_appender_valid: boolean | null;
  chain_valid: boolean | null;
  redacted_payload: Record<string, unknown> | null;
  raw_envelope?: Record<string, unknown> | null;
}

export interface ConversationRow {
  conv_id: string;
  first_ulid: string;
  last_ulid: string;
  first_ts: string;
  last_ts: string;
  subject_prefix: string;
  entry_count: number;
  has_integrity_issues: boolean;
}

export interface VerifyResult {
  date: string;
  total: number;
  sig_service_ok: number;
  sig_appender_ok: number;
  chain_ok: number;
  status: "PASS" | "FAIL" | "NO_DATA";
}
