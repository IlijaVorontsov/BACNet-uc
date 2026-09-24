import { afterEach, describe, expect, it, vi } from "vitest";
import { openSse, SseHttpError, SseParser, type SseMessage, type SseStatus } from "./sse";

function feedAll(parser: SseParser, chunks: string[]): SseMessage[] {
  return chunks.flatMap((c) => parser.feed(c));
}

describe("SseParser", () => {
  it("parses id, event and data", () => {
    const p = new SseParser();
    expect(p.feed('id: 7\nevent: run.state\ndata: {"seq":7}\n\n')).toEqual([
      { id: "7", event: "run.state", data: '{"seq":7}' },
    ]);
    expect(p.lastEventId).toBe("7");
  });

  it("joins multi-line data with LF", () => {
    const p = new SseParser();
    expect(p.feed("data: first\ndata:second\ndata\n\n")).toEqual([{ id: "", event: "message", data: "first\nsecond\n" }]);
  });

  it("accepts CRLF, CR and LF line ends, also mixed and split across chunks", () => {
    const p = new SseParser();
    const out = feedAll(p, ["data: a\r", "\n\r", "\ndata: b\r\rdata: c\n", "\n"]);
    expect(out.map((m) => m.data)).toEqual(["a", "b", "c"]);
  });

  it("does not treat a lone CR followed by a line as CRLF", () => {
    const p = new SseParser();
    expect(p.feed("data: x\rdata: y\r\r").map((m) => m.data)).toEqual(["x\ny"]);
  });

  it("ignores comments, unknown fields and keep-alives", () => {
    const p = new SseParser();
    expect(p.feed(": keep-alive\n\nfoo: bar\n: hi\ndata: v\n\n")).toEqual([{ id: "", event: "message", data: "v" }]);
  });

  it("does not dispatch events without data and resets the event type", () => {
    const p = new SseParser();
    expect(p.feed("event: nothing\n\ndata: d\n\n")).toEqual([{ id: "", event: "message", data: "d" }]);
  });

  it("dispatches an empty data field", () => {
    expect(new SseParser().feed("data\n\n")).toEqual([{ id: "", event: "message", data: "" }]);
  });

  it("removes only one leading space from values", () => {
    expect(new SseParser().feed("data:  two\n\n")[0]?.data).toBe(" two");
  });

  it("reassembles lines split at arbitrary positions", () => {
    const text = 'id: 1\nevent: reading\ndata: {"id":"hq/a/b","value":1.5}\n\nid: 2\ndata: x\n\n';
    for (let cut = 0; cut <= text.length; cut++) {
      const out = feedAll(new SseParser(), [text.slice(0, cut), text.slice(cut)]);
      expect(out).toEqual([
        { id: "1", event: "reading", data: '{"id":"hq/a/b","value":1.5}' },
        { id: "2", event: "message", data: "x" },
      ]);
    }
  });

  it("keeps the last event id across events and ignores ids with NUL", () => {
    const p = new SseParser("3");
    const out = p.feed("data: a\n\nid: 4\ndata: b\n\nid: bad\u0000id\ndata: c\n\n");
    expect(out.map((m) => m.id)).toEqual(["3", "4", "4"]);
  });

  it("an empty id resets the last event id", () => {
    const p = new SseParser("9");
    p.feed("id\ndata: a\n\n");
    expect(p.lastEventId).toBe("");
  });

  it("reads retry only when it is all digits", () => {
    const p = new SseParser();
    p.feed("retry: 2500\n\n");
    expect(p.retry).toBe(2500);
    p.feed("retry: 1s\n\n");
    expect(p.retry).toBe(2500);
  });

  it("strips a leading byte order mark only at the start of the stream", () => {
    const p = new SseParser();
    expect(p.feed("﻿data: a\n\n")[0]?.data).toBe("a");
    expect(p.feed("﻿data: b\n\n")).toEqual([]);
  });

  it("drops an unterminated event at the end of the input", () => {
    expect(new SseParser().feed("data: incomplete\n")).toEqual([]);
  });
});

// ------------------------------------------------------------ openSse

const enc = new TextEncoder();

function streamResponse(chunks: string[], opts: { keepOpen?: boolean; signal?: AbortSignal | null } = {}): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      if (!opts.keepOpen) controller.close();
      opts.signal?.addEventListener("abort", () => {
        try {
          controller.error(new DOMException("aborted", "AbortError"));
        } catch {
          // already closed
        }
      });
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

interface Call {
  url: string;
  headers: Record<string, string>;
}

function fakeFetch(responder: (call: Call, n: number, signal: AbortSignal | null) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const call = { url: String(input), headers: (init?.headers ?? {}) as Record<string, string> };
    calls.push(call);
    return responder(call, calls.length, init?.signal ?? null);
  });
  return { fn: fn as unknown as typeof fetch, calls };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("openSse", () => {
  it("delivers messages and reconnects with Last-Event-ID after the stream ends", async () => {
    const { fn, calls } = fakeFetch((_call, n, signal) =>
      n === 1
        ? streamResponse(["id: 1\ndata: a\n\nid: 2\nda", "ta: b\n\n"])
        : streamResponse(["id: 3\ndata: c\n\n"], { keepOpen: true, signal }),
    );
    const got: string[] = [];
    const conn = openSse({
      url: (last) => `/events?after=${last || 0}`,
      headers: { Authorization: "Bearer t" },
      fetch: fn,
      initialDelayMs: 1,
      random: () => 0,
      onMessage: (m) => got.push(`${m.id}:${m.data}`),
    });
    await vi.waitFor(() => expect(got).toEqual(["1:a", "2:b", "3:c"]));
    expect(calls[0]?.url).toBe("/events?after=0");
    expect(calls[0]?.headers["Last-Event-ID"]).toBeUndefined();
    expect(calls[0]?.headers.Authorization).toBe("Bearer t");
    expect(calls[1]?.url).toBe("/events?after=2");
    expect(calls[1]?.headers["Last-Event-ID"]).toBe("2");
    expect(conn.lastEventId).toBe("3");
    conn.close();
    await conn.done;
  });

  it("backs off after failures and stops for good on 4xx", async () => {
    const statuses: SseStatus[] = [];
    const delays: number[] = [];
    const { fn } = fakeFetch((_c, n) => {
      if (n <= 2) return Promise.reject(new TypeError("network down"));
      return new Response('{"error":{"code":"denied","message":"no"}}', { status: 403 });
    });
    let failure: unknown;
    const conn = openSse({
      url: "/live",
      fetch: fn,
      initialDelayMs: 4,
      random: () => 1,
      onMessage: () => undefined,
      onStatus: (s, info) => {
        statuses.push(s);
        if (info.delayMs !== undefined) delays.push(info.delayMs);
        if (s === "failed") failure = info.error;
      },
    });
    await conn.done;
    expect(delays).toEqual([4, 8]);
    expect(statuses.at(-1)).toBe("failed");
    expect(failure).toBeInstanceOf(SseHttpError);
    expect((failure as SseHttpError).status).toBe(403);
  });

  it("honours the server retry delay", async () => {
    const delays: number[] = [];
    let n = 0;
    const { fn } = fakeFetch((_c, _n, signal) => {
      n += 1;
      return n === 1 ? streamResponse(["retry: 3\n\n"]) : streamResponse([], { keepOpen: true, signal });
    });
    const conn = openSse({
      url: "/x",
      fetch: fn,
      initialDelayMs: 1000,
      random: () => 1,
      onMessage: () => undefined,
      onStatus: (_s, info) => {
        if (info.delayMs !== undefined) delays.push(info.delayMs);
      },
    });
    await vi.waitFor(() => expect(n).toBe(2));
    expect(delays).toEqual([3]);
    conn.close();
    await conn.done;
  });

  it("fails when the response is not an event stream", async () => {
    const statuses: SseStatus[] = [];
    const { fn } = fakeFetch(() => new Response("<html>", { status: 200, headers: { "content-type": "text/html" } }));
    const conn = openSse({ url: "/x", fetch: fn, onMessage: () => undefined, onStatus: (s) => statuses.push(s) });
    await conn.done;
    expect(statuses).toEqual(["connecting", "failed"]);
  });

  it("aborts the request on close and does not reconnect", async () => {
    let seenSignal: AbortSignal | null = null;
    const { fn, calls } = fakeFetch((_c, _n, signal) => {
      seenSignal = signal;
      return streamResponse(["data: a\n\n"], { keepOpen: true, signal });
    });
    const ctrl = new AbortController();
    const got: string[] = [];
    const conn = openSse({ url: "/x", fetch: fn, signal: ctrl.signal, onMessage: (m) => got.push(m.data) });
    await vi.waitFor(() => expect(got).toEqual(["a"]));
    ctrl.abort();
    await conn.done;
    expect(seenSignal!.aborted).toBe(true);
    expect(calls).toHaveLength(1);
  });

  it("reconnects when the stream goes silent", async () => {
    const { fn, calls } = fakeFetch((_c, _n, signal) => streamResponse([": hi\n\n"], { keepOpen: true, signal }));
    const conn = openSse({
      url: "/x",
      fetch: fn,
      idleTimeoutMs: 20,
      initialDelayMs: 1,
      random: () => 0,
      onMessage: () => undefined,
    });
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    conn.close();
    await conn.done;
  });

  it("keeps the stream alive when a handler throws", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { fn } = fakeFetch((_c, _n, signal) => streamResponse(["data: 1\n\ndata: 2\n\n"], { keepOpen: true, signal }));
    const got: string[] = [];
    const conn = openSse({
      url: "/x",
      fetch: fn,
      onMessage: (m) => {
        if (m.data === "1") throw new Error("boom");
        got.push(m.data);
      },
    });
    await vi.waitFor(() => expect(got).toEqual(["2"]));
    expect(errors).toHaveBeenCalled();
    conn.close();
    await conn.done;
  });
});
