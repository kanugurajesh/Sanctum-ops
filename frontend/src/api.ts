import type { AgentResponse, ApprovalRow, AuditRow, DemoUser, Me } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function call<T>(path: string, token: string | null, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  demoUsers: () => call<DemoUser[]>("/auth/demo-users", null),
  login: (email: string, password: string) =>
    call<{ token: string }>("/auth/login", null, { method: "POST", body: JSON.stringify({ email, password }) }),
  me: (t: string) => call<Me>("/me", t),
  chat: (t: string, message: string, conversation_id: string) =>
    call<AgentResponse>("/chat", t, { method: "POST", body: JSON.stringify({ message, conversation_id }) }),
  confirm: (t: string, id: string, typed_count: number | null) =>
    call<AgentResponse>(`/operations/${id}/confirm`, t, { method: "POST", body: JSON.stringify({ typed_count }) }),
  cancel: (t: string, id: string) => call<AgentResponse>(`/operations/${id}/cancel`, t, { method: "POST" }),
  requestApproval: (t: string, audit_id: number) =>
    call<AgentResponse>("/approvals", t, { method: "POST", body: JSON.stringify({ audit_id }) }),
  approvals: (t: string) => call<ApprovalRow[]>("/approvals", t),
  audit: (t: string) => call<AuditRow[]>("/audit?limit=60", t),
  health: () => call<{ ok: boolean; llm_configured: boolean; llm_provider: string; llm_model: string }>("/health", null),
};
