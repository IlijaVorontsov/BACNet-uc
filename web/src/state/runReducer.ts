/**
 * Folds a run's event stream (API.md "Run events") into what the agent
 * panel shows. The reducer is pure and idempotent per `seq`: the replay after
 * a (re)connect and the live stream go through the same code, and an event
 * that was already folded is ignored.
 */

import type { Approval, RunEvent, RunState, TestResult, Tier } from "../api/types";

interface ItemBase {
  /** Stable React key. */
  key: string;
  /** Seq of the event that created the item. */
  seq: number;
  ts: number;
}

export interface UserItem extends ItemBase {
  kind: "user";
  text: string;
  user: string;
}

export interface AssistantItem extends ItemBase {
  kind: "assistant";
  text: string;
  streaming: boolean;
}

export interface ThinkingItem extends ItemBase {
  kind: "thinking";
  text: string;
  streaming: boolean;
  /** Time of the last event that belonged to this block. */
  endTs: number;
}

export type ToolStatus = "running" | "approval" | "question" | "ok" | "error";

export interface ToolResultInfo {
  ok: boolean;
  summary: string;
  durationMs: number;
  data?: unknown;
  handle?: string;
}

export interface ToolItem extends ItemBase {
  kind: "tool";
  callId: string;
  tool: string;
  tier: Tier;
  args: Record<string, unknown>;
  /** Raw argument JSON while the model is still streaming it. */
  argsText: string;
  /** The complete arguments arrived (the repeated `tool.call`), so `args` is final even when empty. */
  argsComplete: boolean;
  status: ToolStatus;
  result: ToolResultInfo | null;
  approvalId: string | null;
  questionId: string | null;
}

export interface ApprovalItem extends ItemBase {
  kind: "approval";
  approval: Approval;
}

export interface QuestionItem extends ItemBase {
  kind: "question";
  questionId: string;
  callId: string;
  text: string;
  options: string[];
  answer: string | null;
  answeredBy: string | null;
}

export interface ErrorItem extends ItemBase {
  kind: "error";
  message: string;
  code: string | null;
}

/** A terminal state change worth showing in the stream (failed, cancelled). */
export interface NoticeItem extends ItemBase {
  kind: "notice";
  state: RunState;
  reason: string | null;
}

export type RunItem = UserItem | AssistantItem | ThinkingItem | ToolItem | ApprovalItem | QuestionItem | ErrorItem | NoticeItem;

export interface RunView {
  runId: string | null;
  lastSeq: number;
  state: RunState | null;
  reason: string | null;
  items: RunItem[];
  /** Latest `plan.updated`. */
  plan: { planId: string | null; revision: number | null; changes: number; seq: number } | null;
  /** Latest `tests.updated`. */
  tests: { results: TestResult[]; seq: number } | null;
  /** Index into `items` of the thinking block still receiving deltas, or -1. */
  openThinking: number;
  /** Index into `items` of the assistant message still receiving deltas, or -1. */
  openAssistant: number;
  /**
   * Index into `items` of the first assistant message of the current model
   * step, or -1. A `tool.call` closes the message, but the step's
   * `message.done` comes after its calls and completes it (API.md).
   */
  stepAssistant: number;
  /** The error that ended the last turn; null once a new turn starts. */
  turnError: string | null;
}

export type RunAction = { type: "reset"; runId: string | null } | { type: "events"; events: RunEvent[] };

export function initialRunView(runId: string | null = null): RunView {
  return {
    runId,
    lastSeq: 0,
    state: null,
    reason: null,
    items: [],
    plan: null,
    tests: null,
    openThinking: -1,
    openAssistant: -1,
    stepAssistant: -1,
    turnError: null,
  };
}

export function runReducer(state: RunView, action: RunAction): RunView {
  switch (action.type) {
    case "reset":
      return state.runId === action.runId && state.lastSeq === 0 ? state : initialRunView(action.runId);
    case "events":
      return foldEvents(state, action.events);
  }
}

export function foldEvents(state: RunView, events: readonly RunEvent[]): RunView {
  let s = state;
  for (const ev of events) s = applyEvent(s, ev);
  return s;
}

function replaceItem<T extends RunItem>(items: RunItem[], index: number, patch: Partial<T>): RunItem[] {
  const next = items.slice();
  next[index] = { ...(items[index] as T), ...patch };
  return next;
}

function findLast(items: readonly RunItem[], pred: (item: RunItem) => boolean): number {
  for (let i = items.length - 1; i >= 0; i--) {
    if (pred(items[i] as RunItem)) return i;
  }
  return -1;
}

function findTool(items: readonly RunItem[], callId: string): number {
  return findLast(items, (it) => it.kind === "tool" && it.callId === callId);
}

/** Ends the streaming thinking block and/or assistant message. */
function closeOpen(s: RunView, ts: number, which: "thinking" | "both"): RunView {
  let items = s.items;
  let { openThinking, openAssistant } = s;
  if (openThinking >= 0) {
    items = replaceItem<ThinkingItem>(items, openThinking, { streaming: false, endTs: ts });
    openThinking = -1;
  }
  if (which === "both" && openAssistant >= 0) {
    items = replaceItem<AssistantItem>(items, openAssistant, { streaming: false });
    openAssistant = -1;
  }
  return items === s.items ? s : { ...s, items, openThinking, openAssistant };
}

function push(s: RunView, item: RunItem): RunView {
  return { ...s, items: [...s.items, item] };
}

/** The model step is over: a later `message.done` belongs to a new step. */
function endStep(s: RunView): RunView {
  return s.stepAssistant < 0 ? s : { ...s, stepAssistant: -1 };
}

const TERMINAL_NOTICE: ReadonlySet<RunState> = new Set(["failed", "cancelled"]);

export function applyEvent(prev: RunView, ev: RunEvent): RunView {
  if (typeof ev.seq !== "number" || ev.seq <= prev.lastSeq) return prev;
  if (prev.runId !== null && ev.run_id !== prev.runId) return prev;
  const base: RunView = { ...prev, lastSeq: ev.seq, runId: prev.runId ?? ev.run_id };
  const at = { seq: ev.seq, ts: ev.ts };

  switch (ev.type) {
    case "run.state": {
      let s: RunView = { ...base, state: ev.state, reason: ev.reason ?? null };
      if (ev.state !== "running") s = endStep(closeOpen(s, ev.ts, "both"));
      if (TERMINAL_NOTICE.has(ev.state)) {
        s = push(s, { kind: "notice", key: `n${ev.seq}`, ...at, state: ev.state, reason: ev.reason ?? null });
      }
      return s;
    }

    case "message.user": {
      const s = endStep(closeOpen(base, ev.ts, "both"));
      return push({ ...s, turnError: null }, { kind: "user", key: `u${ev.seq}`, ...at, text: ev.text, user: ev.user });
    }

    case "thinking.delta": {
      if (base.openThinking >= 0) {
        const cur = base.items[base.openThinking] as ThinkingItem;
        return {
          ...base,
          items: replaceItem<ThinkingItem>(base.items, base.openThinking, { text: cur.text + ev.text, endTs: ev.ts }),
        };
      }
      const s = closeOpen(base, ev.ts, "both");
      return {
        ...push(s, { kind: "thinking", key: `t${ev.seq}`, ...at, text: ev.text, streaming: true, endTs: ev.ts }),
        openThinking: s.items.length,
      };
    }

    case "message.delta": {
      const s = closeOpen(base, ev.ts, "thinking");
      if (s.openAssistant >= 0) {
        const cur = s.items[s.openAssistant] as AssistantItem;
        return { ...s, items: replaceItem<AssistantItem>(s.items, s.openAssistant, { text: cur.text + ev.text }) };
      }
      return {
        ...push(s, { kind: "assistant", key: `a${ev.seq}`, ...at, text: ev.text, streaming: true }),
        openAssistant: s.items.length,
        stepAssistant: s.stepAssistant >= 0 ? s.stepAssistant : s.items.length,
      };
    }

    case "message.done": {
      const s = { ...closeOpen(base, ev.ts, "thinking"), openAssistant: -1, stepAssistant: -1 };
      const first = base.stepAssistant;
      if (first < 0) return push(s, { kind: "assistant", key: `a${ev.seq}`, ...at, text: ev.text, streaming: false });
      const own = s.items.flatMap((it, i) => (i >= first && it.kind === "assistant" ? [i] : []));
      let items = s.items;
      if (own.length === 1) {
        // The authoritative text of the step's one message.
        items = replaceItem<AssistantItem>(items, first, { text: ev.text, streaming: false });
      } else {
        // The text went on after a tool call: keep the pieces where they streamed.
        for (const i of own) items = replaceItem<AssistantItem>(items, i, { streaming: false });
      }
      return { ...s, items };
    }

    case "tool.call": {
      const s = closeOpen(base, ev.ts, "both");
      const args = isPlainObject(ev.args) ? ev.args : {};
      const i = findTool(s.items, ev.call_id);
      if (i >= 0) {
        const cur = s.items[i] as ToolItem;
        return {
          ...s,
          items: replaceItem<ToolItem>(s.items, i, {
            tool: ev.tool,
            tier: ev.tier,
            args: Object.keys(args).length > 0 ? args : cur.args,
            argsComplete: true,
          }),
        };
      }
      const tool = newTool(ev.call_id, ev.tool, ev.tier, at, args, "");
      return push(s, { ...tool, argsComplete: Object.keys(args).length > 0 });
    }

    case "tool.args.delta": {
      const i = findTool(base.items, ev.call_id);
      if (i >= 0) {
        const cur = base.items[i] as ToolItem;
        return { ...base, items: replaceItem<ToolItem>(base.items, i, { argsText: cur.argsText + ev.delta }) };
      }
      // A delta before its tool.call (should not happen): keep the text anyway.
      const s = closeOpen(base, ev.ts, "both");
      return push(s, newTool(ev.call_id, "", "R", at, {}, ev.delta));
    }

    case "tool.result": {
      const result: ToolResultInfo = {
        ok: ev.ok,
        summary: ev.summary,
        durationMs: ev.duration_ms,
        ...(ev.data !== undefined ? { data: ev.data } : {}),
        ...(ev.handle !== undefined ? { handle: ev.handle } : {}),
      };
      const status: ToolStatus = ev.ok ? "ok" : "error";
      const done = endStep(base);
      const i = findTool(done.items, ev.call_id);
      if (i >= 0) return { ...done, items: replaceItem<ToolItem>(done.items, i, { result, status }) };
      const s = closeOpen(done, ev.ts, "both");
      return push(s, { ...newTool(ev.call_id, "", "R", at, {}, ""), result, status });
    }

    case "approval.request":
    case "approval.decided": {
      const approval = ev.approval;
      let s = ev.type === "approval.request" ? closeOpen(base, ev.ts, "both") : base;
      const t = findTool(s.items, approval.call_id);
      if (t >= 0) {
        const tool = s.items[t] as ToolItem;
        const status: ToolStatus =
          approval.state === "pending" ? "approval" : tool.status === "approval" ? "running" : tool.status;
        s = { ...s, items: replaceItem<ToolItem>(s.items, t, { approvalId: approval.id, status }) };
      }
      const a = findLast(s.items, (it) => it.kind === "approval" && it.approval.id === approval.id);
      if (a >= 0) return { ...s, items: replaceItem<ApprovalItem>(s.items, a, { approval }) };
      return push(s, { kind: "approval", key: `ap-${approval.id}`, ...at, approval });
    }

    case "question": {
      let s = closeOpen(base, ev.ts, "both");
      const t = findTool(s.items, ev.call_id);
      if (t >= 0) s = { ...s, items: replaceItem<ToolItem>(s.items, t, { questionId: ev.question_id, status: "question" }) };
      const q = findLast(s.items, (it) => it.kind === "question" && it.questionId === ev.question_id);
      if (q >= 0) return s;
      return push(s, {
        kind: "question",
        key: `q-${ev.question_id}`,
        ...at,
        questionId: ev.question_id,
        callId: ev.call_id,
        text: ev.text,
        options: Array.isArray(ev.options) ? ev.options : [],
        answer: null,
        answeredBy: null,
      });
    }

    case "question.answered": {
      let s = base;
      const q = findLast(s.items, (it) => it.kind === "question" && it.questionId === ev.question_id);
      if (q < 0) return s;
      s = { ...s, items: replaceItem<QuestionItem>(s.items, q, { answer: ev.answer, answeredBy: ev.user }) };
      const t = findLast(s.items, (it) => it.kind === "tool" && it.questionId === ev.question_id);
      if (t >= 0 && (s.items[t] as ToolItem).status === "question") {
        s = { ...s, items: replaceItem<ToolItem>(s.items, t, { status: "running" }) };
      }
      return s;
    }

    case "plan.updated":
      return { ...base, plan: { planId: ev.plan_id, revision: ev.revision, changes: ev.changes, seq: ev.seq } };

    case "tests.updated":
      return { ...base, tests: { results: Array.isArray(ev.results) ? ev.results : [], seq: ev.seq } };

    case "error": {
      const s = endStep(closeOpen(base, ev.ts, "both"));
      const item: ErrorItem = { kind: "error", key: `e${ev.seq}`, ...at, message: ev.message, code: ev.code ?? null };
      return push({ ...s, turnError: ev.message }, item);
    }

    default:
      // Unknown event types come from a newer hub; skip them but keep the seq.
      return base;
  }
}

function newTool(
  callId: string,
  tool: string,
  tier: Tier,
  at: { seq: number; ts: number },
  args: Record<string, unknown>,
  argsText: string,
): ToolItem {
  return {
    kind: "tool",
    key: `c-${callId}`,
    ...at,
    callId,
    tool,
    tier,
    args,
    argsText,
    argsComplete: false,
    status: "running",
    result: null,
    approvalId: null,
    questionId: null,
  };
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// ------------------------------------------------------------ selectors

export function pendingApprovals(view: RunView): Approval[] {
  return view.items.flatMap((it) => (it.kind === "approval" && it.approval.state === "pending" ? [it.approval] : []));
}

/** True while the run's questions can still be answered (it may not have switched to waiting yet). */
export function acceptsAnswers(state: RunState | null): boolean {
  return state === "waiting_answer" || state === "running";
}

/** Unanswered questions the run still waits for; none once it ended or was cancelled. */
export function openQuestions(view: RunView): QuestionItem[] {
  if (!acceptsAnswers(view.state)) return [];
  return view.items.filter((it): it is QuestionItem => it.kind === "question" && it.answer === null);
}

/** True while the agent is working and the composer must wait. */
export function isBusy(state: RunState | null): boolean {
  return state === "running";
}

/** True while the run has not finished its turn (it can be cancelled). */
export function isActive(state: RunState | null): boolean {
  return state === "running" || state === "waiting_approval" || state === "waiting_answer";
}
