import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, initAuthToken, isAbort } from "./client";
import type { RunEvent } from "./types";

const realFetch = globalThis.fetch;

function respond(body: string, status = 200, type = "application/json"): Response {
  return new Response(body, { status, headers: { "content-type": type } });
}

beforeEach(() => {
  window.localStorage.clear();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("initAuthToken", () => {
  it("takes ?token= once, stores it and removes it from the address bar", () => {
    window.history.replaceState(null, "", "/?mock=0&token=s3cret#run=r_1");
    expect(initAuthToken()).toBe("s3cret");
    expect(window.location.search).toBe("?mock=0");
    expect(window.location.hash).toBe("#run=r_1");
    expect(initAuthToken()).toBe("s3cret");
  });

  it("returns null in dev mode and clears the token with an empty ?token=", () => {
    expect(initAuthToken()).toBeNull();
    window.localStorage.setItem("uc-hub.token", "old");
    window.history.replaceState(null, "", "/?token=");
    expect(initAuthToken()).toBeNull();
    expect(window.localStorage.getItem("uc-hub.token")).toBeNull();
  });
});

describe("ApiClient", () => {
  it("sends the bearer token and JSON body and parses the response", async () => {
    const fetchMock = vi.fn(async () => respond('{"id":"r_1","state":"running"}', 201));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    const client = new ApiClient({ token: "t0k" });
    const run = await client.createRun({ message: "hi", playbook: "onboard" });
    expect(run).toEqual({ id: "r_1", state: "running" });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/runs");
    expect(init.method).toBe("POST");
    expect(init.body).toBe('{"message":"hi","playbook":"onboard"}');
    expect(init.headers).toMatchObject({ Authorization: "Bearer t0k", "Content-Type": "application/json" });
  });

  it("builds query strings without empty values", async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => respond('{"total":0,"points":[]}'));
    globalThis.fetch = fetchMock as unknown as typeof fetch;
    await new ApiClient({ baseUrl: "http://hub:8080/" }).points({ q: "r204 temp", space: "", limit: 50 });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("http://hub:8080/api/points?q=r204+temp&limit=50");
  });

  it("turns the error body into a typed ApiError", async () => {
    globalThis.fetch = (async () =>
      respond('{"error":{"code":"conflict","message":"already approved","details":{"state":"approved"}}}', 409)) as typeof fetch;
    const err = await new ApiClient().decide("a_1", { decision: "approve" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "conflict", message: "already approved", details: { state: "approved" } });
  });

  it("copes with error responses that are not API errors", async () => {
    globalThis.fetch = (async () => respond("<h1>Bad gateway</h1>", 502, "text/html")) as typeof fetch;
    await expect(new ApiClient().site()).rejects.toMatchObject({ status: 502, code: "http_502" });
    globalThis.fetch = (async () => respond("not json")) as typeof fetch;
    await expect(new ApiClient().site()).rejects.toMatchObject({ code: "bad_response" });
    globalThis.fetch = (async () => new Response(null, { status: 202 })) as typeof fetch;
    await expect(new ApiClient().sendMessage("r", "x")).resolves.toEqual({});
  });

  it("reports network failures, timeouts and aborts distinctly", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as typeof fetch;
    await expect(new ApiClient().health()).rejects.toMatchObject({ status: 0, code: "network" });

    globalThis.fetch = ((_u: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      })) as typeof fetch;
    await expect(new ApiClient({ timeoutMs: 10 }).health()).rejects.toMatchObject({ code: "timeout" });

    const ctrl = new AbortController();
    const pending = new ApiClient().health(ctrl.signal);
    ctrl.abort();
    const err = await pending.catch((e: unknown) => e);
    expect(isAbort(err)).toBe(true);
  });

  it("opens run events with the resume point in the query and the header", async () => {
    const calls: [string, Record<string, string>][] = [];
    globalThis.fetch = (async (url: string, init: RequestInit) => {
      calls.push([url, init.headers as Record<string, string>]);
      const frame = 'id: 6\nevent: message.user\ndata: {"seq":6,"run_id":"r_1","ts":1,"type":"message.user","text":"hi","user":"dev"}\n\n';
      return new Response(frame + "data: {not json}\n\n", { headers: { "content-type": "text/event-stream" } });
    }) as typeof fetch;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const got: RunEvent[] = [];
    const ctrl = new AbortController();
    const conn = new ApiClient({ token: "t" }).runEvents("r_1", 5, (ev) => got.push(ev), {
      signal: ctrl.signal,
      initialDelayMs: 1,
      random: () => 0,
    });
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    ctrl.abort();
    await conn.done;
    expect(calls[0]?.[0]).toBe("/api/runs/r_1/events?after=5");
    expect(calls[0]?.[1]).toMatchObject({ "Last-Event-ID": "5", Authorization: "Bearer t" });
    expect(calls[1]?.[0]).toBe("/api/runs/r_1/events?after=6");
    expect(calls[1]?.[1]["Last-Event-ID"]).toBe("6");
    expect(got[0]).toMatchObject({ seq: 6, type: "message.user" });
    expect(warn).toHaveBeenCalled();
  });
});
