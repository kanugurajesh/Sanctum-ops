import { Ban, CheckCircle2, CircleHelp, Eye, Info, Undo2, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { AgentResponse } from "../types";
import DataTable from "./DataTable";
import PreviewCard from "./PreviewCard";

const STYLE: Record<AgentResponse["type"], { label: string; rule: string; text: string; icon: ReactNode }> = {
  answer: { label: "Answer", rule: "border-l-ink/40", text: "text-ink-2", icon: <Info size={15} /> },
  clarification: { label: "Needs your answer", rule: "border-l-caution", text: "text-caution", icon: <CircleHelp size={15} /> },
  refusal: { label: "Not allowed", rule: "border-l-stamp", text: "text-stamp", icon: <Ban size={15} /> },
  preview: { label: "Preview, nothing has changed yet", rule: "border-l-pen", text: "text-pen", icon: <Eye size={15} /> },
  executed: { label: "Changes applied", rule: "border-l-ok", text: "text-ok", icon: <CheckCircle2 size={15} /> },
  cancelled: { label: "Cancelled", rule: "border-l-rule", text: "text-ink-2", icon: <XCircle size={15} /> },
  error: { label: "Request failed", rule: "border-l-stamp", text: "text-stamp", icon: <XCircle size={15} /> },
};

type Props = {
  r: AgentResponse;
  resolved?: "executed" | "cancelled" | "failed";
  busy: boolean;
  onSend: (t: string) => void;
  onConfirm: (id: string, typed: number | null) => Promise<string | null>;
  onCancel: (id: string) => void;
  onRequestApproval: (auditId: number) => void;
};

const chip =
  "rounded-full border px-3 py-1 text-[13px] font-medium disabled:opacity-50";

export default function AgentCard({ r, resolved, busy, onSend, onConfirm, onCancel, onRequestApproval }: Props) {
  const s = STYLE[r.type];
  return (
    <article className={`entry-in rounded-r-xl rounded-l-sm border border-l-[3px] border-rule bg-sheet px-4 py-3.5 md:px-5 ${s.rule}`}>
      <header className="mb-2 flex items-center gap-1.5 text-[13px] font-semibold">
        <span className={`inline-flex items-center gap-1.5 ${s.text}`}>{s.icon} {s.label}</span>
        {r.audit_id && <span className="tnum ml-auto text-xs font-normal text-ink-2">Audit #{r.audit_id}</span>}
      </header>

      {r.type === "preview" && r.preview ? (
        <PreviewCard preview={r.preview} resolved={resolved} busy={busy} onConfirm={onConfirm} onCancel={onCancel} />
      ) : r.type === "answer" && r.answer ? (
        <div className="space-y-2.5">
          <h3 className="text-base font-bold leading-snug">{r.answer.title}</h3>
          {r.answer.summary.length > 0 && (
            <ul className="space-y-0.5 text-[15px] leading-relaxed">
              {r.answer.summary.map((l, i) => <li key={i}>{l}</li>)}
            </ul>
          )}
          {r.answer.table && <DataTable table={r.answer.table} />}
          {r.answer.notes.map((n, i) => <p key={i} className="text-[13px] text-ink-2">{n}</p>)}
        </div>
      ) : r.type === "refusal" && r.refusal ? (
        <div className="space-y-2.5">
          <p className="max-w-[68ch] text-[15px] leading-relaxed">{r.refusal.reason}</p>
          {r.refusal.suggestions.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {r.refusal.suggestions.map((sg, i) => (
                <button
                  key={i}
                  disabled={busy}
                  onClick={() => (sg.kind === "request_approval" && sg.audit_id ? onRequestApproval(sg.audit_id) : sg.message && onSend(sg.message))}
                  className={
                    sg.kind === "request_approval"
                      ? `${chip} border-ink bg-ink text-sheet hover:bg-ink/90`
                      : `${chip} border-rule bg-sheet hover:border-ink-2/40 hover:bg-paper`
                  }
                >
                  {sg.label}
                </button>
              ))}
            </div>
          )}
          <p className="text-xs text-ink-2">
            Reason code <code className="font-mono">{r.refusal.code}</code>
          </p>
        </div>
      ) : r.type === "clarification" && r.clarification ? (
        <div className="space-y-2.5">
          <p className="max-w-[68ch] text-[15px] leading-relaxed">{r.clarification.question}</p>
          {r.clarification.options.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {r.clarification.options.map((o, i) => (
                <button
                  key={i}
                  disabled={busy}
                  onClick={() => onSend(o.message)}
                  className={`${chip} border-caution/40 bg-caution-wash text-ink hover:border-caution`}
                >
                  {o.label}
                </button>
              ))}
            </div>
          )}
          <p className="text-xs text-ink-2">Nothing happens until you answer.</p>
        </div>
      ) : (
        <div className="flex items-start gap-3">
          <p className="flex-1 text-[15px] leading-relaxed">{r.message}</p>
          {r.type === "executed" && r.executed?.undoable && (
            <button
              disabled={busy}
              onClick={() => onSend("Undo my last change")}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-rule px-3 py-1.5 text-[13px] font-medium hover:bg-paper disabled:opacity-50"
            >
              <Undo2 size={14} /> Undo
            </button>
          )}
        </div>
      )}

      {r.intent && (
        <details className="group mt-3 border-t border-rule-soft pt-2 text-xs">
          <summary className="cursor-pointer select-none text-ink-2 hover:text-ink">
            How the model read this request
          </summary>
          <p className="mt-1.5 text-ink-2">
            The language model only proposes. Permission checks, limits and hospital scoping ran in code.
          </p>
          <pre className="scroll-thin mt-1.5 overflow-x-auto rounded-lg bg-ink p-3 font-mono text-xs leading-relaxed text-[#d7e3de]">
            {JSON.stringify(r.intent, null, 2)}
          </pre>
        </details>
      )}
    </article>
  );
}
