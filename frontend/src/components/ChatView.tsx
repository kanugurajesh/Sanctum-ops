import { useEffect, useRef, useState } from "react";
import { History, Loader2, Plus, SendHorizonal, Users } from "lucide-react";
import type { ChatItem, Me } from "../types";
import AgentCard from "./AgentCard";
import { RoleBadge } from "./Sidebar";

type Props = {
  me?: Me;
  items: ChatItem[];
  busy: boolean;
  examples: string[];
  pendingApprovals: number;
  onSend: (t: string) => void;
  onConfirm: (id: string, typed: number | null) => Promise<string | null>;
  onCancel: (id: string) => void;
  onRequestApproval: (auditId: number) => void;
  onNewChat: () => void;
  onOpenUsers: () => void;
  onOpenActivity: () => void;
};

export default function ChatView({
  me, items, busy, examples, pendingApprovals, onSend, onConfirm, onCancel, onRequestApproval, onNewChat, onOpenUsers, onOpenActivity,
}: Props) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    endRef.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth" });
  }, [items.length, items[items.length - 1]?.id]);

  // Grow the composer with its content, up to the max height set in CSS.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text]);

  const submit = () => {
    if (!text.trim() || busy) return;
    onSend(text.trim());
    setText("");
  };

  const firstName = me ? (me.name.startsWith("Dr. ") ? me.name : me.name.split(" ")[0]) : "";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-rule bg-sheet px-4 py-3 md:px-6">
        <button onClick={onOpenUsers} className="rounded-md p-2 text-ink-2 hover:bg-paper md:hidden" aria-label="Switch user">
          <Users size={18} />
        </button>
        {me ? (
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h1 className="text-base font-bold">{me.name}</h1>
              <span className="flex flex-wrap gap-1.5">{me.roles.map((r) => <RoleBadge key={r} role={r} />)}</span>
            </div>
            <div className="truncate text-xs text-ink-2">
              Hospital {me.hospital.code}, {me.hospital.name}, {me.hospital.city}
            </div>
          </div>
        ) : (
          <div className="text-sm text-ink-2">Signing in…</div>
        )}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {me && items.length > 0 && (
            <button
              onClick={onNewChat}
              className="inline-flex items-center gap-1.5 rounded-lg border border-rule bg-sheet px-3 py-1.5 text-sm font-medium hover:border-ink-2/40 hover:bg-paper"
            >
              <Plus size={15} /> <span className="hidden sm:inline">New chat</span>
            </button>
          )}
          <button
            onClick={onOpenActivity}
            className="relative inline-flex items-center gap-1.5 rounded-lg border border-rule bg-sheet px-3 py-1.5 text-sm font-medium hover:bg-paper xl:hidden"
          >
            <History size={15} /> <span className="hidden sm:inline">Activity</span>
            {pendingApprovals > 0 && (
              <span className="tnum rounded-full bg-caution px-1.5 text-xs font-bold text-white" aria-label={`${pendingApprovals} pending approvals`}>
                {pendingApprovals}
              </span>
            )}
          </button>
        </div>
      </header>

      <div className="scroll-thin flex-1 overflow-y-auto px-4 py-6 md:px-8">
        <div className="mx-auto max-w-3xl space-y-5">
          {items.length === 0 && me && (
            <div className="pt-6 md:pt-12">
              <h2 className="text-2xl font-bold leading-tight md:text-[28px]">What needs doing, {firstName}?</h2>
              <p className="mt-2 max-w-[60ch] text-[15px] leading-relaxed text-ink-2">
                Type an instruction in plain English. You can ask questions freely. Anything that changes records is shown to
                you first as a preview, and nothing is written until you apply it.
              </p>
              <h3 className="mt-8 text-[13px] font-semibold text-ink-2">Things you can try with your roles</h3>
              <ul className="mt-2 divide-y divide-rule-soft overflow-hidden rounded-xl border border-rule bg-sheet">
                {examples.map((q) => (
                  <li key={q}>
                    <button
                      disabled={busy}
                      onClick={() => onSend(q)}
                      className="group flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm hover:bg-pen-wash disabled:opacity-50"
                    >
                      <span className="flex-1">{q}</span>
                      <SendHorizonal size={14} className="text-ink-2 opacity-0 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {items.map((it) =>
            it.kind === "user" ? (
              <div key={it.id} className="flex justify-end">
                <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-ink px-4 py-2.5 text-[15px] leading-snug text-sheet md:max-w-[75%]">
                  {it.text}
                </div>
              </div>
            ) : it.kind === "pending" ? (
              <div key={it.id} role="status" className="flex items-center gap-2.5 py-1 text-sm text-ink-2">
                <Loader2 size={16} className="motion-safe:animate-spin" /> Reading your request and checking permissions…
              </div>
            ) : (
              <AgentCard
                key={it.id}
                r={it.response}
                resolved={it.resolved}
                busy={busy}
                onSend={onSend}
                onConfirm={onConfirm}
                onCancel={onCancel}
                onRequestApproval={onRequestApproval}
              />
            ),
          )}
          <div ref={endRef} />
        </div>
      </div>

      <div className="border-t border-rule bg-sheet px-4 pb-4 pt-3 md:px-8">
        <div className="mx-auto max-w-3xl">
          {items.length > 0 && (
            <div className="scroll-thin -mx-1 mb-2.5 flex gap-1.5 overflow-x-auto px-1 pb-1">
              {examples.map((q) => (
                <button
                  key={q}
                  disabled={busy || !me}
                  onClick={() => onSend(q)}
                  className="shrink-0 rounded-full border border-rule bg-paper/60 px-3 py-1 text-[13px] text-ink hover:border-ink-2/40 hover:bg-paper disabled:opacity-50"
                >
                  {q}
                </button>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2 rounded-2xl border border-rule bg-sheet p-1.5 pl-4 focus-within:border-pen focus-within:ring-2 focus-within:ring-pen/15">
            <label htmlFor="composer" className="sr-only">Instruction</label>
            <textarea
              id="composer"
              ref={inputRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              rows={1}
              placeholder={me ? "e.g. 10% off pending bills from last week" : "Signing in…"}
              disabled={!me}
              className="scroll-thin max-h-36 min-h-[40px] flex-1 resize-none bg-transparent py-2 text-[15px] placeholder:text-ink-2/70 focus:outline-none focus-visible:outline-none"
            />
            <button
              onClick={submit}
              disabled={busy || !text.trim()}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-ink text-sheet hover:bg-ink/90 disabled:opacity-30"
              aria-label="Send"
            >
              {busy ? <Loader2 size={17} className="motion-safe:animate-spin" /> : <SendHorizonal size={17} />}
            </button>
          </div>
          <p className="mt-1.5 hidden text-xs text-ink-2 sm:block">Enter to send, Shift+Enter for a new line.</p>
        </div>
      </div>
    </div>
  );
}
