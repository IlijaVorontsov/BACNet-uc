import { describe, expect, it } from "vitest";
import type { Approval, RunEvent } from "../api/types";
import {
  applyEvent,
  foldEvents,
  initialRunView,
  isBusy,
  openQuestions,
  pendingApprovals,
  runReducer,
  type ApprovalItem,
  type AssistantItem,
  type QuestionItem,
  type RunView,
  type ThinkingItem,
  type ToolItem,
} from "./runReducer";

type Body = RunEvent extends infer E ? (E extends RunEvent ? Omit<E, "seq" | "run_id" | "ts"> : never) : never;

function events(...bodies: Body[]): RunEvent[] {
  return bodies.map((b, i) => ({ ...b, seq: i + 1, run_id: "r_1", ts: 1000 + i }) as RunEvent);
}

function approval(state: Approval["state"], extra: Partial<Approval> = {}): Approval {
  return {
    id: "a_12",
    run_id: "r_1",
    call_id: "call_3",
    tool: "apply",
    tier: "C",
    title: "Apply plan p17",
    summary: ["r204-ctl: io.json +1 point"],
    diff: "--- a/io.json\n+++ b/io.json",
    rollback: "Apply revision 16",
    plan_id: "p17",
    state,
    requested_at: 1000,
    expires_at: 2800,
    requested_by: "dev",
    decided_by: state === "pending" ? null : "dev",
    decided_at: state === "pending" ? null : 1100,
    comment: null,
    ...extra,
  };
}

const fold = (evs: RunEvent[], view: RunView = initialRunView("r_1")) => foldEvents(view, evs);

describe("runReducer", () => {
  it("builds user and streamed assistant messages", () => {
    const v = fold(
      events(
        { type: "run.state", state: "running" },
        { type: "message.user", text: "Commission room 204", user: "dev" },
        { type: "message.delta", text: "The plan " },
        { type: "message.delta", text: "is ready." },
      ),
    );
    expect(v.state).toBe("running");
    expect(v.items.map((i) => i.kind)).toEqual(["user", "assistant"]);
    const a = v.items[1] as AssistantItem;
    expect(a.text).toBe("The plan is ready.");
    expect(a.streaming).toBe(true);
    expect(v.openAssistant).toBe(1);

    const done = applyEvent(v, { type: "message.done", text: "The plan is ready.", seq: 5, run_id: "r_1", ts: 1005 });
    const a2 = done.items[1] as AssistantItem;
    expect(a2.streaming).toBe(false);
    expect(done.openAssistant).toBe(-1);
  });

  it("uses message.done as the authoritative text and handles done without deltas", () => {
    const v = fold(
      events(
        { type: "message.delta", text: "Partial" },
        { type: "message.done", text: "Full text" },
        { type: "message.done", text: "Second message" },
      ),
    );
    expect(v.items.map((i) => (i as AssistantItem).text)).toEqual(["Full text", "Second message"]);
    expect(v.items.every((i) => !(i as AssistantItem).streaming)).toBe(true);
  });

  it("collapses thinking into one block that ends when the answer starts", () => {
    const v = fold(
      events(
        { type: "thinking.delta", text: "r204-ctl is new " },
        { type: "thinking.delta", text: "and has no io.json." },
        { type: "message.delta", text: "Plan:" },
      ),
    );
    expect(v.items.map((i) => i.kind)).toEqual(["thinking", "assistant"]);
    const t = v.items[0] as ThinkingItem;
    expect(t.text).toBe("r204-ctl is new and has no io.json.");
    expect(t.streaming).toBe(false);
    expect(t.endTs).toBe(1002);
    expect(v.openThinking).toBe(-1);
  });

  it("starts a new thinking block after a tool call", () => {
    const v = fold(
      events(
        { type: "thinking.delta", text: "first" },
        { type: "tool.call", call_id: "c1", tool: "site_search", tier: "R", args: { query: "r204" } },
        { type: "thinking.delta", text: "second" },
      ),
    );
    expect(v.items.map((i) => i.kind)).toEqual(["thinking", "tool", "thinking"]);
    expect((v.items[0] as ThinkingItem).streaming).toBe(false);
    expect((v.items[2] as ThinkingItem).streaming).toBe(true);
  });

  it("streams tool arguments and fills the result", () => {
    const v = fold(
      events(
        { type: "tool.call", call_id: "c1", tool: "manifest_edit", tier: "S", args: {} },
        { type: "tool.args.delta", call_id: "c1", delta: '{"patch": [' },
        { type: "tool.args.delta", call_id: "c1", delta: '{"op": "add"}]}' },
      ),
    );
    let t = v.items[0] as ToolItem;
    expect(t.argsText).toBe('{"patch": [{"op": "add"}]}');
    expect(t.args).toEqual({});
    expect(t.status).toBe("running");

    const complete = applyEvent(v, {
      type: "tool.call",
      call_id: "c1",
      tool: "manifest_edit",
      tier: "S",
      args: { patch: [{ op: "add" }] },
      seq: 4,
      run_id: "r_1",
      ts: 1004,
    });
    const done = applyEvent(complete, {
      type: "tool.result",
      call_id: "c1",
      ok: true,
      summary: "Draft is valid",
      duration_ms: 120,
      handle: "result://r42",
      seq: 5,
      run_id: "r_1",
      ts: 1005,
    });
    expect(done.items).toHaveLength(1);
    t = done.items[0] as ToolItem;
    expect(t.args).toEqual({ patch: [{ op: "add" }] });
    expect(t.status).toBe("ok");
    expect(t.result).toEqual({ ok: true, summary: "Draft is valid", durationMs: 120, handle: "result://r42" });
  });

  it("marks failed tool results", () => {
    const v = fold(
      events(
        { type: "tool.call", call_id: "c1", tool: "point_write", tier: "L", args: { point: "x" } },
        { type: "tool.result", call_id: "c1", ok: false, summary: "denied", duration_ms: 3 },
      ),
    );
    expect((v.items[0] as ToolItem).status).toBe("error");
  });

  it("keeps a result whose tool.call never arrived", () => {
    const v = fold(events({ type: "tool.result", call_id: "zz", ok: true, summary: "orphan", duration_ms: 1 }));
    expect(v.items).toHaveLength(1);
    expect((v.items[0] as ToolItem).result?.summary).toBe("orphan");
  });

  it("tracks an approval from request to decision and links it to the tool", () => {
    const v = fold(
      events(
        { type: "tool.call", call_id: "call_3", tool: "apply", tier: "C", args: { plan_id: "p17" } },
        { type: "approval.request", approval: approval("pending") },
        { type: "run.state", state: "waiting_approval" },
      ),
    );
    expect(v.items.map((i) => i.kind)).toEqual(["tool", "approval"]);
    expect((v.items[0] as ToolItem).status).toBe("approval");
    expect((v.items[0] as ToolItem).approvalId).toBe("a_12");
    expect(pendingApprovals(v).map((a) => a.id)).toEqual(["a_12"]);
    expect(isBusy(v.state)).toBe(false);

    const decided = foldEvents(v, [
      { type: "approval.decided", approval: approval("approved"), seq: 4, run_id: "r_1", ts: 1004 },
      { type: "run.state", state: "running", seq: 5, run_id: "r_1", ts: 1005 },
    ]);
    expect(decided.items).toHaveLength(2);
    expect((decided.items[1] as ApprovalItem).approval.state).toBe("approved");
    expect((decided.items[0] as ToolItem).status).toBe("running");
    expect(pendingApprovals(decided)).toEqual([]);
    expect(isBusy(decided.state)).toBe(true);
  });

  it("adds an approval.decided without a prior request", () => {
    const v = fold(events({ type: "approval.decided", approval: approval("rejected") }));
    expect((v.items[0] as ApprovalItem).approval.state).toBe("rejected");
  });

  it("handles questions and answers", () => {
    const v = fold(
      events(
        { type: "tool.call", call_id: "c9", tool: "ask_user", tier: "R", args: { question: "Open?" } },
        { type: "question", question_id: "q1", call_id: "c9", text: "Is the valve open?", options: ["Yes", "No"] },
        { type: "run.state", state: "waiting_answer" },
      ),
    );
    expect(openQuestions(v)).toHaveLength(1);
    expect((v.items[0] as ToolItem).status).toBe("question");
    expect((v.items[0] as ToolItem).questionId).toBe("q1");

    const answered = foldEvents(v, [
      { type: "question.answered", question_id: "q1", answer: "Yes", user: "tech1", seq: 4, run_id: "r_1", ts: 1004 },
      { type: "tool.result", call_id: "c9", ok: true, summary: "Yes", duration_ms: 9000, seq: 5, run_id: "r_1", ts: 1005 },
    ]);
    const q = answered.items[1] as QuestionItem;
    expect(q.answer).toBe("Yes");
    expect(q.answeredBy).toBe("tech1");
    expect(openQuestions(answered)).toEqual([]);
    expect((answered.items[0] as ToolItem).status).toBe("ok");
  });

  it("stops counting a question as open once the run no longer waits for it", () => {
    const asked = fold(
      events(
        { type: "question", question_id: "q1", call_id: "c9", text: "Is the valve open?", options: ["Yes"] },
        { type: "run.state", state: "waiting_answer" },
      ),
    );
    expect(openQuestions(asked)).toHaveLength(1);
    for (const state of ["cancelled", "failed", "idle"] as const) {
      const ended = foldEvents(asked, [{ type: "run.state", state, seq: 3, run_id: "r_1", ts: 1003 }]);
      expect(openQuestions(ended)).toEqual([]);
    }
  });

  it("ignores an answer for an unknown question and non-array options", () => {
    const v = fold(
      events(
        { type: "question.answered", question_id: "nope", answer: "x", user: "u" },
        { type: "question", question_id: "q2", call_id: "c", text: "Name?", options: null as unknown as string[] },
      ),
    );
    expect(v.items).toHaveLength(1);
    expect((v.items[0] as QuestionItem).options).toEqual([]);
    expect(v.lastSeq).toBe(2);
  });

  it("records plan and test updates", () => {
    const v = fold(
      events(
        { type: "plan.updated", plan_id: "p17", revision: 17, changes: 4 },
        {
          type: "tests.updated",
          results: [{ name: "t", status: "pass", duration_ms: 1, failed_step: null, detail: "", target: "sim" }],
        },
        { type: "plan.updated", plan_id: null, revision: null, changes: 0 },
      ),
    );
    expect(v.plan).toEqual({ planId: null, revision: null, changes: 0, seq: 3 });
    expect(v.tests?.results[0]?.status).toBe("pass");
    expect(v.tests?.seq).toBe(2);
  });

  it("shows errors and terminal states, and closes open streams", () => {
    const v = fold(
      events(
        { type: "message.delta", text: "Working" },
        { type: "error", message: "LLM timeout", code: "timeout" },
        { type: "run.state", state: "failed", reason: "llm timeout" },
      ),
    );
    expect(v.items.map((i) => i.kind)).toEqual(["assistant", "error", "notice"]);
    expect((v.items[0] as AssistantItem).streaming).toBe(false);
    expect(v.state).toBe("failed");
    expect(v.reason).toBe("llm timeout");
  });

  it("ignores duplicate and older seq numbers, so replay after reconnect is harmless", () => {
    const evs = events(
      { type: "message.user", text: "hi", user: "dev" },
      { type: "message.delta", text: "Hel" },
      { type: "message.delta", text: "lo" },
    );
    const once = fold(evs);
    const twice = foldEvents(once, evs);
    expect(twice).toBe(once);
    const overlapping = foldEvents(fold(evs.slice(0, 2)), evs.slice(1));
    expect((overlapping.items[1] as AssistantItem).text).toBe("Hello");
    expect(overlapping).toEqual(once);
  });

  it("gives the same view for replay in one batch and live one by one", () => {
    const evs = events(
      { type: "run.state", state: "running" },
      { type: "message.user", text: "IO checkout", user: "dev" },
      { type: "thinking.delta", text: "four channels" },
      { type: "tool.call", call_id: "c1", tool: "io_force", tier: "L", args: {} },
      { type: "tool.args.delta", call_id: "c1", delta: '{"channel":"ao0"}' },
      { type: "tool.call", call_id: "c1", tool: "io_force", tier: "L", args: { channel: "ao0" } },
      { type: "tool.result", call_id: "c1", ok: true, summary: "Forced", duration_ms: 300 },
      { type: "message.delta", text: "Is it open?" },
      { type: "message.done", text: "Is it open?" },
      { type: "run.state", state: "idle" },
    );
    const batch = runReducer(initialRunView("r_1"), { type: "events", events: evs });
    let live = initialRunView("r_1");
    for (const ev of evs) live = runReducer(live, { type: "events", events: [ev] });
    expect(live).toEqual(batch);
    expect(batch.state).toBe("idle");
    expect(batch.items.map((i) => i.kind)).toEqual(["user", "thinking", "tool", "assistant"]);
  });

  it("ignores events of other runs and adopts the run id when unset", () => {
    const other = { type: "message.user", text: "x", user: "u", seq: 1, run_id: "r_2", ts: 1 } as const;
    expect(applyEvent(initialRunView("r_1"), other).items).toHaveLength(0);
    expect(applyEvent(initialRunView(null), other).runId).toBe("r_2");
  });

  it("skips unknown event types but advances seq", () => {
    const v = applyEvent(initialRunView("r_1"), {
      type: "future.thing",
      seq: 7,
      run_id: "r_1",
      ts: 1,
    } as unknown as RunEvent);
    expect(v.items).toHaveLength(0);
    expect(v.lastSeq).toBe(7);
  });

  it("does not mutate the previous state", () => {
    const v1 = fold(events({ type: "message.delta", text: "a" }));
    const snapshot = JSON.parse(JSON.stringify(v1)) as RunView;
    applyEvent(v1, { type: "message.delta", text: "b", seq: 2, run_id: "r_1", ts: 2 });
    expect(v1).toEqual(snapshot);
  });

  it("resets for another run and keeps a fresh state for the same one", () => {
    const fresh = initialRunView("r_1");
    expect(runReducer(fresh, { type: "reset", runId: "r_1" })).toBe(fresh);
    const used = fold(events({ type: "message.delta", text: "a" }));
    expect(runReducer(used, { type: "reset", runId: "r_1" }).items).toHaveLength(0);
    expect(runReducer(used, { type: "reset", runId: "r_9" }).runId).toBe("r_9");
  });
});
