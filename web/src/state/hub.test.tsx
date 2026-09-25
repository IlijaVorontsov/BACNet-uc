import { act, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { HealthChips } from "../ui/HealthChips";
import { HubProvider, useResource } from "./hub";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Advances fake time in steps, letting React commit and run effects between them as a browser would. */
async function advance(ms: number, step = 100): Promise<void> {
  for (let t = 0; t < ms; t += step) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(Math.min(step, ms - t));
    });
  }
}

/** A fetch that never answers, like a gateway behind a dead link; it only ends when aborted. */
function hang(_input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

const FIXTURES: Record<string, unknown> = {
  "/api/health": { ok: true, version: "t", site: "hq", llm: { provider: "x", model: "m", configured: true }, dev_mode: true },
  "/api/me": { user: "dev", roles: ["admin"] },
  "/api/site": { name: "hq", description: "HQ", spaces: [], devices: [], summary: { devices: 0, online: 0, points: 0, unassigned: 0, pending_changes: 0 } },
  "/api/plan": { plan: null },
  "/api/manifest": { live_revision: 1, draft_revision: null, yaml: "", draft_yaml: null },
  "/api/tests": { results: [], updated_at: null },
  "/api/runs": { runs: [] },
  "/api/approvals": { approvals: [] },
};

describe("useResource", () => {
  it("lets a hung request time out instead of restarting it on every poll", async () => {
    const fetchMock = vi.fn(hang);
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient({ timeoutMs: 2500 });
    const { result } = renderHook(() => useResource((s) => client.health(s), [client], 1000));
    await advance(2600);
    expect(result.current.error?.code).toBe("timeout");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("still restarts the request on an explicit reload", async () => {
    const fetchMock = vi.fn(hang);
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient({ timeoutMs: 60000 });
    const { result } = renderHook(() => useResource((s) => client.health(s), [client], 1000));
    act(() => result.current.reload());
    await advance(10);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).signal?.aborted).toBe(true);
  });
});

describe("HealthChips", () => {
  it("keeps showing the gateway as unreachable while a retry is in flight", async () => {
    let healthCalls = 0;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://hub.local").pathname;
      if (path === "/api/health" && ++healthCalls > 1) return hang(input, init);
      return Promise.resolve(json(FIXTURES[path] ?? {}));
    });
    render(
      <HubProvider client={new ApiClient({ timeoutMs: 2500 })} mock={false}>
        <HealthChips />
      </HubProvider>,
    );
    await advance(10);
    expect(screen.getByText("Gateway online")).toBeTruthy();
    // Poll at 15 s hangs and times out at 17.5 s.
    await advance(18000, 500);
    expect(screen.getByText("Gateway unreachable")).toBeTruthy();
    // The next poll at 30 s is in flight; the gateway is still not known to be back.
    await advance(13000, 500);
    expect(screen.queryByText("Gateway online")).toBeNull();
    expect(screen.getByText("Gateway unreachable")).toBeTruthy();
  });
});
