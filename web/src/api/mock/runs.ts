/**
 * Scripted agent runs for the mock backend.
 *
 * A script is an async function driving a `ScriptContext`, which emits the
 * same events the hub's agent loop would (API.md "Run events"). Seeded runs
 * start in fast-forward: sleeps only advance a virtual clock, so the history
 * is generated instantly with plausible timestamps, until the script reaches
 * a gate (approval or question) nobody has answered yet. From then on it runs
 * in real time.
 */

import type { Approval, RunEvent, RunState, RunSummary, Tier } from "../types";
import type { MockSite } from "./site";

/** An event without the envelope fields the run assigns. */
export type EventBody = RunEvent extends infer E ? (E extends RunEvent ? Omit<E, "seq" | "run_id" | "ts"> : never) : never;

export class Cancelled extends Error {
  constructor() {
    super("run cancelled");
    this.name = "Cancelled";
  }
}

export type Decision = "approved" | "rejected" | "expired";

interface ApprovalGate {
  approval: Approval;
  resolve: (d: Decision) => void;
  reject: (e: Error) => void;
  timer: ReturnType<typeof setTimeout> | null;
}

interface QuestionGate {
  questionId: string;
  resolve: (a: { answer: string; user: string }) => void;
  reject: (e: Error) => void;
}

export interface ToolOutcome {
  ok: boolean;
  summary: string;
  data?: unknown;
  handle?: string;
}

export interface ApprovalSpec {
  title: string;
  summary: string[];
  diff: string;
  rollback: string;
  planId: string | null;
}

export interface Ids {
  next(prefix: string): string;
}

export class MockRun {
  readonly summary: RunSummary;
  readonly events: RunEvent[] = [];
  private readonly listeners = new Set<(ev: RunEvent) => void>();
  private abort = new AbortController();
  approvalGate: ApprovalGate | null = null;
  questionGate: QuestionGate | null = null;
  /** Who approved the run's tier L calls for the rest of the run (scope "run"). */
  runApprover: string | null = null;

  constructor(id: string, title: string, createdBy: string, createdAt: number, model: string) {
    this.summary = {
      id,
      title,
      state: "idle",
      created_at: createdAt,
      updated_at: createdAt,
      created_by: createdBy,
      last_seq: 0,
      model,
    };
  }

  get signal(): AbortSignal {
    return this.abort.signal;
  }

  get cancelled(): boolean {
    return this.abort.signal.aborted;
  }

  emit(body: EventBody, ts: number): RunEvent {
    const ev = { ...body, seq: this.summary.last_seq + 1, run_id: this.summary.id, ts } as RunEvent;
    this.events.push(ev);
    this.summary.last_seq = ev.seq;
    this.summary.updated_at = ts;
    if (ev.type === "run.state") this.summary.state = ev.state;
    for (const fn of this.listeners) {
      try {
        fn(ev);
      } catch (err) {
        console.error("mock run listener failed", err);
      }
    }
    return ev;
  }

  subscribe(fn: (ev: RunEvent) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** A new turn after a cancellation gets a fresh cancellation signal. */
  newTurn(): void {
    if (this.abort.signal.aborted) this.abort = new AbortController();
  }

  /** Stops the run; open gates close, so a late answer or expiry timer cannot reach them. */
  cancel(): void {
    this.abort.abort();
    const err = new Cancelled();
    const { approvalGate, questionGate } = this;
    this.approvalGate = null;
    this.questionGate = null;
    if (approvalGate?.timer) clearTimeout(approvalGate.timer);
    approvalGate?.reject(err);
    questionGate?.reject(err);
  }

  /** Called by POST /api/approvals/{id}; the approval object is updated by the caller. */
  decide(decision: Decision): void {
    const gate = this.approvalGate;
    if (!gate) return;
    if (gate.timer) clearTimeout(gate.timer);
    this.approvalGate = null;
    gate.resolve(decision);
  }

  answer(questionId: string, answer: string, user: string): boolean {
    const gate = this.questionGate;
    if (!gate || gate.questionId !== questionId) return false;
    this.questionGate = null;
    gate.resolve({ answer, user });
    return true;
  }
}

export interface ContextOptions {
  site: MockSite;
  ids: Ids;
  /** Registers an approval so GET/POST /api/approvals can see it. */
  addApproval: (a: Approval) => void;
  user: string;
  speed: number;
  /** Start of the virtual clock (Unix s) for fast-forward; null = real time from the start. */
  virtualStart: number | null;
  /** Answers given to questions while fast-forwarding, as [answer, user]. */
  seedAnswers?: [string, string][];
  /** Called when fast-forward ends, i.e. the run waits for a person. */
  onLive?: () => void;
  approvalTtlS?: number;
}

export class ScriptContext {
  readonly site: MockSite;
  readonly user: string;
  private virtual: number | null;
  private readonly seedAnswers: [string, string][];
  private readonly opts: ContextOptions;

  constructor(
    readonly run: MockRun,
    opts: ContextOptions,
  ) {
    this.opts = opts;
    this.site = opts.site;
    this.user = opts.user;
    this.virtual = opts.virtualStart;
    this.seedAnswers = [...(opts.seedAnswers ?? [])];
  }

  get fastForward(): boolean {
    return this.virtual !== null;
  }

  now(): number {
    return this.virtual ?? Date.now() / 1000;
  }

  private goLive(): void {
    if (this.virtual === null) return;
    this.virtual = null;
    this.opts.onLive?.();
  }

  async sleep(ms: number): Promise<void> {
    if (this.run.cancelled) throw new Cancelled();
    if (this.virtual !== null) {
      this.virtual += ms / 1000;
      await Promise.resolve();
      return;
    }
    const signal = this.run.signal;
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", onAbort);
        resolve();
      }, ms / this.opts.speed);
      const onAbort = (): void => {
        clearTimeout(timer);
        reject(new Cancelled());
      };
      signal.addEventListener("abort", onAbort, { once: true });
    });
  }

  emit(body: EventBody): RunEvent {
    if (this.run.cancelled) throw new Cancelled();
    return this.run.emit(body, this.now());
  }

  state(state: RunState, reason?: string): void {
    if (this.run.summary.state === state && reason === undefined) return;
    this.emit(reason === undefined ? { type: "run.state", state } : { type: "run.state", state, reason });
  }

  userMessage(text: string, user = this.user): void {
    this.emit({ type: "message.user", text, user });
  }

  private async stream(kind: "thinking.delta" | "message.delta", text: string, msPerChunk: number): Promise<void> {
    const parts = text.match(/\S+\s*/g) ?? [text];
    for (let i = 0; i < parts.length; i += 3) {
      this.emit({ type: kind, text: parts.slice(i, i + 3).join("") });
      await this.sleep(msPerChunk);
    }
  }

  async think(text: string): Promise<void> {
    await this.stream("thinking.delta", text, 70);
  }

  async say(text: string): Promise<void> {
    await this.stream("message.delta", text, 45);
    this.emit({ type: "message.done", text });
  }

  private async callStart(tool: string, tier: Tier, args: Record<string, unknown>): Promise<string> {
    const callId = this.opts.ids.next("call_");
    this.emit({ type: "tool.call", call_id: callId, tool, tier, args: {} });
    const json = JSON.stringify(args);
    const size = Math.max(8, Math.ceil(json.length / 3));
    for (let i = 0; i < json.length; i += size) {
      this.emit({ type: "tool.args.delta", call_id: callId, delta: json.slice(i, i + size) });
      await this.sleep(40);
    }
    this.emit({ type: "tool.call", call_id: callId, tool, tier, args });
    return callId;
  }

  private finish(callId: string, tool: string, tier: Tier, args: Record<string, unknown>, out: ToolOutcome, durationMs: number): void {
    this.emit({
      type: "tool.result",
      call_id: callId,
      ok: out.ok,
      summary: out.summary,
      duration_ms: durationMs,
      ...(out.data !== undefined ? { data: out.data } : {}),
      ...(out.handle !== undefined ? { handle: out.handle } : {}),
    });
    this.site.addAudit({
      user: this.user,
      run_id: this.run.summary.id,
      action: "tool",
      tool,
      tier,
      args,
      outcome: out.ok ? "ok" : "error",
      detail: out.summary,
    });
  }

  /** Runs one tool call: streamed arguments, a (shortened) wait, then the result. */
  async tool(
    tool: string,
    tier: Tier,
    args: Record<string, unknown>,
    durationMs: number,
    exec: () => ToolOutcome | Promise<ToolOutcome>,
  ): Promise<ToolOutcome> {
    const callId = await this.callStart(tool, tier, args);
    await this.sleep(this.fastForward ? durationMs : Math.min(durationMs, 1600));
    const out = await exec();
    this.finish(callId, tool, tier, args, out, durationMs);
    return out;
  }

  /** A tool call that waits for a human decision before it runs. */
  async gatedTool(
    tool: string,
    tier: Tier,
    args: Record<string, unknown>,
    spec: ApprovalSpec,
    durationMs: number,
    exec: () => ToolOutcome | Promise<ToolOutcome>,
  ): Promise<{ decision: Decision; outcome: ToolOutcome }> {
    const callId = await this.callStart(tool, tier, args);
    const approver = this.run.runApprover;
    if (tier === "L" && approver !== null) {
      // Covered by the run-wide approval: runs at once, audited as such.
      this.site.addAudit({
        user: approver,
        run_id: this.run.summary.id,
        action: "approval",
        tool,
        tier,
        args,
        outcome: "approved",
        detail: `covered by the run-wide approval of ${approver}`,
      });
      const started = this.now();
      await this.sleep(Math.min(durationMs, 2200));
      const outcome = await exec();
      this.finish(callId, tool, tier, args, outcome, Math.max(durationMs, Math.round((this.now() - started) * 1000)));
      return { decision: "approved", outcome };
    }
    const requested = this.now();
    const ttl = this.opts.approvalTtlS ?? 1800;
    const approval: Approval = {
      id: this.opts.ids.next("a_"),
      run_id: this.run.summary.id,
      call_id: callId,
      tool,
      tier,
      title: spec.title,
      summary: spec.summary,
      diff: spec.diff,
      rollback: spec.rollback,
      plan_id: spec.planId,
      state: "pending",
      scope: "call",
      requested_at: requested,
      expires_at: requested + ttl,
      requested_by: this.run.summary.created_by,
      decided_by: null,
      decided_at: null,
      comment: null,
    };
    this.opts.addApproval(approval);
    this.emit({ type: "approval.request", approval: { ...approval } });
    this.state("waiting_approval");
    this.goLive();
    const decision = await new Promise<Decision>((resolve, reject) => {
      const expireIn = Math.max(0, (approval.expires_at - Date.now() / 1000) * 1000);
      const timer = setTimeout(() => {
        approval.state = "expired";
        approval.decided_at = Date.now() / 1000;
        this.run.decide("expired");
      }, expireIn);
      this.run.approvalGate = { approval, resolve, reject, timer };
    }).catch((err: unknown) => {
      if (err instanceof Cancelled && approval.state === "pending") {
        approval.state = "rejected";
        approval.comment = "run cancelled";
        approval.decided_at = Date.now() / 1000;
        this.run.emit({ type: "approval.decided", approval: { ...approval } }, Date.now() / 1000);
      }
      throw err;
    });
    this.emit({ type: "approval.decided", approval: { ...approval } });
    this.site.addAudit({
      user: approval.decided_by ?? "system",
      run_id: this.run.summary.id,
      action: `approval.${decision}`,
      tool,
      tier,
      args,
      outcome: decision,
      detail: approval.title,
    });
    this.state("running");
    if (decision !== "approved") {
      const summary =
        decision === "expired"
          ? "Approval expired; nothing was changed"
          : `Rejected by ${approval.decided_by ?? "user"}${approval.comment ? `: ${approval.comment}` : ""}`;
      const outcome = { ok: false, summary };
      this.finish(callId, tool, tier, args, outcome, 0);
      return { decision, outcome };
    }
    const started = this.now();
    await this.sleep(Math.min(durationMs, 2200));
    const outcome = await exec();
    this.finish(callId, tool, tier, args, outcome, Math.max(durationMs, Math.round((this.now() - started) * 1000)));
    return { decision, outcome };
  }

  /** `ask_user`: a question event, then the answer from /answer (or a seeded one). */
  async ask(text: string, options: string[]): Promise<string> {
    const args = { question: text, options };
    const callId = await this.callStart("ask_user", "R", args);
    const questionId = this.opts.ids.next("q_");
    const asked = this.now();
    this.emit({ type: "question", question_id: questionId, call_id: callId, text, options });
    let answer: { answer: string; user: string };
    const seeded = this.fastForward ? this.seedAnswers.shift() : undefined;
    if (seeded) {
      await this.sleep(9000);
      answer = { answer: seeded[0], user: seeded[1] };
    } else {
      this.state("waiting_answer");
      this.goLive();
      answer = await new Promise((resolve, reject) => {
        this.run.questionGate = { questionId, resolve, reject };
      });
    }
    this.emit({ type: "question.answered", question_id: questionId, answer: answer.answer, user: answer.user });
    this.state("running");
    this.finish(callId, "ask_user", "R", args, { ok: true, summary: `Answered: ${answer.answer}` }, Math.round((this.now() - asked) * 1000));
    return answer.answer;
  }
}

/** Runs a script to completion and turns its end into the right run.state. */
export async function runScript(ctx: ScriptContext, script: (ctx: ScriptContext) => Promise<void>): Promise<void> {
  try {
    await script(ctx);
    ctx.state("idle");
  } catch (err) {
    if (err instanceof Cancelled) {
      ctx.run.emit({ type: "run.state", state: "cancelled", reason: "cancelled by user" }, Date.now() / 1000);
      return;
    }
    console.error("mock run script failed", err);
    const message = err instanceof Error ? err.message : String(err);
    ctx.run.emit({ type: "error", message, code: "internal" }, Date.now() / 1000);
    ctx.run.emit({ type: "run.state", state: "failed", reason: message }, Date.now() / 1000);
  }
}
