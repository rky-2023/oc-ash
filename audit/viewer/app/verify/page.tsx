import { api } from "@/lib/api";
import type { VerifyResult } from "@/lib/types";

export default async function VerifyPage({
  searchParams,
}: {
  searchParams: Promise<{ date?: string }>;
}) {
  const p = await searchParams;
  const date = p.date ?? new Date().toISOString().slice(0, 10);

  let result: VerifyResult | null = null;
  let err = "";
  try {
    result = await api.verify(date);
  } catch (e) {
    err = String(e);
  }

  const statusClass =
    result?.status === "PASS" ? "ok" :
    result?.status === "FAIL" ? "fail" : "na";

  return (
    <main className="page">
      <h1>Chain verification</h1>
      <form method="get" style={{ marginBottom: 24, display: "flex", gap: 8, alignItems: "center" }}>
        <label htmlFor="date" style={{ color: "#8b949e" }}>Date:</label>
        <input
          id="date" name="date" type="date" defaultValue={date}
          style={{
            background: "#161b22", border: "1px solid #30363d",
            color: "#c9d1d9", padding: "4px 8px", borderRadius: 4,
            fontFamily: "inherit", fontSize: 13,
          }}
        />
        <button
          type="submit"
          style={{
            background: "#21262d", border: "1px solid #30363d",
            color: "#c9d1d9", padding: "4px 12px", borderRadius: 4,
            cursor: "pointer", fontFamily: "inherit", fontSize: 13,
          }}
        >
          Verify
        </button>
      </form>

      {err && <pre style={{ color: "#f85149" }}>{err}</pre>}

      {result && (
        <table style={{ maxWidth: 400 }}>
          <tbody>
            <tr>
              <td style={{ color: "#8b949e", width: 180 }}>Date</td>
              <td>{result.date}</td>
            </tr>
            <tr>
              <td style={{ color: "#8b949e" }}>Total entries</td>
              <td>{result.total}</td>
            </tr>
            <tr>
              <td style={{ color: "#8b949e" }}>sig_service valid</td>
              <td>{result.sig_service_ok} / {result.total}</td>
            </tr>
            <tr>
              <td style={{ color: "#8b949e" }}>sig_appender valid</td>
              <td>{result.sig_appender_ok} / {result.total}</td>
            </tr>
            <tr>
              <td style={{ color: "#8b949e" }}>Chain links valid</td>
              <td>{result.chain_ok} / {result.total}</td>
            </tr>
            <tr>
              <td style={{ color: "#8b949e" }}>Status</td>
              <td>
                <span className={`badge ${statusClass}`} style={{ fontSize: 13, padding: "2px 10px" }}>
                  {result.status}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      )}
    </main>
  );
}
