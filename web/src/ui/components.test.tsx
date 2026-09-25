import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import type { Approval, DeviceDescription, Plan } from "../api/types";
import { ChangesPane } from "../panes/ChangesPane";
import { DevicesPane } from "../panes/DevicesPane";
import { ApprovalSheet } from "../phone/ApprovalSheet";
import { HubProvider } from "../state/hub";
import { UiProvider } from "../state/ui";

/** Answers requests from a table instead of the network, and keeps the bodies sent. */
class FakeClient extends ApiClient {
  readonly sent: { key: string; body: unknown }[] = [];

  constructor(private readonly routes: Record<string, unknown>) {
    super();
  }

  override async request<T>(method: string, path: string, opts: { body?: unknown } = {}): Promise<T> {
    const key = `${method} ${path}`;
    if (method !== "GET") this.sent.push({ key, body: opts.body });
    if (!(key in this.routes)) throw new ApiError(404, "not_found", key);
    const value = this.routes[key];
    if (value instanceof Error) throw value;
    return structuredClone(value) as T;
  }
}

const BASE: Record<string, unknown> = {
  "GET /api/health": { ok: true, version: "t", site: "hq", llm: { provider: "x", model: "m", configured: true }, dev_mode: true },
  "GET /api/me": { user: "dev", roles: ["admin"] },
  "GET /api/site": { name: "hq", description: "HQ", spaces: [], devices: [], summary: { devices: 0, online: 0, points: 0, unassigned: 0, pending_changes: 0 } },
  "GET /api/plan": { plan: null },
  "GET /api/manifest": { live_revision: 16, draft_revision: null, yaml: "", draft_yaml: null },
  "GET /api/tests": { results: [], updated_at: null },
  "GET /api/runs": { runs: [] },
  "GET /api/approvals": { approvals: [] },
};

function wrap(client: ApiClient, node: ReactNode): ReactNode {
  return (
    <HubProvider client={client} mock={false}>
      <UiProvider>{node}</UiProvider>
    </HubProvider>
  );
}

afterEach(() => {
  vi.useRealTimers();
});

const APPROVAL: Approval = {
  id: "a_12",
  run_id: "r_1",
  call_id: "c3",
  tool: "apply",
  tier: "C",
  title: "Apply plan p17",
  summary: ["r204-ctl: io.json +1 point"],
  diff: "+x",
  rollback: "Apply revision 16",
  plan_id: "p17",
  state: "pending",
  scope: "call",
  requested_at: Date.now() / 1000,
  expires_at: Date.now() / 1000 + 1800,
  requested_by: "dev",
  decided_by: null,
  decided_at: null,
  comment: null,
};

describe("ApprovalSheet", () => {
  it("lets the user hold again after the approval request failed", async () => {
    vi.useFakeTimers();
    const client = new FakeClient({ ...BASE, "POST /api/approvals/a_12": new ApiError(0, "network", "cannot reach the hub") });
    const onDecided = vi.fn();
    render(wrap(client, <ApprovalSheet approval={APPROVAL} onDecided={onDecided} />));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    const hold = screen.getByRole<HTMLButtonElement>("button", { name: "Hold to apply" });
    expect(hold.disabled).toBe(false);
    fireEvent.pointerDown(hold, { button: 0, pointerId: 1 });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    fireEvent.pointerUp(hold);
    expect(screen.getByRole("alert").textContent).toMatch(/cannot reach the hub/);
    expect(onDecided).not.toHaveBeenCalled();
    const again = screen.getByRole<HTMLButtonElement>("button", { name: "Hold to apply" });
    expect(again.disabled).toBe(false);
  });

  it("approves a tier L call for the rest of the run", async () => {
    const live: Approval = { ...APPROVAL, id: "a_13", tool: "io_force", tier: "L", title: "Force r204-ctl ao0", plan_id: null };
    const decided: Approval = { ...live, state: "approved", scope: "run", decided_by: "dev", decided_at: Date.now() / 1000 };
    const client = new FakeClient({ ...BASE, "POST /api/approvals/a_13": decided });
    const onDecided = vi.fn();
    render(wrap(client, <ApprovalSheet approval={live} onDecided={onDecided} />));
    const button = await screen.findByRole<HTMLButtonElement>("button", { name: "Approve for this run" });
    await waitFor(() => expect(button.disabled).toBe(false));
    fireEvent.click(button);
    await waitFor(() => expect(onDecided).toHaveBeenCalledWith(decided));
    expect(client.sent).toEqual([{ key: "POST /api/approvals/a_13", body: { decision: "approve", scope: "run" } }]);
  });

  it("offers no run-wide approval for tier C", async () => {
    render(wrap(new FakeClient(BASE), <ApprovalSheet approval={APPROVAL} onDecided={vi.fn()} />));
    await screen.findByRole("button", { name: "Hold to apply" });
    expect(screen.queryByRole("button", { name: "Approve for this run" })).toBeNull();
  });
});

function description(name: string, address: string): DeviceDescription {
  return {
    device: {
      name,
      protocol: "bacnet-uc",
      address,
      online: true,
      managed: true,
      model: "nucleo_f767zi",
      firmware: "0.1.0",
      hwid: "",
      instance: 2041,
      space: null,
      last_seen: null,
      points: 0,
    },
    points: [],
    apps: [],
    extra: {},
  };
}

describe("DevicesPane", () => {
  it("does not carry one device's details or identify state over to another", async () => {
    const client = new FakeClient({
      ...BASE,
      "GET /api/devices/r204-ctl": description("r204-ctl", "10.0.2.51:1337"),
      "GET /api/devices/r205-ctl": description("r205-ctl", "10.0.2.52:1337"),
      "POST /api/devices/r204-ctl/identify": {},
    });
    const { rerender } = render(wrap(client, <DevicesPane scope={{ kind: "device", name: "r204-ctl" }} />));
    fireEvent.click(await screen.findByRole("button", { name: "Identify" }));
    await screen.findByRole("button", { name: "Blinking for 30 s" });

    rerender(wrap(client, <DevicesPane scope={{ kind: "device", name: "r205-ctl" }} />));
    expect(screen.queryByText("10.0.2.51:1337")).toBeNull();
    await waitFor(() => expect(screen.getByText("10.0.2.52:1337")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Identify" })).toBeTruthy();
    expect(screen.queryByText("Blinking for 30 s")).toBeNull();
  });
});

describe("ChangesPane", () => {
  it("says which targets keep a plan from being applied", async () => {
    const plan: Plan = {
      id: "p18",
      revision: 18,
      base_revision: 17,
      created_at: 1790290000,
      targets: ["gateway"],
      warnings: ["r205-ctl: unreachable: DeviceTimeout: no answer"],
      changes: [{ id: "c1", target: "gateway", kind: "tags", summary: "tags: 1 point", diff: "", tier: "C" }],
      blocked: { "r205-ctl": "DeviceTimeout: no answer" },
    };
    render(wrap(new FakeClient({ ...BASE, "GET /api/plan": { plan } }), <ChangesPane />));
    const blocked = await screen.findByRole("list", { name: "Blocked targets" });
    expect(blocked.textContent).toBe("Cannot be applied: r205-ctl could not be planned (DeviceTimeout: no answer)");
  });
});
