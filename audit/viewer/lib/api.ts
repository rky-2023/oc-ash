import type { ConversationRow, EntryRow, VerifyResult } from "./types";

const BASE = process.env.OC_API_BASE ?? "http://localhost:8000";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return res.json() as Promise<T>;
}

export const api = {
  entries: (limit = 200, offset = 0, subject?: string) => {
    const q = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (subject) q.set("subject", subject);
    return get<EntryRow[]>(`/api/audit/entries?${q}`);
  },
  entry: (ulid: string) => get<EntryRow>(`/api/audit/entries/${ulid}`),
  conversations: (limit = 50, offset = 0) =>
    get<ConversationRow[]>(`/api/audit/conversations?limit=${limit}&offset=${offset}`),
  conversation: (conv_id: string) =>
    get<EntryRow[]>(`/api/audit/conversations/${conv_id}`),
  verify: (date?: string) =>
    get<VerifyResult>(`/api/audit/verify${date ? `?date=${date}` : ""}`),
};
