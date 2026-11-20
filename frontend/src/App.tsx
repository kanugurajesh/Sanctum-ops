import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { api, ApiError } from "./api";
import type { AgentResponse, ApprovalRow, AuditRow, ChatItem, DemoUser, Me } from "./types";
import Sidebar from "./components/Sidebar";
import ChatView from "./components/ChatView";
import SidePanel from "./components/SidePanel";

const DEMO_PASSWORD = "demo123";

type Session = { token: string; me: Me; conversationId: string; items: ChatItem[] };

const uid = () => crypto.randomUUID();

export default function App() {
  const [users, setUsers] = useState<DemoUser[]>([]);
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [sessions, setSessions] = useState<Record<number, Session>>({});
  const [busy, setBusy] = useState(false);
  const [audit, setAudit] = useState<AuditRow[]>([]);
  const [approvals, setApprovals] = useState<ApprovalRow[]>([]);
  const [llmReady, setLlmReady] = useState<boolean | null>(null);
  const [llmProvider, setLlmProvider] = useState("groq");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<"users" | "activity" | null>(null);

  const session = currentId != null ? sessions[currentId] : undefined;

  useEffect(() => {
    api.demoUsers()
      .then((u) => {
        setUsers(u);
        let saved: number | null = null;
        try {
          saved = Number(localStorage.getItem("hms.user")) || null;
        } catch {
          /* storage unavailable */
        }
        setCurrentId(u.find((x) => x.user_id === saved)?.user_id ?? u[0]?.user_id ?? null);
      })
      .catch((e) => setLoadError(`Can't reach the API (${e.message}). Start the backend and run the seed script, then reload.`));
    api.health().then((h) => { setLlmReady(h.llm_configured); setLlmProvider(h.llm_provider); }).catch(() => setLlmReady(null));
  }, []);

  // Log in as the selected demo user (the token is the only identity the backend trusts).
  useEffect(() => {
    if (currentId == null || sessions[currentId]) return;
    const u = users.find((x) => x.user_id === currentId);
    if (!u) return;
    (async () => {
      const { token } = await api.login(u.email, DEMO_PASSWORD);
      const me = await api.me(token);
      setSessions((s) => ({ ...s, [currentId]: { token, me, conversationId: uid(), items: [] } }));
    })().catch((e) => setLoadError(String(e.message)));
  }, [currentId, users, sessions]);

  useEffect(() => {
    if (!drawer) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawer(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawer]);

  useEffect(() => {
    try {
      if (currentId != null) localStorage.setItem("hms.user", String(currentId));
    } catch {
      /* ignore */
    }
  }, [currentId]);

  const refreshPanels = useCallback(async () => {
    if (!session) return;
    const [a, p] = await Promise.all([api.audit(session.token), api.approvals(session.token)]);
    setAudit(a);
    setApprovals(p);
  }, [session]);

  useEffect(() => {
    refreshPanels().catch(() => undefined);
  }, [refreshPanels]);

  const update = (id: number, fn: (s: Session) => Session) =>
    setSessions((all) => (all[id] ? { ...all, [id]: fn(all[id]) } : all));

  const pushAgent = (id: number, response: AgentResponse) =>
    update(id, (s) => ({
      ...s,
      items: [...s.items.filter((i) => i.kind !== "pending"), { id: uid(), kind: "agent", response }],
    }));

  const errorResponse = (message: string): AgentResponse => ({
    type: "error", request_id: uid(), message, intent: null, answer: null, clarification: null,
    refusal: null, preview: null, executed: null, audit_id: null,
  });

  const send = async (text: string) => {
    if (!session || currentId == null || !text.trim()) return;
    const id = currentId;
    update(id, (s) => ({
      ...s,
      items: [...s.items, { id: uid(), kind: "user", text }, { id: uid(), kind: "pending" }],
    }));
    setBusy(true);
    try {
      pushAgent(id, await api.chat(session.token, text, session.conversationId));
    } catch (e) {
      pushAgent(id, errorResponse(e instanceof Error ? e.message : String(e)));
    } finally {
      setBusy(false);
      refreshPanels().catch(() => undefined);
    }
  };

  const resolve = (id: number, operationId: string, how: "executed" | "cancelled" | "failed") =>
    update(id, (s) => ({
      ...s,
      items: s.items.map((i) =>
        i.kind === "agent" && i.response.preview?.operation_id === operationId ? { ...i, resolved: how } : i,
      ),
    }));

  const confirm = async (operationId: string, typed: number | null): Promise<string | null> => {
    if (!session || currentId == null) return null;
    const id = currentId;
    setBusy(true);
    try {
      const r = await api.confirm(session.token, operationId, typed);
      resolve(id, operationId, r.type === "executed" ? "executed" : "failed");
      pushAgent(id, r);
      return null;
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) return e.message; // wrong typed count: stay on the card
      resolve(id, operationId, "failed");
      pushAgent(id, errorResponse(e instanceof Error ? e.message : String(e)));
      return null;
    } finally {
      setBusy(false);
      refreshPanels().catch(() => undefined);
    }
  };

  const cancel = async (operationId: string) => {
    if (!session || currentId == null) return;
    const id = currentId;
    try {
      const r = await api.cancel(session.token, operationId);
      resolve(id, operationId, "cancelled");
      pushAgent(id, r);
    } catch (e) {
      pushAgent(id, errorResponse(e instanceof Error ? e.message : String(e)));
    } finally {
      refreshPanels().catch(() => undefined);
    }
  };

  const requestApproval = async (auditId: number) => {
    if (!session || currentId == null) return;
    try {
      pushAgent(currentId, await api.requestApproval(session.token, auditId));
    } catch (e) {
      pushAgent(currentId, errorResponse(e instanceof Error ? e.message : String(e)));
    } finally {
      refreshPanels().catch(() => undefined);
    }
  };

  const newChat = () =>
    currentId != null && update(currentId, (s) => ({ ...s, items: [], conversationId: uid() }));

  const examples = useMemo(() => examplesFor(session?.me.roles ?? []), [session?.me.roles]);

  const pendingApprovals = approvals.filter((a) => a.status === "pending").length;
  const selectUser = (id: number) => {
    setCurrentId(id);
    setDrawer(null);
  };

  return (
    <div className="flex h-full overflow-hidden">
      {drawer && (
        <div
          aria-hidden
          onClick={() => setDrawer(null)}
          className={`fixed inset-0 z-30 bg-ink/35 ${drawer === "users" ? "md:hidden" : "xl:hidden"}`}
        />
      )}
      <Sidebar
        users={users}
        currentId={currentId}
        onSelect={selectUser}
        me={session?.me}
        open={drawer === "users"}
        onClose={() => setDrawer(null)}
      />
      <main className="flex min-w-0 flex-1 flex-col">
        {loadError && (
          <div role="alert" className="flex items-start gap-2 border-b border-stamp/25 bg-stamp-wash px-4 py-2.5 text-sm text-stamp md:px-6">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" /> {loadError}
          </div>
        )}
        {llmReady === false && (
          <div role="alert" className="flex items-start gap-2 border-b border-caution/25 bg-caution-wash px-4 py-2.5 text-sm text-ink md:px-6">
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-caution" />
            <span>
              The language model isn't connected. Add{" "}
              <code className="font-mono">{llmProvider === "gemini" ? "GEMINI_API_KEY" : "GROQ_API_KEY"}</code> to{" "}
              <code className="font-mono">.env</code> and restart the api container.
            </span>
          </div>
        )}
        <ChatView
          me={session?.me}
          items={session?.items ?? []}
          busy={busy}
          examples={examples}
          pendingApprovals={pendingApprovals}
          onSend={send}
          onConfirm={confirm}
          onCancel={cancel}
          onRequestApproval={requestApproval}
          onNewChat={newChat}
          onOpenUsers={() => setDrawer("users")}
          onOpenActivity={() => setDrawer("activity")}
        />
      </main>
      <SidePanel
        audit={audit}
        approvals={approvals}
        me={session?.me}
        onRefresh={refreshPanels}
        onSend={send}
        open={drawer === "activity"}
        onClose={() => setDrawer(null)}
      />
    </div>
  );
}

function examplesFor(roles: string[]): string[] {
  const out: string[] = [];
  const add = (...q: string[]) => q.forEach((x) => !out.includes(x) && out.push(x));
  if (roles.includes("admin"))
    add(
      "Give 20% discount on all bills from last month",
      "Bulk update the entire inventory with quantity 5",
      "Remove inventory edit access from Ravi",
      "Give Ravi access",
      "Remove my admin access",
      "Update stock of paracetamol at Hospital B to 100",
      "Delete all bills",
      "Show pending approvals",
      "Undo my last change",
    );
  if (roles.includes("billing_manager"))
    add("Give 20% discount on all bills from last month", "Make me an admin", "Show pending bills from this week");
  if (roles.includes("pharmacist"))
    add(
      "Bulk update the entire inventory with quantity 5",
      "Show items expiring in the next 30 days",
      "Summarise low-stock items",
    );
  if (roles.includes("front_desk")) add("Set all inventory quantities to 5", "Show my bills from last month");
  if (roles.includes("doctor")) add("Show items expiring in the next 30 days", "Set all inventory quantities to 5");
  add("What can I do?");
  return out;
}
