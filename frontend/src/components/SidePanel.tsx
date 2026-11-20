import { useState } from "react";
import { RefreshCw, X } from "lucide-react";
import type { ApprovalRow, AuditRow, Me } from "../types";

const DECISION: Record<string, { label: string; color: string }> = {
  answered: { label: "Answered", color: "#546863" },
  clarified: { label: "Asked to clarify", color: "#8f5a00" },
  refused: { label: "Refused", color: "#b3362c" },
  previewed: { label: "Previewed", color: "#2537a6" },
  executed: { label: "Applied", color: "#1f7049" },
  cancelled: { label: "Cancelled", color: "#546863" },
  expired: { label: "Expired", color: "#546863" },
  failed: { label: "Failed", color: "#b3362c" },
};

const STATUS: Record<string, string> = {
  pending: "bg-caution-wash text-caution",
  approved: "bg-ok-wash text-ok",
  rejected: "bg-stamp-wash text-stamp",
};

const ist = (iso: string, opts: Intl.DateTimeFormatOptions) =>
  new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", ...opts });

type Props = {
  audit: AuditRow[];
  approvals: ApprovalRow[];
  me?: Me;
  onRefresh: () => Promise<void>;
  onSend: (t: string) => void;
  open: boolean;
  onClose: () => void;
};

export default function SidePanel({ audit, approvals, me, onRefresh, onSend, open, onClose }: Props) {
  const [tab, setTab] = useState<"audit" | "approvals">("audit");
  const [spinning, setSpinning] = useState(false);
  const canDecide = !!me?.permissions["users.manage_access"];
  const pending = approvals.filter((a) => a.status === "pending").length;

  const refresh = async () => {
    setSpinning(true);
    try {
      await onRefresh();
    } finally {
      setSpinning(false);
    }
  };

  return (
    <aside
      aria-label="Activity"
      className={`${open ? "fixed inset-y-0 right-0 z-40 flex shadow-2xl" : "hidden"} w-[min(380px,100vw)] shrink-0 flex-col border-l border-rule bg-sheet xl:static xl:z-auto xl:flex xl:w-[360px] xl:shadow-none`}
    >
      <div className="flex items-end gap-5 border-b border-rule px-5 pt-3" role="tablist">
        {(["audit", "approvals"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`-mb-px flex items-center gap-1.5 border-b-2 pb-2.5 pt-1.5 text-sm font-semibold ${
              tab === t ? "border-ink text-ink" : "border-transparent text-ink-2 hover:text-ink"
            }`}
          >
            {t === "audit" ? "Audit log" : "Approvals"}
            {t === "approvals" && pending > 0 && (
              <span className="tnum rounded-full bg-caution px-1.5 text-xs font-bold text-white">{pending}</span>
            )}
          </button>
        ))}
        <div className="mb-1.5 ml-auto flex items-center gap-1">
          <button onClick={refresh} className="rounded-md p-1.5 text-ink-2 hover:bg-paper hover:text-ink" aria-label="Refresh">
            <RefreshCw size={15} className={spinning ? "motion-safe:animate-spin" : ""} />
          </button>
          <button onClick={onClose} className="rounded-md p-1.5 text-ink-2 hover:bg-paper xl:hidden" aria-label="Close activity">
            <X size={17} />
          </button>
        </div>
      </div>

      <div className="scroll-thin flex-1 overflow-y-auto">
        {tab === "audit" ? (
          <>
            <p className="px-5 pb-1 pt-3 text-xs text-ink-2">
              {me?.permissions["audit.read"] ? "Everything asked in your hospital" : "Everything you've asked"}, newest first.
              Entries can't be edited or deleted.
            </p>
            <ol>
              {audit.map((a) => {
                const d = DECISION[a.decision] ?? { label: a.decision, color: "#546863" };
                return (
                  <li key={a.id} className="grid grid-cols-[3.75rem_1fr] gap-x-3 border-b border-rule-soft px-5 py-3">
                    <div className="tnum pt-px text-xs leading-snug text-ink-2" title={`${ist(a.created_at, { dateStyle: "medium", timeStyle: "medium" })} IST`}>
                      <div className="font-semibold text-ink">{ist(a.created_at, { hour: "numeric", minute: "2-digit" })}</div>
                      <div>{ist(a.created_at, { day: "numeric", month: "short" })}</div>
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-xs">
                        <span className="inline-flex items-center gap-1.5 font-semibold" style={{ color: d.color }}>
                          <span aria-hidden className="h-2 w-2 rounded-full" style={{ background: d.color }} />
                          {d.label}
                        </span>
                        {a.action && <code className="truncate font-mono text-ink-2">{a.action}</code>}
                        <span className="tnum ml-auto shrink-0 text-ink-2">#{a.id}</span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-sm leading-snug" title={a.raw_query}>“{a.raw_query}”</p>
                      {a.reason && <p className="mt-0.5 line-clamp-2 text-xs leading-snug text-ink-2" title={a.reason}>{a.reason}</p>}
                      <p className="mt-1 text-xs text-ink-2">
                        {a.user_name}
                        {a.rows_affected ? <>, <span className="tnum">{a.rows_affected.toLocaleString("en-IN")}</span> {a.rows_affected === 1 ? "record" : "records"}</> : null}
                      </p>
                    </div>
                  </li>
                );
              })}
              {audit.length === 0 && <li className="px-5 py-6 text-sm text-ink-2">Nothing recorded yet. Every request you send appears here.</li>}
            </ol>
          </>
        ) : (
          <>
            <p className="px-5 pb-1 pt-3 text-xs text-ink-2">
              {canDecide ? "Requests from staff that go beyond their own limits." : "Requests you've sent for approval."}
            </p>
            <ol>
              {approvals.map((a) => (
                <li key={a.id} className="border-b border-rule-soft px-5 py-3 text-sm">
                  <div className="flex items-center gap-2 text-xs">
                    <span className="tnum font-semibold">Request #{a.id}</span>
                    <span className="text-ink-2">from {a.requested_by}</span>
                    <span className={`ml-auto rounded-full px-2 py-0.5 font-semibold ${STATUS[a.status] ?? "bg-paper text-ink-2"}`}>
                      {a.status.charAt(0).toUpperCase() + a.status.slice(1)}
                    </span>
                  </div>
                  <p className="mt-1.5 leading-snug">{a.summary}</p>
                  {canDecide && a.status === "pending" && (
                    <div className="mt-2.5 flex gap-2">
                      <button
                        onClick={() => { onSend(`Approve request ${a.id}`); onClose(); }}
                        className="rounded-lg bg-ink px-3 py-1.5 text-[13px] font-semibold text-sheet hover:bg-ink/90"
                      >
                        Review and approve
                      </button>
                      <button
                        onClick={() => { onSend(`Reject request ${a.id}`); onClose(); }}
                        className="rounded-lg border border-rule px-3 py-1.5 text-[13px] font-medium hover:bg-paper"
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </li>
              ))}
              {approvals.length === 0 && (
                <li className="px-5 py-6 text-sm text-ink-2">
                  {canDecide ? "No requests waiting for you." : "You haven't asked for approval on anything."}
                </li>
              )}
            </ol>
          </>
        )}
      </div>
    </aside>
  );
}
