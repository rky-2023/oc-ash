import { api } from "@/lib/api";
import type { EntryRow } from "@/lib/types";

function sigBadge(v: boolean | null) {
  if (v === true)  return <span className="badge ok">✓</span>;
  if (v === false) return <span className="badge fail">✗</span>;
  return <span className="badge na">—</span>;
}

export default async function RecentPage({
  searchParams,
}: {
  searchParams: Promise<{ offset?: string; subject?: string }>;
}) {
  const p = await searchParams;
  const offset = Number(p.offset ?? 0);
  const subject = p.subject ?? undefined;
  let entries: EntryRow[] = [];
  let err = "";
  try {
    entries = await api.entries(200, offset, subject);
  } catch (e) {
    err = String(e);
  }

  return (
    <main className="page">
      <h1>Recent audit entries</h1>
      {err && <pre style={{ color: "#f85149" }}>{err}</pre>}
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Subject</th>
            <th>Actor</th>
            <th>Action</th>
            <th>Conv</th>
            <th>Svc</th>
            <th>App</th>
            <th>Chain</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.ulid}>
              <td className="ts">{new Date(e.ts).toISOString().replace("T", " ").slice(0, 19)}</td>
              <td className="mono">{e.subject}</td>
              <td className="mono">{e.actor_kind}:{e.actor_id}</td>
              <td>{e.action}</td>
              <td>
                {e.conv_id
                  ? <a href={`/conv/${e.conv_id}`} className="mono">{e.conv_id.slice(0, 8)}</a>
                  : "—"}
              </td>
              <td>{sigBadge(e.sig_service_valid)}</td>
              <td>{sigBadge(e.sig_appender_valid)}</td>
              <td>{sigBadge(e.chain_valid)}</td>
            </tr>
          ))}
          {entries.length === 0 && !err && (
            <tr><td colSpan={8} style={{ color: "#8b949e", textAlign: "center", padding: "32px" }}>No entries yet</td></tr>
          )}
        </tbody>
      </table>
      <div style={{ marginTop: 16, display: "flex", gap: 16 }}>
        {offset > 0 && <a href={`/?offset=${Math.max(0, offset - 200)}`}>← Newer</a>}
        {entries.length === 200 && <a href={`/?offset=${offset + 200}`}>Older →</a>}
      </div>
    </main>
  );
}
