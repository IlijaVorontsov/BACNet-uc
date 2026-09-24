/** Mutable state of the mock site: devices, points, live values, plan, tests, audit. */

import type {
  AuditEntry,
  Device,
  DeviceDescription,
  ManifestRevision,
  Plan,
  PointsPage,
  PointsQuery,
  PointWithReading,
  Reading,
  Site,
  Space,
  TestResult,
} from "../types";
import {
  DRAFT_YAML_EXTRA,
  MANIFEST_YAML,
  r204PlanPoints,
  SITE,
  SPACES,
  seedDevices,
  seedPoints,
  seedReading,
  testResults,
  type SeedPoint,
} from "./data";

type ReadingListener = (r: Reading) => void;

export interface MockSiteOptions {
  now: () => number;
  random?: () => number;
  /** Interval of the live-value random walk. */
  tickMs?: number;
}

export class MockSite {
  readonly name = SITE;
  readonly description = "HQ building";
  readonly spaces: Space[] = SPACES.map((s) => ({ ...s }));
  readonly devices: Device[];
  private readonly points = new Map<string, SeedPoint>();
  private readonly readings = new Map<string, Reading>();
  plan: Plan | null = null;
  liveRevision = 16;
  draftRevision: number | null = null;
  readonly revisions: ManifestRevision[];
  tests: TestResult[] = [];
  testsUpdatedAt: number | null = null;
  readonly audit: AuditEntry[] = [];

  private readonly listeners = new Set<ReadingListener>();
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly now: () => number;
  private readonly random: () => number;
  private readonly tickMs: number;

  constructor(opts: MockSiteOptions) {
    this.now = opts.now;
    this.random = opts.random ?? Math.random;
    this.tickMs = opts.tickMs ?? 1500;
    const t = this.now();
    this.devices = seedDevices(t);
    for (const p of seedPoints()) this.addPoint(p, t);
    this.revisions = [
      { revision: 15, created_at: t - 86400 * 6, author: "ilija", message: "Floor 2 rooms 201-203", live: false },
      { revision: 16, created_at: t - 86400 * 2, author: "ilija", message: "Room 205, AHU-1 mapping", live: true },
    ];
    this.tests = [...testResults("sim", "not-run"), ...testResults("live", "not-run")];
  }

  private addPoint(p: SeedPoint, ts: number): void {
    this.points.set(p.point.id, p);
    const online = this.device(p.point.device)?.online ?? false;
    this.readings.set(p.point.id, seedReading(p, online, ts - 1 - this.random() * 20));
  }

  device(name: string): Device | undefined {
    return this.devices.find((d) => d.name === name);
  }

  hasPoint(id: string): boolean {
    return this.points.has(id);
  }

  reading(id: string): Reading | undefined {
    return this.readings.get(id);
  }

  private pointCount(device: string): number {
    let n = 0;
    for (const p of this.points.values()) if (p.point.device === device) n++;
    return n;
  }

  siteJson(): Site {
    const devices = this.devices.map((d) => ({ ...d, points: this.pointCount(d.name) }));
    return {
      name: this.name,
      description: this.description,
      spaces: this.spaces.map((s) => ({ ...s })),
      devices,
      summary: {
        devices: devices.length,
        online: devices.filter((d) => d.online).length,
        points: this.points.size,
        unassigned: devices.filter((d) => d.space === null).length,
        pending_changes: this.plan?.changes.length ?? 0,
      },
    };
  }

  describe(name: string): DeviceDescription | null {
    const d = this.device(name);
    if (!d) return null;
    const points = [...this.points.values()].filter((p) => p.point.device === name).map((p) => ({ ...p.point }));
    const apps = d.protocol === "bacnet-uc" ? this.appsOf(name) : [];
    const extra: Record<string, unknown> =
      d.protocol === "bacnet-uc"
        ? { board: d.model, uptime_s: 86112, free_heap: 41236, smp: d.address }
        : d.protocol === "mqtt"
          ? { topic: d.address, last_payload: d.name === "r204-co2" ? { ppm: 612, bat: { pct: 87 } } : { uptime_s: 86112 } }
          : { vendor: "third party", object_count: points.length };
    return { device: { ...d, points: points.length }, points, apps, extra };
  }

  private appsOf(name: string): DeviceDescription["apps"] {
    const room = /^r20\d-ctl$/.test(name);
    const hasThermostat = room && (name !== "r204-ctl" || this.liveRevision >= 17);
    const online = this.device(name)?.online ?? false;
    const app = (appName: string, file: string, perms: string[]): DeviceDescription["apps"][number] => ({
      name: appName,
      file,
      state: online ? "running" : "stopped",
      autostart: true,
      period_ms: 1000,
      heap_kb: 8,
      stack_kb: 4,
      perms,
      ticks: online ? 84210 : 0,
      events: online ? 312 : 0,
      errors: 0,
      last_error: "",
      uptime_ms: online ? 84210000 : 0,
    });
    const apps = hasThermostat ? [app("thermostat", "/lfs/apps/thermostat.wasm", ["bacnet.local"])] : [];
    if (name === "r204-ctl" && this.liveRevision >= 17) {
      apps.push(app("link", "/lfs/apps/uc-link.wasm", ["bacnet.local", "bacnet.remote"]));
    }
    return apps;
  }

  /** Space ids of `id` and all spaces below it. */
  subtree(id: string): Set<string> {
    const out = new Set([id]);
    let grew = true;
    while (grew) {
      grew = false;
      for (const s of this.spaces) {
        if (s.parent !== null && out.has(s.parent) && !out.has(s.id)) {
          out.add(s.id);
          grew = true;
        }
      }
    }
    return out;
  }

  queryPoints(q: PointsQuery): PointsPage {
    const needle = (q.q ?? "").trim().toLowerCase();
    const spaces = q.space ? this.subtree(q.space) : null;
    const matches = [...this.points.values()]
      .map((p) => p.point)
      .filter((p) => {
        if (q.device && p.device !== q.device) return false;
        if (spaces && (p.space === null || !spaces.has(p.space))) return false;
        if (q.tag && !p.tags.includes(q.tag)) return false;
        if (!needle) return true;
        const hay = [p.id, p.name, p.description, p.source, ...p.tags].join(" ").toLowerCase();
        return needle.split(/\s+/).every((w) => hay.includes(w));
      })
      .sort((a, b) => a.device.localeCompare(b.device) || a.obj.localeCompare(b.obj, undefined, { numeric: true }));
    const offset = Math.max(0, q.offset ?? 0);
    const limit = Math.min(1000, Math.max(1, q.limit ?? 200));
    const points: PointWithReading[] = matches.slice(offset, offset + limit).map((p) => {
      const r = this.readings.get(p.id);
      return r ? { ...p, reading: { ...r } } : { ...p };
    });
    return { total: matches.length, points };
  }

  // -------------------------------------------------------- live values

  subscribe(fn: ReadingListener): () => void {
    this.listeners.add(fn);
    if (!this.timer) this.timer = setInterval(() => this.tick(), this.tickMs);
    return () => {
      this.listeners.delete(fn);
      if (this.listeners.size === 0 && this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
    };
  }

  /** One random-walk step: moves about half of the walking points. */
  tick(): void {
    const ts = this.now();
    for (const p of this.points.values()) {
      if (!p.walk || this.random() < 0.5) continue;
      const cur = this.readings.get(p.point.id);
      if (!cur || cur.quality !== "good" || typeof cur.value !== "number") continue;
      const { step, min, max } = p.walk;
      let v = cur.value + (this.random() - 0.5) * 2 * step;
      v = Math.min(max, Math.max(min, v));
      v = p.point.units === "percent" || p.point.units === "parts-per-million" ? Math.round(v) : Math.round(v * 100) / 100;
      if (v === cur.value) continue;
      this.publish({ ...cur, value: v, ts });
    }
  }

  private publish(r: Reading): void {
    this.readings.set(r.id, r);
    for (const fn of this.listeners) {
      try {
        fn(r);
      } catch (err) {
        console.error("mock live listener failed", err);
      }
    }
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.listeners.clear();
  }

  // ---------------------------------------------------- manifest, plan

  setPlan(plan: Plan | null): void {
    this.plan = plan;
    this.draftRevision = plan ? plan.revision : null;
  }

  manifestYaml(): { yaml: string; draft_yaml: string | null } {
    return { yaml: MANIFEST_YAML, draft_yaml: this.plan ? `${MANIFEST_YAML}${DRAFT_YAML_EXTRA}` : null };
  }

  /** Makes plan p17 live: r204-ctl gets its new points, the draft is gone. */
  applyPlan(author: string): void {
    const plan = this.plan;
    if (!plan) return;
    const ts = this.now();
    if (plan.id === "p17") for (const p of r204PlanPoints()) this.addPoint(p, ts);
    for (const r of this.revisions) r.live = false;
    this.revisions.push({ revision: plan.revision, created_at: ts, author, message: `Apply plan ${plan.id}`, live: true });
    this.liveRevision = plan.revision;
    this.setPlan(null);
  }

  setTests(results: TestResult[]): void {
    const byKey = new Map(this.tests.map((t) => [`${t.target}:${t.name}`, t]));
    for (const r of results) byKey.set(`${r.target}:${r.name}`, r);
    this.tests = [...byKey.values()];
    this.testsUpdatedAt = this.now();
  }

  addAudit(entry: Omit<AuditEntry, "ts">): void {
    this.audit.unshift({ ts: this.now(), ...entry });
    if (this.audit.length > 500) this.audit.length = 500;
  }
}
