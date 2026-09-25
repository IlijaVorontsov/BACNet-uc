// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { foldEvents, initialRunView, openQuestions, type AssistantItem, type RunView } from "../../state/runReducer";
import { ApiClient, ApiError } from "../client";
import type { Approval, Reading, RunEvent } from "../types";
import { MockRun, runScript, ScriptContext } from "./runs";
import { MockServer } from "./server";
import { MockSite } from "./site";

const ORIGIN = "http://mock.local";
let server: MockServer;
let client: ApiClient;
const realFetch = globalThis.fetch;

beforeEach(async () => {
  server = new MockServer({ speed: 200, latencyMs: 0, tickMs: 15, keepAliveMs: 50 });
  globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
    server.fetch(new Request(new URL(String(input), ORIGIN), init))) as typeof fetch;
  client = new ApiClient({ baseUrl: ORIGIN });
  await server.ready;
});

afterEach(() => {
  server.dispose();
  globalThis.fetch = realFetch;
});

/** Follows a run through the real SSE client and reducer until `until` holds. */
async function follow(runId: string, until: (v: RunView) => boolean, act?: (v: RunView) => Promise<void>): Promise<RunView> {
  let view = initialRunView(runId);
  const ctrl = new AbortController();
  const pending: RunEvent[] = [];
  const conn = client.runEvents(runId, 0, (ev) => pending.push(ev), { signal: ctrl.signal, initialDelayMs: 5 });
  try {
    await vi.waitFor(
      async () => {
        view = foldEvents(view, pending.splice(0));
        if (act) await act(view);
        view = foldEvents(view, pending.splice(0));
        expect(until(view)).toBe(true);
      },
      { timeout: 8000, interval: 10 },
    );
  } finally {
    ctrl.abort();
    await conn.done;
  }
  return view;
}

describe("MockServer", () => {
  it("serves the site, points and the seeded runs", async () => {
    const health = await client.health();
    expect(health).toMatchObject({ ok: true, site: "hq", dev_mode: true });
    const site = await client.site();
    expect(site.spaces.map((s) => s.id)).toEqual(expect.arrayContaining(["f2", "r201", "r205", "plant"]));
    expect(site.summary).toMatchObject({ devices: 9, online: 8, unassigned: 2, pending_changes: 6 });

    const floor = await client.points({ space: "f2", tag: "Zone_Air_Temperature_Sensor" });
    expect(floor.total).toBe(5);
    expect(floor.points[0]?.reading?.quality).toBeDefined();
    const search = await client.points({ q: "co2" });
    expect(search.points.map((p) => p.id)).toContain("hq/r204-co2/co2");

    const { runs } = await client.runs();
    expect(runs.map((r) => [r.title, r.state])).toEqual([
      ["IO checkout · r204-ctl", "waiting_answer"],
      ["Commission room 204", "waiting_approval"],
    ]);
    const { approvals } = await client.approvals("pending");
    expect(approvals.map((a) => [a.plan_id, a.tier])).toEqual([["p17", "C"]]);
    const { plan } = await client.plan();
    expect(plan?.changes).toHaveLength(6);
    const tests = await client.tests();
    expect(tests.results.filter((t) => t.target === "sim").every((t) => t.status === "pass")).toBe(true);
  });

  it("replays history in seq order with timestamps in the past", async () => {
    const { runs } = await client.runs();
    const commission = runs.find((r) => r.title === "Commission room 204");
    const view = await follow(commission!.id, (v) => v.state === "waiting_approval");
    expect(view.items[0]).toMatchObject({ kind: "user" });
    expect(view.items.filter((i) => i.kind === "tool").length).toBeGreaterThan(5);
    expect(view.items.at(-1)).toMatchObject({ kind: "approval" });
    expect(view.items[0]!.ts).toBeLessThan(Date.now() / 1000 - 300);
  });

  it("applies the plan when the approval is given", async () => {
    const { approvals } = await client.approvals("pending");
    const approval = approvals[0]!;
    let decided = false;
    const view = await follow(
      approval.run_id,
      (v) => v.state === "idle",
      async () => {
        if (decided) return;
        decided = true;
        const res = await client.decide(approval.id, { decision: "approve" });
        expect(res.state).toBe("approved");
      },
    );
    const last = view.items.at(-1) as AssistantItem;
    expect(last.text).toMatch(/Room 204 is commissioned/);
    expect(view.plan).toMatchObject({ planId: null, changes: 0 });
    expect((await client.plan()).plan).toBeNull();
    expect((await client.site()).summary.pending_changes).toBe(0);
    expect((await client.manifest()).live_revision).toBe(17);
    const live = (await client.tests()).results.filter((t) => t.target === "live");
    expect(live.map((t) => t.status)).toEqual(["pass", "pass", "pass"]);
    const r204 = await client.device("r204-ctl");
    expect(r204.points.map((p) => p.obj)).toContain("analog-value:20");
    expect(r204.apps.map((a) => a.name)).toEqual(["thermostat", "link"]);

    const again = await client.decide(approval.id, { decision: "approve" }).catch((e: unknown) => e);
    expect(again).toBeInstanceOf(ApiError);
    expect((again as ApiError).status).toBe(409);
    expect((again as ApiError).code).toBe("conflict");
  });

  it("keeps the plan when the approval is rejected", async () => {
    const { approvals } = await client.approvals("pending");
    let decided = false;
    const view = await follow(
      approvals[0]!.run_id,
      (v) => v.state === "idle",
      async () => {
        if (decided) return;
        decided = true;
        await client.decide(approvals[0]!.id, { decision: "reject", comment: "not today" });
      },
    );
    expect((view.items.at(-1) as AssistantItem).text).toMatch(/nothing was applied/);
    expect((await client.plan()).plan?.id).toBe("p17");
  });

  it("walks through the IO checkout questions", async () => {
    const { runs } = await client.runs();
    const run = runs[0]!;
    await expect(client.sendMessage(run.id, "hello")).rejects.toMatchObject({ status: 409 });
    const answered = new Set<string>();
    const view = await follow(
      run.id,
      (v) => v.state === "idle",
      async (v) => {
        for (const it of v.items) {
          if (it.kind === "question" && it.answer === null && !answered.has(it.questionId)) {
            answered.add(it.questionId);
            await client.answer(run.id, it.questionId, it.text.includes("relay") ? "Skip" : "Yes");
          }
        }
      },
    );
    expect(answered.size).toBe(3);
    const questions = view.items.filter((i) => i.kind === "question");
    expect(questions).toHaveLength(4);
    expect(questions[0]).toMatchObject({ answer: "Yes", answeredBy: "tech1" });
    expect((view.items.at(-1) as AssistantItem).text).toBe(
      "Checkout finished: 3 passed, 1 skipped. All forces are released. The checkout sheet is in the handover report.",
    );
  });

  it("starts new runs, takes follow-up messages and cancels", async () => {
    const created = await client.createRun({ message: "Which rooms are above 24 °C?" });
    expect(created.state).toBe("running");
    const view = await follow(created.id, (v) => v.state === "idle");
    expect((view.items.at(-1) as AssistantItem).text).toMatch(/R205 Temp/);

    await client.sendMessage(created.id, "Why is room 205 warmer than its setpoint?");
    const second = await follow(created.id, (v) => v.state === "idle" && v.items.filter((i) => i.kind === "user").length === 2);
    expect((second.items.at(-1) as AssistantItem).text).toMatch(/priority 8/);

    const slow = await client.createRun({ message: "Run IO checkout for r204-ctl.", playbook: "io-checkout" });
    await client.cancelRun(slow.id);
    const cancelled = await follow(slow.id, (v) => v.state === "cancelled");
    expect(cancelled.items.at(-1)).toMatchObject({ kind: "notice", state: "cancelled" });
  });

  it("closes the open question when the run is cancelled", async () => {
    const { runs } = await client.runs();
    const run = runs[0]!;
    const view = await follow(run.id, (v) => v.state === "waiting_answer" && openQuestions(v).length === 1);
    const question = openQuestions(view)[0]!;
    await client.cancelRun(run.id);
    await expect(client.answer(run.id, question.questionId, "Yes")).rejects.toMatchObject({ status: 409, code: "conflict" });
    await follow(run.id, (v) => v.state === "cancelled");
  });

  it("keeps a cancelled approval rejected: its expiry timer is stopped", async () => {
    const site = new MockSite({ now: () => Date.now() / 1000 });
    const run = new MockRun("r_t", "t", "dev", Date.now() / 1000, "m");
    let n = 0;
    const approvals: Approval[] = [];
    const ctx = new ScriptContext(run, {
      site,
      ids: { next: (prefix) => `${prefix}${++n}` },
      addApproval: (a) => approvals.push(a),
      user: "dev",
      speed: 1000,
      virtualStart: null,
      approvalTtlS: 0.05,
    });
    // The gate is set synchronously right after this event, before the listener's continuation runs.
    const waiting = new Promise<void>((resolve) =>
      run.subscribe((ev) => {
        if (ev.type === "run.state" && ev.state === "waiting_approval") resolve();
      }),
    );
    const spec = { title: "Apply", summary: [], diff: "", rollback: "", planId: null };
    const done = runScript(ctx, async (c) => {
      await c.gatedTool("apply", "C", {}, spec, 0, () => ({ ok: true, summary: "applied" }));
    });
    await waiting;
    expect(run.approvalGate).not.toBeNull();
    run.cancel();
    await done;
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(approvals[0]?.state).toBe("rejected");
    expect(run.approvalGate).toBeNull();
    expect(run.summary.state).toBe("cancelled");
    site.stop();
  });

  it("lets a run-wide approval cover later tier L calls, never tier C", async () => {
    const { approvals: seeded } = await client.approvals("pending");
    await expect(client.decide(seeded[0]!.id, { decision: "approve", scope: "run" })).rejects.toMatchObject({
      status: 400,
      code: "invalid",
    });
    const site = new MockSite({ now: () => Date.now() / 1000 });
    const run = new MockRun("r_t", "t", "dev", Date.now() / 1000, "m");
    run.runApprover = "ops";
    let n = 0;
    const approvals: Approval[] = [];
    const ctx = new ScriptContext(run, {
      site,
      ids: { next: (prefix) => `${prefix}${++n}` },
      addApproval: (a) => approvals.push(a),
      user: "dev",
      speed: 1000,
      virtualStart: null,
    });
    const spec = { title: "Force", summary: [], diff: "", rollback: "", planId: null };
    const out = await ctx.gatedTool("io_force", "L", { node: "r204-ctl" }, spec, 0, () => ({ ok: true, summary: "forced" }));
    expect(out.decision).toBe("approved");
    expect(approvals).toHaveLength(0);
    expect(site.audit[1]).toMatchObject({ action: "approval", user: "ops", detail: "covered by the run-wide approval of ops" });
    expect(site.audit[0]).toMatchObject({ action: "tool", tool: "io_force", outcome: "ok" });
    expect(run.events.map((e) => e.type)).not.toContain("approval.request");
    site.stop();
  });

  it("streams live values, starting with the cached ones", async () => {
    const ids = ["hq/r204-ctl/analog-input:1", "hq/r205-ctl/analog-input:1"];
    const got: Reading[] = [];
    const ctrl = new AbortController();
    const conn = client.live({ ids }, (r) => got.push(r), { signal: ctrl.signal });
    await vi.waitFor(() => expect(got.length).toBeGreaterThan(4), { timeout: 5000 });
    expect(got.slice(0, 2).map((r) => r.id).sort()).toEqual(ids);
    expect(got.every((r) => ids.includes(r.id))).toBe(true);
    ctrl.abort();
    await conn.done;
  });

  it("maps errors to the API error body", async () => {
    await expect(client.device("nope")).rejects.toMatchObject({ status: 404, code: "not_found" });
    await expect(client.identify("r203-ctl")).rejects.toMatchObject({ status: 502, code: "device_error" });
    await expect(client.identify("r204-ctl", 0)).rejects.toMatchObject({ status: 400, code: "validation" });
    await expect(client.identify("r204-ctl", 30)).resolves.toEqual({});
    await expect(client.createRun({ message: " " })).rejects.toMatchObject({ status: 400 });
    await expect(client.get("/api/nothing")).rejects.toMatchObject({ status: 404 });
    const audit = await client.audit(5);
    expect(audit.entries[0]).toMatchObject({ action: "identify", tool: "device_identify" });
  });
});
