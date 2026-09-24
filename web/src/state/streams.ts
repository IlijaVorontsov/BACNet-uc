/** Hooks around the SSE streams: live point values and a run's events. */

import { useEffect, useReducer, useRef, useState } from "react";
import type { SseStatus } from "../api/sse";
import type { Reading, RunEvent } from "../api/types";
import { useHub } from "./hub";
import { initialRunView, runReducer, type RunView } from "./runReducer";

/** Calls `flush` once per frame while the page is visible, and soon after otherwise. */
function frameScheduler(flush: () => void): { schedule: () => void; cancel: () => void } {
  let raf = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const run = (): void => {
    raf = 0;
    if (timer) clearTimeout(timer);
    timer = null;
    flush();
  };
  return {
    schedule() {
      if (raf || timer) return;
      if (typeof requestAnimationFrame === "function" && document.visibilityState === "visible") {
        raf = requestAnimationFrame(run);
        // rAF stops in background tabs; the timer makes sure events still land.
        timer = setTimeout(run, 250);
      } else {
        timer = setTimeout(run, 50);
      }
    },
    cancel() {
      if (raf) cancelAnimationFrame(raf);
      if (timer) clearTimeout(timer);
      raf = 0;
      timer = null;
    },
  };
}

export interface LiveValue {
  reading: Reading;
  /** performance.now() of the last value change, 0 for the first value. */
  changedAt: number;
}

export type LiveTarget = { ids: readonly string[] } | { device: string } | null;

function targetKey(t: LiveTarget): string {
  if (!t) return "";
  if ("device" in t) return `device:${t.device}`;
  return t.ids.length ? `ids:${[...t.ids].sort().join(",")}` : "";
}

/** Live readings of points (API `GET /api/live`), batched per frame. */
export function useLive(target: LiveTarget): { values: ReadonlyMap<string, LiveValue>; status: SseStatus | null } {
  const { client } = useHub();
  const [values, setValues] = useState<ReadonlyMap<string, LiveValue>>(() => new Map());
  const [status, setStatus] = useState<SseStatus | null>(null);
  const key = targetKey(target);
  const targetRef = useRef(target);
  targetRef.current = target;

  useEffect(() => {
    const t = targetRef.current;
    if (!key || !t) {
      setStatus(null);
      return;
    }
    const pending = new Map<string, Reading>();
    const sched = frameScheduler(() => {
      if (pending.size === 0) return;
      const batch = [...pending.values()];
      pending.clear();
      setValues((prev) => {
        const next = new Map(prev);
        const now = performance.now();
        for (const r of batch) {
          const old = prev.get(r.id);
          const changed = old !== undefined && old.reading.value !== r.value;
          next.set(r.id, { reading: r, changedAt: changed ? now : (old?.changedAt ?? 0) });
        }
        return next;
      });
    });
    const ctrl = new AbortController();
    client.live(
      "device" in t ? { device: t.device } : { ids: [...t.ids] },
      (r) => {
        pending.set(r.id, r);
        sched.schedule();
      },
      { signal: ctrl.signal, onStatus: (s) => setStatus(s) },
    );
    return () => {
      ctrl.abort();
      sched.cancel();
    };
  }, [client, key]);

  return { values, status };
}

/** A run's view model, folded from its event stream (replay, then live). */
export function useRun(runId: string | null): { view: RunView; status: SseStatus | null } {
  const { client, onRunEvent } = useHub();
  const [view, dispatch] = useReducer(runReducer, runId, initialRunView);
  const [status, setStatus] = useState<SseStatus | null>(null);
  const onEventRef = useRef(onRunEvent);
  onEventRef.current = onRunEvent;

  useEffect(() => {
    dispatch({ type: "reset", runId });
    setStatus(null);
    if (!runId) return;
    const queue: RunEvent[] = [];
    const sched = frameScheduler(() => {
      if (queue.length) dispatch({ type: "events", events: queue.splice(0) });
    });
    const ctrl = new AbortController();
    client.runEvents(
      runId,
      0,
      (ev) => {
        queue.push(ev);
        onEventRef.current(ev);
        sched.schedule();
      },
      { signal: ctrl.signal, onStatus: (s) => setStatus(s) },
    );
    return () => {
      ctrl.abort();
      sched.cancel();
    };
  }, [client, runId]);

  return { view, status };
}
