import { useId, useState } from "react";
import { ArrowRight, Check, PenLine } from "lucide-react";
import type { PreviewBody } from "../types";
import { RoleBadge } from "./Sidebar";

type Props = {
  preview: PreviewBody;
  resolved?: "executed" | "cancelled" | "failed";
  busy: boolean;
  onConfirm: (id: string, typed: number | null) => Promise<string | null>;
  onCancel: (id: string) => void;
};

function fmt(v: unknown) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en-IN")} ${n === 1 ? one : many}`;

export default function PreviewCard({ preview: p, resolved, busy, onConfirm, onCancel }: Props) {
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const inputId = useId();
  const typedOk = !p.requires_typed_count || typed.trim() === String(p.rows_affected);
  const open = !resolved;
  const fields = p.changed_fields.filter((f) => !f.startsWith("("));
  const expires = new Date(p.expires_at).toLocaleTimeString("en-IN", { hour: "numeric", minute: "2-digit" });
  const moneyDown = p.money_impact?.trim().startsWith("-") || p.money_impact?.trim().startsWith("−");

  const facts: { value: string; label: string; tone?: string }[] = [
    { value: p.rows_affected.toLocaleString("en-IN"), label: p.rows_affected === 1 ? "record changes" : "records change" },
  ];
  if (p.date_range) facts.push({ value: p.date_range, label: "date range" });
  if (p.money_impact) facts.push({ value: p.money_impact, label: p.money_label ?? "money impact", tone: moneyDown ? "text-stamp" : "text-ok" });

  return (
    <div className="space-y-4">
      <h3 className="text-lg font-bold leading-snug">{p.title}</h3>

      <dl className="grid grid-cols-1 overflow-hidden rounded-lg border border-rule sm:auto-cols-fr sm:grid-flow-col sm:grid-cols-none">
        {facts.map((f, i) => (
          <div key={f.label} className={`px-4 py-2.5 ${i > 0 ? "border-t border-rule sm:border-l sm:border-t-0" : ""}`}>
            <dt className="text-xs text-ink-2">{f.label.charAt(0).toUpperCase() + f.label.slice(1)}</dt>
            <dd className={`tnum mt-0.5 font-bold ${i === 0 ? "text-2xl leading-tight" : "text-[15px] leading-snug"} ${f.tone ?? ""}`}>{f.value}</dd>
          </div>
        ))}
      </dl>

      {p.access ? (
        <div className="rounded-lg border border-rule p-4">
          <div className="text-[15px] font-bold">{p.access.user_name}</div>
          <div className="text-[13px] text-ink-2">{p.access.email}</div>
          <dl className="mt-3 grid grid-cols-[6.5rem_1fr] items-baseline gap-x-3 gap-y-2 text-[13px]">
            <dt className="text-ink-2">Roles now</dt>
            <dd className="flex flex-wrap gap-1.5">{p.access.roles_before.map((r) => <RoleBadge key={r} role={r} />)}{p.access.roles_before.length === 0 && "None"}</dd>
            <dt className="text-ink-2">Roles after</dt>
            <dd className="flex flex-wrap gap-1.5">{p.access.roles_after.map((r) => <RoleBadge key={r} role={r} />)}{p.access.roles_after.length === 0 && "None"}</dd>
            {p.access.added.length > 0 && (
              <>
                <dt className="text-ink-2">Gains</dt>
                <dd className="font-mono text-ok">+ {p.access.added.join(", ")}</dd>
              </>
            )}
            {p.access.removed.length > 0 && (
              <>
                <dt className="text-ink-2">Loses</dt>
                <dd className="font-mono text-stamp">− {p.access.removed.join(", ")}</dd>
              </>
            )}
            <dt className="text-ink-2">Keeps</dt>
            <dd className="font-mono text-ink-2">
              {Object.keys(p.access.permissions_after).filter((k) => !p.access!.added.includes(k)).join(", ") || "Nothing"}
            </dd>
          </dl>
        </div>
      ) : (
        <div>
          <div className="mb-1.5 text-[13px] text-ink-2">
            {p.samples.length < p.rows_affected
              ? `Sample of ${Math.min(p.samples.length, 5)} out of ${plural(p.rows_affected, "record")}, before and after`
              : "Every record, before and after"}
          </div>
          <div className="scroll-thin overflow-x-auto rounded-lg border border-rule">
            <table className="tnum w-full text-left text-[13px]">
              <thead className="bg-paper text-ink-2">
                <tr>
                  <th scope="col" className="px-3 py-2 font-semibold">Record</th>
                  {(fields.length ? fields : ["status"]).map((f) => (
                    <th key={f} scope="col" className="whitespace-nowrap px-3 py-2 font-semibold">{f.replace(/_/g, " ")}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-rule-soft">
                {p.samples.map((s) => (
                  <tr key={s.id}>
                    <td className="max-w-64 truncate px-3 py-2" title={s.label}>{s.label}</td>
                    {fields.length ? fields.map((f) => (
                      <td key={f} className="whitespace-nowrap px-3 py-2">
                        <span className="text-ink-2 line-through decoration-ink-2/50">{fmt(s.before?.[f])}</span>
                        <ArrowRight size={12} aria-label="becomes" className="mx-1.5 inline text-ink-2" />
                        <span className="font-bold text-pen-deep">{fmt(s.after?.[f])}</span>
                      </td>
                    )) : (
                      <td className="px-3 py-2 font-semibold text-stamp">{s.after ? "Restored" : "Deleted"}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {p.notes.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-[13px] leading-relaxed text-ink-2 marker:text-rule">
          {p.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}

      {open ? (
        <div className="-mx-4 -mb-3.5 rounded-br-xl border-t-2 border-dashed border-pen/35 bg-pen-wash px-4 pb-4 pt-3.5 md:-mx-5 md:px-5">
          <div className="flex items-center gap-2 text-[13px] font-semibold text-pen-deep">
            <PenLine size={15} /> Your sign-off
            <span className="tnum ml-auto font-normal text-ink-2">Preview expires at {expires}</span>
          </div>

          {p.requires_typed_count && (
            <div className="mt-2.5">
              <label htmlFor={inputId} className="block text-sm leading-relaxed">
                This changes more than 50 records. Type <b className="tnum">{p.rows_affected}</b> to confirm you've checked the count.
              </label>
              <input
                id={inputId}
                value={typed}
                onChange={(e) => { setTyped(e.target.value); setErr(null); }}
                inputMode="numeric"
                autoComplete="off"
                aria-invalid={!!err}
                className="tnum mt-2 block w-36 border-0 border-b-2 border-pen/50 bg-transparent px-1 py-1 text-xl font-bold text-pen-deep outline-none placeholder:font-normal placeholder:text-pen/30 focus:border-pen focus-visible:outline-none"
              />
            </div>
          )}
          {err && <p role="alert" className="mt-2 text-[13px] font-medium text-stamp">{err}</p>}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              disabled={!typedOk || busy}
              onClick={async () => setErr(await onConfirm(p.operation_id, p.requires_typed_count ? Number(typed.trim()) : null))}
              className="rounded-lg bg-pen px-4 py-2 text-sm font-bold text-white shadow-[0_1px_0_#1b2a84] hover:bg-pen-deep disabled:cursor-not-allowed disabled:opacity-35"
            >
              Apply {p.access ? "access change" : plural(p.rows_affected, "change")}
            </button>
            <button
              disabled={busy}
              onClick={() => onCancel(p.operation_id)}
              className="rounded-lg border border-rule bg-sheet px-4 py-2 text-sm font-medium hover:bg-paper disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <p
          className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[13px] font-semibold ${
            resolved === "executed" ? "bg-ok-wash text-ok" : resolved === "cancelled" ? "bg-paper text-ink-2" : "bg-stamp-wash text-stamp"
          }`}
        >
          {resolved === "executed" && <Check size={14} />}
          {resolved === "executed" ? "Applied" : resolved === "cancelled" ? "Cancelled. Nothing was changed." : "Not applied"}
        </p>
      )}
    </div>
  );
}
