export type DemoUser = {
  user_id: number;
  name: string;
  email: string;
  hospital_id: number;
  hospital_name: string;
  hospital_code: string;
  roles: string[];
};

export type Me = {
  user_id: number;
  name: string;
  email: string;
  roles: string[];
  hospital: { id: number; name: string; city: string; code: string };
  permissions: Record<string, Record<string, unknown>>;
};

export type Table = { columns: string[]; rows: (string | number | null)[][] };

export type AnswerBody = {
  title: string;
  summary: string[];
  table: Table | null;
  total: number | null;
  date_range: string | null;
  notes: string[];
};

export type Option = { label: string; message: string };
export type Suggestion = { label: string; message: string | null; kind: string; audit_id: number | null };

export type SampleRow = {
  id: number;
  label: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
};

export type AccessPreview = {
  user_id: number;
  user_name: string;
  email: string;
  roles_before: string[];
  roles_after: string[];
  permissions_before: Record<string, Record<string, unknown>>;
  permissions_after: Record<string, Record<string, unknown>>;
  added: string[];
  removed: string[];
};

export type PreviewBody = {
  operation_id: string;
  action: string;
  title: string;
  rows_affected: number;
  changed_fields: string[];
  samples: SampleRow[];
  date_range: string | null;
  money_label: string | null;
  money_impact: string | null;
  requires_typed_count: boolean;
  expires_at: string;
  notes: string[];
  access: AccessPreview | null;
};

export type AgentResponse = {
  type: "answer" | "clarification" | "refusal" | "preview" | "executed" | "cancelled" | "error";
  request_id: string;
  message: string;
  intent: Record<string, unknown> | null;
  answer: AnswerBody | null;
  clarification: { question: string; options: Option[] } | null;
  refusal: { code: string; reason: string; suggestions: Suggestion[] } | null;
  preview: PreviewBody | null;
  executed: { operation_id: string; title: string; rows_affected: number; audit_id: number; undoable: boolean } | null;
  audit_id: number | null;
};

export type ChatItem =
  | { id: string; kind: "user"; text: string }
  | { id: string; kind: "agent"; response: AgentResponse; resolved?: "executed" | "cancelled" | "failed" }
  | { id: string; kind: "pending" };

export type AuditRow = {
  id: number;
  created_at: string;
  action: string | null;
  decision: string;
  reason: string | null;
  reason_code: string | null;
  rows_affected: number;
  raw_query: string;
  user_name: string;
};

export type ApprovalRow = {
  id: number;
  summary: string;
  status: string;
  created_at: string;
  requested_by: string;
};
