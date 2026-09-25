import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import type { Approval, RunEvent } from "../api/types";
import { HubProvider } from "../state/hub";
import { foldEvents, initialRunView } from "../state/runReducer";
import { UiProvider } from "../state/ui";
import { argSummary } from "./argSummary";
import { Composer } from "./Composer";
import { PLAYBOOKS } from "./playbooks";
import { RunStream } from "./RunStream";

/** Answers requests from a table instead of the network and records them. */
class FakeClient extends ApiClient {
  readonly calls: { method: string; path: string; body: unknown }[] = [];

  constructor(private readonly routes: Record<string, unknown>) {
    super();
  }

  override async request<T>(method: string, path: string, opts: { body?: unknown } = {}): Promise<T> {
    this.calls.push({ method, path, body: opts.body });
    const key = `${method} ${path}`;
    if (!(key in this.routes)) throw new ApiError(404, "not_found", key);
    const value = this.routes[key];
    if (value instanceof Error) throw value;
    return structuredClone(value) as T;
  }
}

const APPROVAL: Approval = {
  id: "a_12",
  run_id: "r_1",
  call_id: "c3",
  tool: "apply",
  tier: "C",
  title: "Apply plan p17",
  summary: ["r204-ctl: io.json +1 point"],
  diff: "--- a/io.json\n+++ b/io.json\n@@ -1 +1,2 @@\n [\n+  {\"channel\": \"di0\"}",
  rollback: "Apply revision 16",
  plan_id: "p17",
  state: "pending",
  requested_at: Date.now() / 1000,
  expires_at: Date.now() / 1000 + 1800,
  requested_by: "dev",
  decided_by: null,
  decided_at: null,
  comment: null,
};

function base(roles: string[] = ["admin"]): Record<string, unknown> {
  return {
    "GET /api/health": { ok: true, version: "t", site: "hq", llm: { provider: "x", model: "glm-5.3", configured: true }, dev_mode: true },
    "GET /api/me": { user: "dev", roles },
    "GET /api/site": { name: "hq", description: "HQ", spaces: [], devices: [], summary: { devices: 0, online: 0, points: 0, unassigned: 0, pending_changes: 0 } },
    "GET /api/plan": { plan: null },
    "GET /api/manifest": { live_revision: 16, draft_revision: null, yaml: "", draft_yaml: null },
    "GET /api/tests": { results: [], updated_at: null },
    "GET /api/runs": { runs: [] },
    "GET /api/approvals": { approvals: [] },
  };
}

function setup(node: ReactNode, routes: Record<string, unknown>): FakeClient {
  const client = new FakeClient(routes);
  render(
    <HubProvider client={client} mock={false}>
      <UiProvider>{node}</UiProvider>
    </HubProvider>,
  );
  return client;
}

type Body = RunEvent extends infer E ? (E extends RunEvent ? Omit<E, "seq" | "run_id" | "ts"> : never) : never;
function view(...bodies: Body[]) {
  return foldEvents(
    initialRunView("r_1"),
    bodies.map((b, i) => ({ ...b, seq: i + 1, run_id: "r_1", ts: 1000 + i }) as RunEvent),
  );
}

const STREAM = view(
  { type: "message.user", text: "Commission room 204", user: "dev" },
  { type: "thinking.delta", text: "r204-ctl is new" },
  { type: "tool.call", call_id: "c1", tool: "site_search", tier: "R", args: { query: "space:r204" } },
  { type: "tool.result", call_id: "c1", ok: true, summary: "1 new node", duration_ms: 200 },
  { type: "tool.call", call_id: "c2", tool: "ask_user", tier: "R", args: { question: "Open?" } },
  { type: "question", question_id: "q1", call_id: "c2", text: "Is the valve open?", options: ["Yes", "No"] },
  { type: "run.state", state: "waiting_answer" },
);

describe("RunStream", () => {
  it("renders messages, collapsed thinking and tool cards, and hides ask_user cards", async () => {
    setup(<RunStream view={STREAM} runId="r_1" variant="desktop" />, base());
    expect(screen.getByText("Commission room 204")).toBeTruthy();
    const think = screen.getByRole("button", { name: /Reasoned for/ });
    expect(think.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(think);
    expect(screen.getByText("r204-ctl is new")).toBeTruthy();
    const tool = screen.getByRole("button", { name: /site_search/ });
    expect(screen.queryByRole("button", { name: /ask_user/ })).toBeNull();
    fireEvent.click(tool);
    expect(screen.getByText(/"query": "space:r204"/)).toBeTruthy();
    await waitFor(() => expect(screen.getByText("The agent asks: Is the valve open?")).toBeTruthy());
  });

  it("posts the answer to a question", async () => {
    const client = setup(<RunStream view={STREAM} runId="r_1" variant="phone" />, {
      ...base(),
      "POST /api/runs/r_1/answer": {},
    });
    const card = screen.getByRole("region", { name: "Question from the agent" });
    fireEvent.click(within(card).getByRole("button", { name: "Yes" }));
    await waitFor(() => expect(within(card).getByText("Answered")).toBeTruthy());
    expect(client.calls.find((c) => c.method === "POST")).toEqual({
      method: "POST",
      path: "/api/runs/r_1/answer",
      body: { question_id: "q1", answer: "Yes" },
    });
  });

  it("needs the expanded card before a tier C approval, then posts it", async () => {
    const v = view(
      { type: "tool.call", call_id: "c3", tool: "apply", tier: "C", args: { plan_id: "p17" } },
      { type: "approval.request", approval: APPROVAL },
      { type: "run.state", state: "waiting_approval" },
    );
    const client = setup(<RunStream view={v} runId="r_1" variant="desktop" />, {
      ...base(),
      "POST /api/approvals/a_12": { ...APPROVAL, state: "approved", decided_by: "dev", decided_at: Date.now() / 1000 },
    });
    const card = screen.getByRole("region", { name: "Approval: Apply plan p17" });
    expect(within(card).queryByRole("button", { name: "Approve and apply" })).toBeNull();
    fireEvent.click(within(card).getByRole("button", { name: "Review diff" }));
    expect(card.querySelector(".ln.add")?.textContent).toContain('"channel": "di0"');
    await waitFor(() => expect(within(card).getByRole<HTMLButtonElement>("button", { name: "Approve and apply" }).disabled).toBe(false));
    fireEvent.change(within(card).getByRole("textbox"), { target: { value: "go ahead" } });
    fireEvent.click(within(card).getByRole("button", { name: "Approve and apply" }));
    await waitFor(() => expect(within(card).getByText(/Approved by dev/)).toBeTruthy());
    expect(client.calls.find((c) => c.method === "POST")?.body).toEqual({ decision: "approve", comment: "go ahead" });
  });

  it("does not let a viewer approve", async () => {
    const v = view({ type: "approval.request", approval: APPROVAL });
    setup(<RunStream view={v} runId="r_1" variant="desktop" />, base(["viewer"]));
    const card = screen.getByRole("region", { name: "Approval: Apply plan p17" });
    await waitFor(() => expect(within(card).getByText(/Your role cannot approve tier C/)).toBeTruthy());
    expect(within(card).getByRole<HTMLButtonElement>("button", { name: "Reject" }).disabled).toBe(true);
  });

  it("shows why a decision failed", async () => {
    const v = view({ type: "approval.request", approval: { ...APPROVAL, tier: "L", tool: "point_write", title: "Write setpoint" } });
    setup(<RunStream view={v} runId="r_1" variant="desktop" />, {
      ...base(),
      "POST /api/approvals/a_12": new ApiError(409, "conflict", "approval a_12 is already expired"),
    });
    const card = screen.getByRole("region", { name: "Approval: Write setpoint" });
    await waitFor(() => expect(within(card).getByRole<HTMLButtonElement>("button", { name: "Approve" }).disabled).toBe(false));
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(within(card).getByRole("alert").textContent).toContain("already expired"));
  });
});

describe("Composer", () => {
  it("starts a new run with the chosen playbook", async () => {
    const client = setup(<Composer variant="desktop" state={null} />, {
      ...base(),
      "POST /api/runs": { id: "r_9", title: "IO checkout", state: "running", created_at: 1, updated_at: 1, created_by: "dev", last_seq: 0, model: "glm-5.3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "IO checkout" }));
    const box = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message to the agent" });
    await waitFor(() => expect(box.value).toBe("Run IO checkout for "));
    fireEvent.change(box, { target: { value: `${box.value}r204-ctl.` } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(client.calls.some((c) => c.method === "POST" && c.path === "/api/runs")).toBe(true));
    expect(client.calls.find((c) => c.path === "/api/runs" && c.method === "POST")?.body).toEqual({
      message: "Run IO checkout for r204-ctl.",
      playbook: "io-checkout",
    });
    await waitFor(() => expect(box.value).toBe(""));
  });

  it("drafts playbook prompts that name only what is in scope, never the demo site", () => {
    const prompt = (id: string, scope: Parameters<(typeof PLAYBOOKS)[number]["prompt"]>[0]) =>
      PLAYBOOKS.find((p) => p.id === id)!.prompt(scope);
    const site = { scope: { kind: "site" } as const, scopeName: "Plant B" };
    for (const p of PLAYBOOKS) expect(p.prompt(site)).not.toMatch(/r20\d|room 205/i);
    expect(prompt("io-checkout", { scope: { kind: "device", name: "ahu1-ctl" }, scopeName: "ahu1-ctl" })).toBe(
      "Run IO checkout for ahu1-ctl.",
    );
    expect(prompt("troubleshoot", site)).toBe("Troubleshoot Plant B: ");
    expect(prompt("troubleshoot", { scope: { kind: "space", id: "r3" }, scopeName: "Room 3" })).toBe("Troubleshoot Room 3: ");
  });

  it("explains why it cannot send while the open run waits for an answer", async () => {
    const run = { id: "r_1", title: "IO checkout", state: "waiting_answer", created_at: 1, updated_at: 1, created_by: "dev", last_seq: 9, model: "glm-5.3" };
    setup(<Composer variant="desktop" state="waiting_answer" />, { ...base(), "GET /api/runs": { runs: [run] } });
    await waitFor(() => expect(screen.getByText("Answer the question first.")).toBeTruthy());
    fireEvent.change(screen.getByRole("textbox", { name: "Message to the agent" }), { target: { value: "hello" } });
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Send" }).disabled).toBe(true);
  });
});

describe("argSummary", () => {
  it("summarises common argument shapes", () => {
    expect(argSummary({ node: "r204-ctl", channel: "ao0", value: 100, lease_s: 60 })).toBe("r204-ctl · ao0 · value 100 · lease 60 s");
    expect(argSummary({ json_patch: [1, 2, 3] })).toBe("3 JSON-patch ops");
    expect(argSummary({ points: ["hq/a/b"] })).toBe("hq/a/b");
    expect(argSummary({ tests: [], target: "live" })).toBe("all tests · live");
    expect(argSummary({ app: "thermostat", source_c: "a\nb\nc" })).toBe("thermostat · 3 lines");
    expect(argSummary({}, '{"patch": [{"op": "add", "path": "/system/nodes/4/io/-", "value"')).toMatch(/^…/);
    expect(argSummary({}, "{}")).toBe("");
  });
});
