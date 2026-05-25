import { api } from "@/lib/api";

function SigRow({ label, valid }: { label: string; valid: boolean | null }) {
  const cls = valid === true ? "ok" : valid === false ? "fail" : "na";
  const text = valid === true ? "VALID" : valid === false ? "INVALID" : "UNKNOWN";
  return (
    <tr>
      <td style={{ color: "#8b949e", width: 180 }}>{label}</td>
      <td><span className={`badge ${cls}`}>{text}</span></td>
    </tr>
  );
}

export default async function EntryPage({ params }: { params: Promise<{ ulid: string }> }) {
  const { ulid } = await params;
  let entry;
  let err = "";
  try {
    entry = await api.entry(ulid);
  } catch (e) {
    err = String(e);
  }

  if (err || !entry) {
    return (
      <main className="page">
        <h1>Entry not found</h1>
        <pre style={{ color: "#f85149" }}>{err || "Unknown error"}</pre>
      </main>
    );
  }

  return (
    <main className="page">
      <h1>Entry <span className="mono">{ulid}</span></h1>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
        <div>
          <h2>Metadata</h2>
          <table>
            <tbody>
              <tr><td style={{ color: "#8b949e", width: 140 }}>Timestamp</td><td>{new Date(entry.ts).toISOString()}</td></tr>
              <tr><td style={{ color: "#8b949e" }}>Subject</td><td className="mono">{entry.subject}</td></tr>
              <tr><td style={{ color: "#8b949e" }}>Actor</td><td className="mono">{entry.actor_kind}:{entry.actor_id}</td></tr>
              <tr><td style={{ color: "#8b949e" }}>Action</td><td>{entry.action}</td></tr>
              <tr><td style={{ color: "#8b949e" }}>Direction</td><td>{entry.direction}</td></tr>
              {entry.conv_id && (
                <tr>
                  <td style={{ color: "#8b949e" }}>Conversation</td>
                  <td><a href={`/conv/${entry.conv_id}`} className="mono">{entry.conv_id}</a></td>
                </tr>
              )}
            </tbody>
          </table>

          <h2 style={{ marginTop: 20 }}>Integrity</h2>
          <table>
            <tbody>
              <SigRow label="sig_service" valid={entry.sig_service_valid} />
              <SigRow label="sig_appender" valid={entry.sig_appender_valid} />
              <SigRow label="prev_hash chain" valid={entry.chain_valid} />
            </tbody>
          </table>
        </div>

        <div>
          {entry.redacted_payload && (
            <>
              <h2>Payload</h2>
              <pre>{JSON.stringify(entry.redacted_payload, null, 2)}</pre>
            </>
          )}
        </div>
      </div>

      {entry.raw_envelope && (
        <>
          <h2 style={{ marginTop: 24 }}>Raw envelope</h2>
          <pre>{JSON.stringify(entry.raw_envelope, null, 2)}</pre>
        </>
      )}
    </main>
  );
}
