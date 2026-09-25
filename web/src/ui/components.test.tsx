import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import type { Approval, DeviceDescription } from "../api/types";
import { DevicesPane } from "../panes/DevicesPane";
import { ApprovalSheet } from "../phone/ApprovalSheet";
import { HubProvider } from "../state/hub";
import { UiProvider } from "../state/ui";

/** Answers requests from a table instead of the network. */
class FakeClient extends ApiClient {
  constructor(private readonly routes: Record<string, unknown>) {
    super();
  }

  override async request<T>(method: string, path: string): Promise<T> {
    const key = `${method} ${path}`;
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
