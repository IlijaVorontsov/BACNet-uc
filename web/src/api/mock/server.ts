/**
 * In-browser implementation of the whole uc-hub API (docs/ai-harness/API.md)
 * on top of `MockSite` and scripted runs. It answers real `Request`s with real
 * `Response`s (JSON, or SSE streamed through a `ReadableStream`), so the app's
 * client and SSE code run unchanged against it.
 */

import type { Approval, CreateRunRequest, DecisionRequest, Reading, RunEvent, RunSummary } from "../types";
import { MockRun, runScript, ScriptContext, type ContextOptions, type Ids } from "./runs";
import { commission, pickScript, runTitle, type Script, type ScriptEnv } from "./scripts";
import { MockSite } from "./site";

export interface MockServerOptions {
  /** Speed factor for scripted runs (2 = twice as fast). */
  speed?: number;
  /** Simulated latency of JSON requests. */
  latencyMs?: number;
  /** Interval of the live-value random walk. */
  tickMs?: number;
  /** Seed the two example runs (commissioning and IO checkout). */
  seed?: boolean;
  /** Split SSE frames at random byte positions, as a real network may. */
  splitFrames?: boolean;
  keepAliveMs?: number;
  random?: () => number;
}

const USER = "dev";
const MODEL = "glm-5.3";
const JSON_HEADERS = { "content-type": "application/json" };

class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function errorResponse(status: number, code: string, message: string): Response {
  return json({ error: { code, message } }, status);
}

type Params = Record<string, string>;
type Handler = (req: Request, url: URL, params: Params) => Response | Promise<Response>;

interface Route {
  method: string;
  pattern: RegExp;
  keys: string[];
  handler: Handler;
}

function route(method: string, path: string, handler: Handler): Route {
  const keys: string[] = [];
  const pattern = new RegExp(
    `^${path.replace(/:([a-z]+)/g, (_m, k: string) => {
      keys.push(k);
      return "([^/]+)";
    })}$`,
  );
  return { method, pattern, keys, handler };
}

async function readBody<T>(req: Request): Promise<Partial<T>> {
  const text = await req.text();
  if (!text) return {};
  try {
    const body = JSON.parse(text) as unknown;
    if (typeof body !== "object" || body === null || Array.isArray(body)) throw new Error("not an object");
    return body as Partial<T>;
  } catch {
    throw new HttpError(400, "invalid", "request body must be a JSON object");
  }
}

function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("The operation was aborted.", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

interface SseWriter {
  send(event: string, data: unknown, id?: number): void;
}

export class MockServer {
  readonly site: MockSite;
  readonly ready: Promise<void>;
  private readonly runs = new Map<string, MockRun>();
  private readonly approvals = new Map<string, Approval>();
  private readonly routes: Route[];
  private readonly opts: Required<Omit<MockServerOptions, "random">> & { random: () => number };
  private counter = 10;
  private runCounter = 0x8f2a;
  private readonly ids: Ids = { next: (prefix) => `${prefix}${++this.counter}` };
  private readonly env: ScriptEnv = {
    pendingApprovalFor: (planId) =>
      [...this.approvals.values()].find((a) => a.plan_id === planId && a.state === "pending"),
  };

  constructor(opts: MockServerOptions = {}) {
    this.opts = {
      speed: opts.speed ?? 1,
      latencyMs: opts.latencyMs ?? 40,
      tickMs: opts.tickMs ?? 1500,
      seed: opts.seed ?? true,
      splitFrames: opts.splitFrames ?? true,
      keepAliveMs: opts.keepAliveMs ?? 15000,
      random: opts.random ?? Math.random,
    };
    this.site = new MockSite({ now: () => Date.now() / 1000, random: this.opts.random, tickMs: this.opts.tickMs });
    this.routes = this.buildRoutes();
    this.ready = this.opts.seed ? this.seed() : Promise.resolve();
  }

  /** Stops timers and running scripts. */
  dispose(): void {
    for (const run of this.runs.values()) run.cancel();
    this.site.stop();
  }

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    await this.ready;
    const r = this.routes.find((x) => x.pattern.test(url.pathname) && x.method === req.method);
    if (!r) {
      const known = this.routes.some((x) => x.pattern.test(url.pathname));
      return known
        ? errorResponse(405, "invalid", `${req.method} is not allowed on ${url.pathname}`)
        : errorResponse(404, "not_found", `no route for ${url.pathname}`);
    }
    const m = r.pattern.exec(url.pathname);
    const params: Params = {};
    r.keys.forEach((k, i) => {
      params[k] = decodeURIComponent(m?.[i + 1] ?? "");
    });
    if (req.headers.get("accept") !== "text/event-stream" && this.opts.latencyMs > 0) {
      await delay(this.opts.latencyMs * (0.5 + this.opts.random()), req.signal);
    }
    try {
      return await r.handler(req, url, params);
    } catch (err) {
      if (err instanceof HttpError) return errorResponse(err.status, err.code, err.message);
      throw err;
    }
  }

  // ------------------------------------------------------------ runs

  private startRun(
    message: string,
    playbook: string | undefined,
    extra: { user?: string; title?: string; virtualStart?: number; seedAnswers?: [string, string][]; script?: Script } = {},
  ): { run: MockRun; live: Promise<void>; finished: Promise<void> } {
    const user = extra.user ?? USER;
    const createdAt = extra.virtualStart ?? Date.now() / 1000;
    const id = `r_${(++this.runCounter).toString(16)}`;
    const run = new MockRun(id, extra.title ?? runTitle(message, playbook), user, createdAt, MODEL);
    this.runs.set(id, run);
    const script = extra.script ?? pickScript(message, playbook, this.env);
    const { ctx, live } = this.context(run, user, extra.virtualStart ?? null, extra.seedAnswers);
    const finished = runScript(ctx, async (c) => {
      c.userMessage(message, user);
      c.state("running");
      await script(c);
    });
    return { run, live: Promise.race([live, finished]), finished };
  }

  private context(
    run: MockRun,
    user: string,
    virtualStart: number | null,
    seedAnswers?: [string, string][],
  ): { ctx: ScriptContext; live: Promise<void> } {
    let onLive = (): void => undefined;
    const live = new Promise<void>((resolve) => {
      onLive = resolve;
    });
    const opts: ContextOptions = {
      site: this.site,
      ids: this.ids,
      addApproval: (a) => this.approvals.set(a.id, a),
      user,
      speed: this.opts.speed,
      virtualStart,
      onLive,
      ...(seedAnswers ? { seedAnswers } : {}),
    };
    return { ctx: new ScriptContext(run, opts), live };
  }

  private async seed(): Promise<void> {
    const now = Date.now() / 1000;
    const first = this.startRun(
      "Commission room 204: map the board's IO, run a thermostat at 21.5 °C, link the AHU-1 supply temperature, bring in the CO2 sensor from MQTT, and write acceptance tests.",
      undefined,
      { title: "Commission room 204", virtualStart: now - 660, script: commission(this.env) },
    );
    await first.live;
    const second = this.startRun("Run IO checkout for r204-ctl.", "io-checkout", {
      user: "tech1",
      virtualStart: now - 240,
      seedAnswers: [["Yes", "tech1"]],
    });
    await second.live;
  }

  private getRun(id: string): MockRun {
    const run = this.runs.get(id);
    if (!run) throw new HttpError(404, "not_found", `no run ${id}`);
    return run;
  }

  // ------------------------------------------------------------ SSE

  private sse(req: Request, setup: (w: SseWriter) => () => void): Response {
    const enc = new TextEncoder();
    const { splitFrames, random, keepAliveMs } = this.opts;
    let cleanup: () => void = () => undefined;
    let closed = false;
    let keepAlive: ReturnType<typeof setInterval> | null = null;
    const finish = (): void => {
      if (closed) return;
      closed = true;
      if (keepAlive) clearInterval(keepAlive);
      cleanup();
    };
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        const write = (text: string): void => {
          if (closed) return;
          const bytes = enc.encode(text);
          if (splitFrames && bytes.length > 12 && random() < 0.5) {
            const cut = 1 + Math.floor(random() * (bytes.length - 2));
            controller.enqueue(bytes.slice(0, cut));
            controller.enqueue(bytes.slice(cut));
          } else {
            controller.enqueue(bytes);
          }
        };
        const writer: SseWriter = {
          send(event, data, id) {
            write(`${id !== undefined ? `id: ${id}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
          },
        };
        write(": connected\n\n");
        keepAlive = setInterval(() => write(": keep-alive\n\n"), keepAliveMs);
        const onAbort = (): void => {
          finish();
          try {
            controller.error(new DOMException("The operation was aborted.", "AbortError"));
          } catch {
            // The consumer already cancelled the stream.
          }
        };
        if (req.signal.aborted) {
          onAbort();
          return;
        }
        req.signal.addEventListener("abort", onAbort, { once: true });
        cleanup = setup(writer);
      },
      cancel: () => finish(),
    });
    return new Response(stream, {
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
    });
  }

  private liveStream(req: Request, url: URL): Response {
    const ids = url.searchParams.get("ids");
    const device = url.searchParams.get("device");
    let wanted: Set<string>;
    if (ids) {
      wanted = new Set(ids.split(",").map((s) => s.trim()).filter(Boolean));
    } else if (device) {
      if (!this.site.device(device)) throw new HttpError(404, "not_found", `no device ${device}`);
      wanted = new Set(this.site.queryPoints({ device, limit: 1000 }).points.map((p) => p.id));
    } else {
      throw new HttpError(400, "invalid", "give ids or device");
    }
    return this.sse(req, (w) => {
      for (const id of wanted) {
        const r = this.site.reading(id);
        if (r) w.send("reading", r);
      }
      return this.site.subscribe((r: Reading) => {
        if (wanted.has(r.id)) w.send("reading", r);
      });
    });
  }

  private runEvents(req: Request, url: URL, run: MockRun): Response {
    const after = Number(url.searchParams.get("after") ?? "0");
    const lastId = Number(req.headers.get("last-event-id") ?? "0");
    if (!Number.isFinite(after) || !Number.isFinite(lastId)) throw new HttpError(400, "invalid", "after must be a number");
    const from = Math.max(after, lastId);
    return this.sse(req, (w) => {
      const send = (ev: RunEvent): void => w.send(ev.type, ev, ev.seq);
      for (const ev of run.events) if (ev.seq > from) send(ev);
      return run.subscribe(send);
    });
  }

  // ---------------------------------------------------------- routes

  private buildRoutes(): Route[] {
    const site = this.site;
    return [
      route("GET", "/api/health", () =>
        json({
          ok: true,
          version: "0.1.0-mock",
          site: site.name,
          llm: { provider: "scripted", model: MODEL, configured: true },
          dev_mode: true,
        }),
      ),
      route("GET", "/api/me", () => json({ user: USER, roles: ["admin"] })),
      route("GET", "/api/site", () => json(site.siteJson())),
      route("GET", "/api/devices/:name", (_req, _url, p) => {
        const d = site.describe(p.name ?? "");
        return d ? json(d) : errorResponse(404, "not_found", `no device ${p.name}`);
      }),
      route("POST", "/api/devices/:name/identify", async (req, _url, p) => {
        const body = await readBody<{ seconds: number }>(req);
        const d = site.device(p.name ?? "");
        if (!d) throw new HttpError(404, "not_found", `no device ${p.name}`);
        const seconds = body.seconds ?? 30;
        if (typeof seconds !== "number" || !Number.isInteger(seconds) || seconds < 1 || seconds > 300) {
          throw new HttpError(400, "validation", "seconds must be an integer from 1 to 300");
        }
        if (!d.online) throw new HttpError(502, "device_error", `${d.name} did not answer (offline)`);
        site.addAudit({ user: USER, run_id: null, action: "identify", tool: "device_identify", tier: "L", args: { device: d.name, seconds }, outcome: "ok", detail: "" });
        return json({});
      }),
      route("GET", "/api/points", (_req, url) => {
        const sp = url.searchParams;
        const num = (k: string): number | undefined => (sp.has(k) ? Number(sp.get(k)) : undefined);
        const limit = num("limit");
        const offset = num("offset");
        if ((limit !== undefined && !Number.isFinite(limit)) || (offset !== undefined && !Number.isFinite(offset))) {
          throw new HttpError(400, "invalid", "limit and offset must be numbers");
        }
        return json(
          site.queryPoints({
            q: sp.get("q") ?? undefined,
            device: sp.get("device") || undefined,
            space: sp.get("space") || undefined,
            tag: sp.get("tag") || undefined,
            limit,
            offset,
          }),
        );
      }),
      route("POST", "/api/points/read", async (req) => {
        const body = await readBody<{ ids: string[] }>(req);
        if (!Array.isArray(body.ids) || !body.ids.every((x) => typeof x === "string")) {
          throw new HttpError(400, "validation", "ids must be a list of point ids");
        }
        const readings = body.ids.map(
          (id): Reading => site.reading(id) ?? { id, value: null, ts: Date.now() / 1000, quality: "fault", error: "unknown point" },
        );
        return json({ readings });
      }),
      route("GET", "/api/live", (req, url) => this.liveStream(req, url)),
      route("POST", "/api/discover", async (req) => {
        const body = await readBody<{ protocol: string; timeout_s: number }>(req);
        await delay(Math.min(2, Math.max(0, body.timeout_s ?? 1)) * 500, req.signal);
        const devices = site.devices
          .filter((d) => !body.protocol || d.protocol === body.protocol)
          .map((d) => ({
            protocol: d.protocol,
            address: d.address,
            instance: d.instance,
            name: d.name,
            model: d.model,
            hwid: d.hwid,
            bacnet_uc: d.protocol === "bacnet-uc",
            extra: {},
            known: d.space !== null,
            device: d.space !== null ? d.name : null,
          }));
        return json({ devices, errors: {} });
      }),
      route("GET", "/api/manifest", () =>
        json({ live_revision: site.liveRevision, draft_revision: site.draftRevision, ...site.manifestYaml() }),
      ),
      route("GET", "/api/manifest/revisions", () => json({ revisions: [...site.revisions].reverse() })),
      route("GET", "/api/plan", () => json({ plan: site.plan })),
      route("GET", "/api/tests", () => json({ results: site.tests, updated_at: site.testsUpdatedAt })),
      route("GET", "/api/runs", (_req, url) => {
        const limit = Math.max(1, Math.min(100, Number(url.searchParams.get("limit") ?? "20") || 20));
        const runs = [...this.runs.values()]
          .map((r) => ({ ...r.summary }))
          .sort((a, b) => b.created_at - a.created_at)
          .slice(0, limit);
        return json({ runs });
      }),
      route("POST", "/api/runs", async (req) => {
        const body = await readBody<CreateRunRequest>(req);
        if (typeof body.message !== "string" || !body.message.trim()) {
          throw new HttpError(400, "validation", "message must be a non-empty string");
        }
        if (body.playbook !== undefined && typeof body.playbook !== "string") {
          throw new HttpError(400, "validation", "playbook must be a string");
        }
        const { run } = this.startRun(body.message.trim(), body.playbook);
        return json({ ...run.summary } satisfies RunSummary, 201);
      }),
      route("GET", "/api/runs/:id", (_req, _url, p) => json({ ...this.getRun(p.id ?? "").summary })),
      route("GET", "/api/runs/:id/events", (req, url, p) => this.runEvents(req, url, this.getRun(p.id ?? ""))),
      route("POST", "/api/runs/:id/messages", async (req, _url, p) => {
        const run = this.getRun(p.id ?? "");
        const body = await readBody<{ message: string }>(req);
        if (typeof body.message !== "string" || !body.message.trim()) {
          throw new HttpError(400, "validation", "message must be a non-empty string");
        }
        const state = run.summary.state;
        if (state === "running" || state === "waiting_approval" || state === "waiting_answer") {
          throw new HttpError(409, "conflict", `run ${run.summary.id} is ${state.replace("_", " ")}`);
        }
        const message = body.message.trim();
        const script = pickScript(message, undefined, this.env);
        run.newTurn();
        const { ctx } = this.context(run, USER, null);
        void runScript(ctx, async (c) => {
          c.userMessage(message);
          c.state("running");
          await script(c);
        });
        return json({}, 202);
      }),
      route("POST", "/api/runs/:id/cancel", (_req, _url, p) => {
        const run = this.getRun(p.id ?? "");
        const state = run.summary.state;
        if (state === "running" || state === "waiting_approval" || state === "waiting_answer") run.cancel();
        return json({});
      }),
      route("POST", "/api/runs/:id/answer", async (req, _url, p) => {
        const run = this.getRun(p.id ?? "");
        const body = await readBody<{ question_id: string; answer: string }>(req);
        if (typeof body.question_id !== "string" || typeof body.answer !== "string" || !body.answer.trim()) {
          throw new HttpError(400, "validation", "question_id and a non-empty answer are required");
        }
        if (!run.answer(body.question_id, body.answer.trim(), USER)) {
          throw new HttpError(409, "conflict", `question ${body.question_id} is not open`);
        }
        return json({});
      }),
      route("GET", "/api/approvals", (_req, url) => {
        const state = url.searchParams.get("state");
        const approvals = [...this.approvals.values()]
          .filter((a) => !state || a.state === state)
          .sort((a, b) => b.requested_at - a.requested_at)
          .map((a) => ({ ...a }));
        return json({ approvals });
      }),
      route("POST", "/api/approvals/:id", async (req, _url, p) => {
        const body = await readBody<DecisionRequest>(req);
        if (body.decision !== "approve" && body.decision !== "reject") {
          throw new HttpError(400, "validation", 'decision must be "approve" or "reject"');
        }
        if (body.scope !== undefined && body.scope !== "call" && body.scope !== "run") {
          throw new HttpError(400, "invalid", 'scope must be "call" or "run"');
        }
        const a = this.approvals.get(p.id ?? "");
        if (!a) throw new HttpError(404, "not_found", `no approval ${p.id}`);
        if (a.state !== "pending") throw new HttpError(409, "conflict", `approval ${a.id} is already ${a.state}`);
        const decision = body.decision === "approve" ? "approved" : "rejected";
        const scope = decision === "approved" ? (body.scope ?? "call") : "call";
        if (scope === "run" && a.tier !== "L") {
          throw new HttpError(400, "invalid", "only tier L approvals can cover the rest of the run");
        }
        const run = this.runs.get(a.run_id);
        if (scope === "run" && run) run.runApprover = USER;
        a.scope = scope;
        a.state = decision;
        a.decided_by = USER;
        a.decided_at = Date.now() / 1000;
        a.comment = typeof body.comment === "string" && body.comment.trim() ? body.comment.trim() : null;
        run?.decide(decision);
        return json({ ...a });
      }),
      route("GET", "/api/audit", (_req, url) => {
        const limit = Math.max(1, Math.min(1000, Number(url.searchParams.get("limit") ?? "100") || 100));
        return json({ entries: site.audit.slice(0, limit) });
      }),
    ];
  }
}
