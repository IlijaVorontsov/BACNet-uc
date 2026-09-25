/**
 * Server-sent events over `fetch` streaming.
 *
 * `EventSource` cannot send an `Authorization` header, so the app reads SSE
 * with `fetch` and parses the stream itself. The parser follows the WHATWG
 * "event stream interpretation" rules (CR, LF and CRLF line ends, comments,
 * multi-line `data`, a sticky last event id, `retry`), and `openSse` adds
 * what `EventSource` would do: reconnect with `Last-Event-ID`, backoff, and
 * an idle watchdog for connections that die silently.
 */

export interface SseMessage {
  /** The last event id seen on this stream ("" when the server never sent one). */
  id: string;
  event: string;
  data: string;
}

const LF = 10;
const CR = 13;

export class SseParser {
  private partial = "";
  private afterCR = false;
  private started = false;
  private data = "";
  private hasData = false;
  private eventType = "";
  /** The `id:` of the event being read; it only counts once that event is complete. */
  private idBuffer: string;
  private lastId: string;
  /** Reconnection delay requested by the server with `retry:`, in ms. */
  retry: number | null = null;

  constructor(lastEventId = "") {
    this.idBuffer = lastEventId;
    this.lastId = lastEventId;
  }

  /** Id of the last complete event, i.e. the point to resume after. */
  get lastEventId(): string {
    return this.lastId;
  }

  /** Feeds decoded text and returns the events completed by it. */
  feed(chunk: string): SseMessage[] {
    const out: SseMessage[] = [];
    let text = chunk;
    if (!this.started && text.length > 0) {
      this.started = true;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    }
    let start = 0;
    for (let i = 0; i < text.length; i++) {
      const c = text.charCodeAt(i);
      if (c === LF && this.afterCR) {
        // Second half of a CRLF that was split across chunks (or not).
        this.afterCR = false;
        start = i + 1;
        continue;
      }
      this.afterCR = false;
      if (c === LF || c === CR) {
        const line = this.partial + text.slice(start, i);
        this.partial = "";
        this.afterCR = c === CR;
        this.processLine(line, out);
        start = i + 1;
      }
    }
    if (start < text.length) this.partial += text.slice(start);
    return out;
  }

  private processLine(line: string, out: SseMessage[]): void {
    if (line === "") {
      this.lastId = this.idBuffer;
      if (this.hasData) {
        out.push({ id: this.lastId, event: this.eventType || "message", data: this.data });
      }
      this.data = "";
      this.hasData = false;
      this.eventType = "";
      return;
    }
    if (line.charCodeAt(0) === 58 /* ':' */) return;
    const colon = line.indexOf(":");
    let field: string;
    let value: string;
    if (colon === -1) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.charCodeAt(0) === 32 /* ' ' */) value = value.slice(1);
    }
    switch (field) {
      case "event":
        this.eventType = value;
        break;
      case "data":
        this.data = this.hasData ? `${this.data}\n${value}` : value;
        this.hasData = true;
        break;
      case "id":
        if (!value.includes("\u0000")) this.idBuffer = value;
        break;
      case "retry":
        if (/^[0-9]+$/.test(value)) this.retry = Number(value);
        break;
      default:
        break;
    }
  }
}

export type SseStatus = "connecting" | "open" | "reconnecting" | "closed" | "failed";

export class SseHttpError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, body: string) {
    super(`event stream request failed with HTTP ${status}`);
    this.name = "SseHttpError";
    this.status = status;
    this.body = body;
  }
}

export interface SseOptions {
  /** A URL, or a function building it from the resume point before each (re)connect. */
  url: string | ((lastEventId: string) => string);
  headers?: Record<string, string>;
  /** Resume point for the first request; later requests use what the stream sent. */
  lastEventId?: string;
  signal?: AbortSignal;
  onMessage: (msg: SseMessage) => void;
  onStatus?: (status: SseStatus, info: { attempt: number; error?: unknown; delayMs?: number }) => void;
  initialDelayMs?: number;
  maxDelayMs?: number;
  /** Reconnect when nothing (not even a keep-alive comment) arrived for this long. */
  idleTimeoutMs?: number;
  fetch?: typeof fetch;
  random?: () => number;
}

export interface SseConnection {
  close(): void;
  readonly lastEventId: string;
  /** Resolves when the connection loop has ended (closed or failed). */
  readonly done: Promise<void>;
}

/** 4xx answers other than these are permanent: retrying cannot fix them. */
const RETRYABLE_4XX = new Set([408, 425, 429]);

class IdleTimeout extends Error {
  constructor() {
    super("event stream idle timeout");
    this.name = "IdleTimeout";
  }
}

export function openSse(opts: SseOptions): SseConnection {
  const initialDelay = opts.initialDelayMs ?? 1000;
  const maxDelay = opts.maxDelayMs ?? 30000;
  const idleTimeout = opts.idleTimeoutMs ?? 45000;
  const random = opts.random ?? Math.random;
  const status = opts.onStatus ?? (() => undefined);

  let lastEventId = opts.lastEventId ?? "";
  let closed = false;
  let current: AbortController | null = null;
  let wake: (() => void) | null = null;

  const close = (): void => {
    if (closed) return;
    closed = true;
    current?.abort();
    wake?.();
  };
  if (opts.signal) {
    if (opts.signal.aborted) closed = true;
    else opts.signal.addEventListener("abort", close, { once: true });
  }

  const sleep = (ms: number): Promise<void> =>
    new Promise((resolve) => {
      const timer = setTimeout(() => {
        wake = null;
        resolve();
      }, ms);
      wake = () => {
        clearTimeout(timer);
        wake = null;
        resolve();
      };
    });

  let retryDelay = initialDelay;

  type Outcome = { kind: "retry"; opened: boolean; error?: unknown } | { kind: "fatal" };

  async function connectOnce(attempt: number): Promise<Outcome> {
    const ctrl = new AbortController();
    current = ctrl;
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const armIdle = (): void => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => ctrl.abort(new IdleTimeout()), idleTimeout);
    };
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let opened = false;
    try {
      status("connecting", { attempt });
      const headers: Record<string, string> = {
        Accept: "text/event-stream",
        "Cache-Control": "no-cache",
        ...opts.headers,
      };
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;
      armIdle();
      const doFetch = opts.fetch ?? globalThis.fetch;
      const url = typeof opts.url === "function" ? opts.url(lastEventId) : opts.url;
      const res = await doFetch(url, { headers, signal: ctrl.signal, cache: "no-store" });
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        const err = new SseHttpError(res.status, body);
        if (res.status >= 400 && res.status < 500 && !RETRYABLE_4XX.has(res.status)) {
          status("failed", { attempt, error: err });
          return { kind: "fatal" };
        }
        return { kind: "retry", opened, error: err };
      }
      const type = res.headers.get("content-type") ?? "";
      if (!type.includes("text/event-stream") || !res.body) {
        status("failed", { attempt, error: new Error(`not an event stream: ${type || "no content type"}`) });
        return { kind: "fatal" };
      }
      opened = true;
      status("open", { attempt });
      const parser = new SseParser(lastEventId);
      const decoder = new TextDecoder();
      reader = res.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        armIdle();
        const messages = parser.feed(decoder.decode(value, { stream: true }));
        lastEventId = parser.lastEventId;
        for (const msg of messages) {
          if (closed) break;
          try {
            opts.onMessage(msg);
          } catch (err) {
            console.error("SSE message handler failed", err);
          }
        }
        if (parser.retry !== null) retryDelay = parser.retry;
        if (closed) break;
      }
      return { kind: "retry", opened };
    } catch (err) {
      return { kind: "retry", opened, error: err };
    } finally {
      clearTimeout(idleTimer);
      if (reader) reader.cancel().catch(() => undefined);
      current = null;
    }
  }

  const done = (async () => {
    let attempt = 0;
    while (!closed) {
      const outcome = await connectOnce(attempt);
      if (outcome.kind === "fatal") {
        closed = true;
        return;
      }
      if (closed) break;
      // A connection that worked resets the backoff: only consecutive failures grow it.
      attempt = outcome.opened ? 1 : attempt + 1;
      const base = Math.min(maxDelay, retryDelay * 2 ** Math.min(attempt - 1, 16));
      const delayMs = Math.round(base * (0.5 + random() * 0.5));
      status("reconnecting", { attempt, delayMs, error: outcome.error });
      await sleep(delayMs);
    }
    status("closed", { attempt });
  })();

  return {
    close,
    get lastEventId() {
      return lastEventId;
    },
    done,
  };
}
