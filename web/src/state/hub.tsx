/**
 * App-wide data from the hub: health, user, site, plan, tests, runs and
 * pending approvals. Each is a small polled resource; run events refresh the
 * ones they affect, so the workspace follows what the agent does.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, isAbort, type ApiClient } from "../api/client";
import type { Approval, Health, ManifestInfo, Me, Plan, RunEvent, RunSummary, Site, TestsInfo } from "../api/types";

export interface Resource<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err;
  return new ApiError(0, "error", err instanceof Error ? err.message : String(err));
}

function documentVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

/**
 * Loads `load` now, on `reload()`, when `deps` change and every `pollMs` while
 * the page is visible. Keeps the last good data while reloading or after an
 * error, and aborts requests that are no longer needed. A poll never restarts
 * a request that is still in flight: with a hung gateway it must be allowed to
 * time out, or the error would never show and stale data would look current.
 */
export function useResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  pollMs = 0,
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const inFlight = useRef(false);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const ctrl = new AbortController();
    inFlight.current = true;
    setLoading(true);
    loadRef
      .current(ctrl.signal)
      .then((d) => {
        if (ctrl.signal.aborted) return;
        setData(d);
        setError(null);
      })
      .catch((err: unknown) => {
        if (ctrl.signal.aborted || isAbort(err)) return;
        setError(toApiError(err));
      })
      .finally(() => {
        if (ctrl.signal.aborted) return;
        inFlight.current = false;
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [tick, ...deps]);

  useEffect(() => {
    if (pollMs <= 0) return;
    const poll = (): void => {
      if (documentVisible() && !inFlight.current) reload();
    };
    const timer = setInterval(poll, pollMs);
    document.addEventListener("visibilitychange", poll);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, [pollMs, reload]);

  return { data, error, loading, reload };
}

/** Collapses bursts of calls (a replayed run can carry many events) into one. */
function useCoalesced(fn: () => void, ms: number): () => void {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return useCallback(() => {
    if (timer.current) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      fnRef.current();
    }, ms);
  }, [ms]);
}

export interface Hub {
  client: ApiClient;
  mock: boolean;
  health: Resource<Health>;
  me: Resource<Me>;
  site: Resource<Site>;
  plan: Resource<{ plan: Plan | null }>;
  manifest: Resource<ManifestInfo>;
  tests: Resource<TestsInfo>;
  runs: Resource<{ runs: RunSummary[] }>;
  approvals: Resource<{ approvals: Approval[] }>;
  /** Run streams report every event here so dependent data is refreshed. */
  onRunEvent: (ev: RunEvent) => void;
}

const HubContext = createContext<Hub | null>(null);

export function HubProvider({ client, mock, children }: { client: ApiClient; mock: boolean; children: ReactNode }) {
  const health = useResource((s) => client.health(s), [client], 15000);
  const me = useResource((s) => client.me(s), [client]);
  const site = useResource((s) => client.site(s), [client], 20000);
  const plan = useResource((s) => client.plan(s), [client], 15000);
  const manifest = useResource((s) => client.manifest(s), [client], 60000);
  const tests = useResource((s) => client.tests(s), [client], 60000);
  const runs = useResource((s) => client.runs(30, s), [client], 10000);
  const approvals = useResource((s) => client.approvals("pending", s), [client], 15000);

  const refreshPlan = useCoalesced(() => {
    plan.reload();
    site.reload();
    manifest.reload();
  }, 150);
  const refreshTests = useCoalesced(tests.reload, 150);
  const refreshApprovals = useCoalesced(() => {
    approvals.reload();
    runs.reload();
  }, 150);
  const refreshRuns = useCoalesced(runs.reload, 150);

  const onRunEvent = useCallback(
    (ev: RunEvent) => {
      switch (ev.type) {
        case "plan.updated":
          refreshPlan();
          break;
        case "tests.updated":
          refreshTests();
          break;
        case "approval.request":
        case "approval.decided":
          refreshApprovals();
          break;
        case "run.state":
        case "message.user":
          refreshRuns();
          break;
        default:
          break;
      }
    },
    [refreshPlan, refreshTests, refreshApprovals, refreshRuns],
  );

  const value = useMemo<Hub>(
    () => ({ client, mock, health, me, site, plan, manifest, tests, runs, approvals, onRunEvent }),
    [client, mock, health, me, site, plan, manifest, tests, runs, approvals, onRunEvent],
  );
  return <HubContext.Provider value={value}>{children}</HubContext.Provider>;
}

export function useHub(): Hub {
  const hub = useContext(HubContext);
  if (!hub) throw new Error("useHub outside HubProvider");
  return hub;
}

/** The plan's warnings among an apply approval's summary lines (the hub lists them after the targets). */
export function usePlanWarnings(approval: Approval): ReadonlySet<string> {
  const current = useHub().plan.data?.plan ?? null;
  return useMemo(
    () => new Set(current && current.id === approval.plan_id ? current.warnings : []),
    [current, approval.plan_id],
  );
}

/** Roles that may approve a tier (API.md "Approvals"). */
export function canApprove(me: Me | null, tier: Approval["tier"]): boolean {
  if (!me) return false;
  const roles = new Set(me.roles);
  if (tier === "C") return roles.has("commissioner") || roles.has("admin");
  if (tier === "L") return roles.has("operator") || roles.has("commissioner") || roles.has("admin");
  return true;
}
