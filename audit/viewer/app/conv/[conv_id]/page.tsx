import { api } from "@/lib/api";
import type { EntryRow } from "@/lib/types";

function sigIcon(v: boolean | null) {
  if (v === true)  return "✓";
  if (v === false) return "✗";
  return "—";
}

export default async function ConvPage({ params }: { params: Promise<{ conv_id: string }> }) {
  const { conv_id } = await params;
  let entries: EntryRow[] = [];
  let err = "";
  try {
    entries = await api.conversation(conv_id);
  } catch (e) {
    err = String(e);
  }

  return (
    <main className="page">
      <h1>Conversation <span className="mono">{conv_id}</span></h1>
      <p className="mono" style={{ marginBottom: 20, color: "#8b949e" }}>
        {entries.length} envelopes
      </p>
      {err && <pre style={{ color: "#f85149" }}>{err}</pre>}
      <div className="ladder">
        {entries.map((e, i) => {
          const isLast = i === entries.length - 1;
          const issues = e.sig_service_valid === false || e.sig_appender_valid === false || e.chain_valid === false;
          return (
            <div key={e.ulid} className="ladder-entry">
              <div className="meta">
                {new Date(e.ts).toISOString().replace("T", " ").slice(0, 23)}Z
                {" · "}
                {e.actor_kind}:{e.actor_id}
                {" · "}
                svc:{sigIcon(e.sig_service_valid)} app:{sigIcon(e.sig_appender_valid)} chain:{sigIcon(e.chain_valid)}
                {issues && <span className="badge fail" style={{ marginLeft: 8 }}>integrity issue</span>}
              </div>
              <div>
                <span className="subj">{e.action.toUpperCase()}</span>
                {" · "}
                <span className="mono">{e.subject}</span>
                {" · "}
                <a href={`/entry/${e.ulid}`} className="mono">{e.ulid.slice(0, 10)}…</a>
              </div>
              {e.redacted_payload && (
                <pre style={{ marginTop: 4 }}>
                  {JSON.stringify(e.redacted_payload, null, 2)}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </main>
  );
}
