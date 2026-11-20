import { X } from "lucide-react";
import type { DemoUser, Me } from "../types";

const ROLE_DOT: Record<string, string> = {
  admin: "#6b3fa0",
  billing_manager: "#1f6f8b",
  pharmacist: "#1f7049",
  front_desk: "#8f5a00",
  doctor: "#a23c6b",
};

export function roleName(role: string) {
  const s = role.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function RoleBadge({ role, tone = "default" }: { role: string; tone?: "default" | "plain" }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium ${
        tone === "plain" ? "text-ink-2" : "rounded-full border border-rule bg-sheet px-2 py-0.5 text-ink"
      }`}
    >
      <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: ROLE_DOT[role] ?? "#546863" }} />
      {roleName(role)}
    </span>
  );
}

function initials(name: string) {
  return name.replace("Dr. ", "").split(" ").map((p) => p[0]).slice(0, 2).join("");
}

type Props = {
  users: DemoUser[];
  currentId: number | null;
  onSelect: (id: number) => void;
  me?: Me;
  open: boolean;
  onClose: () => void;
};

export default function Sidebar({ users, currentId, onSelect, me, open, onClose }: Props) {
  const hospitals = [...new Map(users.map((u) => [u.hospital_id, u])).values()];
  return (
    <aside
      aria-label="Switch user"
      className={`${open ? "fixed inset-y-0 left-0 z-40 flex shadow-2xl" : "hidden"} w-72 shrink-0 flex-col border-r border-rule bg-sheet md:static md:z-auto md:flex md:shadow-none`}
    >
      <div className="flex items-center gap-3 px-5 pb-4 pt-5">
        <svg aria-hidden viewBox="0 0 32 32" className="h-8 w-8 shrink-0">
          <rect width="32" height="32" rx="8" fill="#17302b" />
          <path d="M13 8h6v5h5v6h-5v5h-6v-5H8v-6h5z" fill="#fafbf9" />
          <path d="M19 19l5 5" stroke="#8fa3ff" strokeWidth="2.2" strokeLinecap="round" />
        </svg>
        <div className="min-w-0">
          <div className="text-[15px] font-bold leading-tight">Sanctum Ops</div>
          <div className="text-xs text-ink-2">Checked before anything runs</div>
        </div>
        <button onClick={onClose} className="ml-auto rounded-md p-1.5 text-ink-2 hover:bg-paper md:hidden" aria-label="Close user list">
          <X size={18} />
        </button>
      </div>

      <div className="scroll-thin flex-1 overflow-y-auto pb-4">
        <h2 className="px-5 pb-1 text-[13px] font-semibold text-ink-2">Switch user</h2>
        {hospitals.map((h) => (
          <section key={h.hospital_id} className="mt-3">
            <div className="px-5 pb-1.5">
              <div className="text-sm font-semibold">Hospital {h.hospital_code}</div>
              <div className="text-xs text-ink-2">{h.hospital_name}</div>
            </div>
            <ul className="px-2">
              {users.filter((u) => u.hospital_id === h.hospital_id).map((u) => {
                const active = u.user_id === currentId;
                return (
                  <li key={u.user_id}>
                    <button
                      onClick={() => onSelect(u.user_id)}
                      aria-current={active ? "true" : undefined}
                      className={`relative flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left transition-colors ${
                        active ? "bg-pen-wash" : "hover:bg-paper"
                      }`}
                    >
                      {active && <span aria-hidden className="absolute inset-y-2 left-0 w-[3px] rounded-full bg-pen" />}
                      <span
                        className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
                          active ? "bg-pen text-white" : "bg-paper text-ink"
                        }`}
                      >
                        {initials(u.name)}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className={`flex items-baseline gap-1.5 text-sm ${active ? "font-bold text-pen-deep" : "font-medium"}`}>
                          <span className="truncate">{u.name}</span>
                          <span className="tnum shrink-0 text-xs font-normal text-ink-2">#{u.user_id}</span>
                        </span>
                        <span className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
                          {u.roles.map((r) => <RoleBadge key={r} role={r} tone="plain" />)}
                        </span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>

      {me && (
        <div className="border-t border-rule px-5 py-4">
          <h2 className="mb-2 text-[13px] font-semibold text-ink-2">Your permissions</h2>
          <ul className="scroll-thin max-h-48 space-y-1 overflow-y-auto">
            {Object.entries(me.permissions).map(([code, cons]) => (
              <li key={code} className="flex items-baseline justify-between gap-2 text-xs">
                <code className="font-mono text-ink">{code}</code>
                {Object.keys(cons).length > 0 && (
                  <span className="text-right text-caution">
                    {Object.entries(cons).map(([k, v]) => `${k.replace(/_/g, " ")} ${String(v)}`).join(", ")}
                  </span>
                )}
              </li>
            ))}
            {Object.keys(me.permissions).length === 0 && <li className="text-xs text-ink-2">No permissions.</li>}
          </ul>
        </div>
      )}
    </aside>
  );
}
