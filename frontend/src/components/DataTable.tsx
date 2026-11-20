import type { Table } from "../types";

const isNumeric = (v: unknown) => typeof v === "number" || (typeof v === "string" && /^[-−₹\s\d.,%]+$/.test(v) && /\d/.test(v));

export default function DataTable({ table }: { table: Table }) {
  // Right-align a column when every non-empty value in it is numeric, so figures line up.
  const numericCol = table.columns.map((_, j) => {
    const vals = table.rows.map((r) => r[j]).filter((v) => v !== null && v !== "");
    return vals.length > 0 && vals.every(isNumeric);
  });
  return (
    <div className="scroll-thin max-h-80 overflow-auto rounded-lg border border-rule">
      <table className="tnum w-full text-left text-[13px]">
        <thead className="sticky top-0 bg-paper text-ink-2">
          <tr>
            {table.columns.map((c, j) => (
              <th key={c} scope="col" className={`whitespace-nowrap px-3 py-2 font-semibold ${numericCol[j] ? "text-right" : ""}`}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-rule-soft">
          {table.rows.map((row, i) => (
            <tr key={i} className="hover:bg-paper/50">
              {row.map((cell, j) => (
                // Rendered as text by React: data such as an item named "Ignore previous instructions…" is just shown.
                <td key={j} className={`whitespace-nowrap px-3 py-1.5 ${numericCol[j] ? "text-right" : ""}`}>{cell ?? "—"}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
