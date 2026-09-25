/**
 * Typed fetch wrapper for the uc-hub API (docs/ai-harness/API.md).
 *
 * `fetch` is looked up on every call instead of being captured once, so the
 * in-browser mock backend (src/api/mock) can intercept it and the same code
 * path runs against the mock and the real hub.
 */

import { openSse, type SseConnection, type SseOptions } from "./sse";
import type {
  Approval,
  ApiErrorBody,
  AuditEntry,
  CreateRunRequest,
  DecisionRequest,
  DeviceDescription,
  DiscoverRequest,
  Discovery,
  Health,
  ManifestInfo,
  ManifestRevision,
  Me,
  Plan,
  PointsPage,
  PointsQuery,
  Reading,
  RunEvent,
  RunSummary,
  Site,
  TestsInfo,
} from "./types";

const TOKEN_KEY = "uc-hub.token";
const DEFAULT_TIMEOUT_MS = 20000;

/** An API failure. `status` is 0 when no HTTP response arrived. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function isAbort(err: unknown): boolean {
  return (
    (err instanceof ApiError && err.code === "aborted") ||
    (err instanceof DOMException && err.name === "AbortError")
  );
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * Takes `?token=` from the address bar (a link handed out by an admin),
 * stores it, and removes it from the URL so it does not stay in the history
 * or end up in screenshots. Returns the token to use, or null for dev mode.
 */
export function initAuthToken(loc: Location = window.location, hist: History = window.history): string | null {
  const store = storage();
  const url = new URL(loc.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl !== null) {
    url.searchParams.delete("token");
    try {
      hist.replaceState(hist.state, "", url.pathname + url.search + url.hash);
    } catch {
      // Some embedded browsers refuse replaceState; keeping the URL is harmless.
    }
    if (fromUrl === "") {
      store?.removeItem(TOKEN_KEY);
      return null;
    }
    store?.setItem(TOKEN_KEY, fromUrl);
    return fromUrl;
  }
  return store?.getItem(TOKEN_KEY) ?? null;
}

type Query = Record<string, string | number | boolean | null | undefined>;

interface RequestOptions {
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export interface ClientOptions {
  /** Origin (and optional prefix) of the hub; "" means same origin. */
  baseUrl?: string;
  token?: string | null;
  timeoutMs?: number;
}

export class ApiClient {
  readonly baseUrl: string;
  readonly token: string | null;
  private readonly timeoutMs: number;

  constructor(opts: ClientOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? "").replace(/\/+$/, "");
    this.token = opts.token ?? null;
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  url(path: string, query?: Query): string {
    let out = this.baseUrl + path;
    if (query) {
      const params = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
      }
      const qs = params.toString();
      if (qs) out += `?${qs}`;
    }
    return out;
  }

  authHeaders(): Record<string, string> {
    return this.token ? { Authorization: `Bearer ${this.token}` } : {};
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const ctrl = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      ctrl.abort();
    }, opts.timeoutMs ?? this.timeoutMs);
    const onAbort = (): void => ctrl.abort();
    if (opts.signal) {
      if (opts.signal.aborted) ctrl.abort();
      else opts.signal.addEventListener("abort", onAbort, { once: true });
    }
    const headers: Record<string, string> = { Accept: "application/json", ...this.authHeaders() };
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    try {
      let res: Response;
      try {
        res = await globalThis.fetch(this.url(path, opts.query), { method, headers, body, signal: ctrl.signal });
      } catch (err) {
        if (timedOut) throw new ApiError(0, "timeout", `${method} ${path} timed out`);
        if (ctrl.signal.aborted) throw new ApiError(0, "aborted", "request aborted");
        throw new ApiError(0, "network", `cannot reach the hub (${errorMessage(err)})`);
      }
      const text = await res.text().catch((err: unknown) => {
        if (timedOut) throw new ApiError(0, "timeout", `${method} ${path} timed out`);
        throw new ApiError(0, ctrl.signal.aborted ? "aborted" : "network", errorMessage(err));
      });
      if (!res.ok) throw errorFromResponse(res, text);
      if (!text) return {} as T;
      try {
        return JSON.parse(text) as T;
      } catch {
        throw new ApiError(res.status, "bad_response", `${method} ${path} returned invalid JSON`);
      }
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
    }
  }

  get<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
    return this.request<T>("GET", path, { query, signal });
  }

  post<T>(path: string, body: unknown = {}, signal?: AbortSignal): Promise<T> {
    return this.request<T>("POST", path, { body, signal });
  }

  // -------------------------------------------------------- site and points

  health(signal?: AbortSignal): Promise<Health> {
    return this.get("/api/health", undefined, signal);
  }

  me(signal?: AbortSignal): Promise<Me> {
    return this.get("/api/me", undefined, signal);
  }

  site(signal?: AbortSignal): Promise<Site> {
    return this.get("/api/site", undefined, signal);
  }

  device(name: string, signal?: AbortSignal): Promise<DeviceDescription> {
    return this.get(`/api/devices/${encodeURIComponent(name)}`, undefined, signal);
  }

  points(query: PointsQuery = {}, signal?: AbortSignal): Promise<PointsPage> {
    return this.get("/api/points", { ...query }, signal);
  }

  readPoints(ids: string[], signal?: AbortSignal): Promise<{ readings: Reading[] }> {
    return this.post("/api/points/read", { ids }, signal);
  }

  discover(req: DiscoverRequest = {}, signal?: AbortSignal): Promise<Discovery> {
    return this.request("POST", "/api/discover", {
      body: req,
      signal,
      timeoutMs: ((req.timeout_s ?? 5) + 10) * 1000,
    });
  }

  identify(name: string, seconds?: number): Promise<Record<string, never>> {
    return this.post(`/api/devices/${encodeURIComponent(name)}/identify`, seconds === undefined ? {} : { seconds });
  }

  // ------------------------------------------------ manifest, plan, tests

  manifest(signal?: AbortSignal): Promise<ManifestInfo> {
    return this.get("/api/manifest", undefined, signal);
  }

  revisions(signal?: AbortSignal): Promise<{ revisions: ManifestRevision[] }> {
    return this.get("/api/manifest/revisions", undefined, signal);
  }

  plan(signal?: AbortSignal): Promise<{ plan: Plan | null }> {
    return this.get("/api/plan", undefined, signal);
  }

  tests(signal?: AbortSignal): Promise<TestsInfo> {
    return this.get("/api/tests", undefined, signal);
  }

  // ------------------------------------------------------------------ runs

  runs(limit = 20, signal?: AbortSignal): Promise<{ runs: RunSummary[] }> {
    return this.get("/api/runs", { limit }, signal);
  }

  createRun(req: CreateRunRequest): Promise<RunSummary> {
    return this.post("/api/runs", req);
  }

  run(id: string, signal?: AbortSignal): Promise<RunSummary> {
    return this.get(`/api/runs/${encodeURIComponent(id)}`, undefined, signal);
  }

  sendMessage(id: string, message: string): Promise<Record<string, never>> {
    return this.post(`/api/runs/${encodeURIComponent(id)}/messages`, { message });
  }

  cancelRun(id: string): Promise<Record<string, never>> {
    return this.post(`/api/runs/${encodeURIComponent(id)}/cancel`);
  }

  answer(id: string, questionId: string, answer: string): Promise<Record<string, never>> {
    return this.post(`/api/runs/${encodeURIComponent(id)}/answer`, { question_id: questionId, answer });
  }

  // ------------------------------------------------------ approvals, audit

  approvals(state?: Approval["state"], signal?: AbortSignal): Promise<{ approvals: Approval[] }> {
    return this.get("/api/approvals", { state }, signal);
  }

  decide(id: string, req: DecisionRequest): Promise<Approval> {
    return this.post(`/api/approvals/${encodeURIComponent(id)}`, req);
  }

  audit(limit = 100, signal?: AbortSignal): Promise<{ entries: AuditEntry[] }> {
    return this.get("/api/audit", { limit }, signal);
  }

  // --------------------------------------------------------------- streams

  stream(
    path: string,
    query: Query | ((lastEventId: string) => Query) | undefined,
    opts: Omit<SseOptions, "url" | "headers">,
  ): SseConnection {
    const url = typeof query === "function" ? (last: string) => this.url(path, query(last)) : this.url(path, query);
    return openSse({ ...opts, url, headers: this.authHeaders() });
  }

  /** Live values of the given points (or of one device). The first events are the cached values. */
  live(
    target: { ids: string[] } | { device: string },
    onReading: (r: Reading) => void,
    opts: Omit<SseOptions, "url" | "headers" | "onMessage"> = {},
  ): SseConnection {
    const query = "ids" in target ? { ids: target.ids.join(",") } : { device: target.device };
    return this.stream("/api/live", query, {
      ...opts,
      onMessage: (msg) => {
        if (msg.event !== "reading") return;
        const r = parseJson(msg.data);
        if (isReading(r)) onReading(r);
        else console.warn("ignoring malformed reading", msg.data);
      },
    });
  }

  /** Replays the run's events after `after`, then streams live ones; resumes with Last-Event-ID. */
  runEvents(
    runId: string,
    after: number,
    onEvent: (ev: RunEvent) => void,
    opts: Omit<SseOptions, "url" | "headers" | "onMessage"> = {},
  ): SseConnection {
    // The resume point goes in both ?after= and Last-Event-ID, so it does not
    // matter which of the two the server prefers.
    const query = (last: string): Query => ({ after: Math.max(after, Number(last) || 0) });
    return this.stream(`/api/runs/${encodeURIComponent(runId)}/events`, query, {
      ...opts,
      lastEventId: opts.lastEventId ?? (after > 0 ? String(after) : undefined),
      onMessage: (msg) => {
        const ev = parseJson(msg.data);
        if (isRunEvent(ev)) onEvent(ev);
        else console.warn("ignoring malformed run event", msg.data);
      },
    });
  }
}

function errorFromResponse(res: Response, text: string): ApiError {
  const parsed = parseJson(text) as Partial<ApiErrorBody> | undefined;
  const err = parsed?.error;
  if (err && typeof err.code === "string" && typeof err.message === "string") {
    return new ApiError(res.status, err.code, err.message, err.details);
  }
  const fallback = res.status === 401 ? "unauthorized" : `http_${res.status}`;
  return new ApiError(res.status, fallback, text.trim().slice(0, 200) || res.statusText || `HTTP ${res.status}`);
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isReading(v: unknown): v is Reading {
  return isObject(v) && typeof v.id === "string" && typeof v.ts === "number" && typeof v.quality === "string";
}

function isRunEvent(v: unknown): v is RunEvent {
  return isObject(v) && typeof v.seq === "number" && typeof v.type === "string" && typeof v.run_id === "string";
}
