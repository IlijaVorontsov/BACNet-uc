/**
 * View state shared by the desktop and phone layouts: which run is open, what
 * the site tree selects, the workspace tab, the composer draft. Kept in one
 * context so that, for example, an approval card can open the Changes tab.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { scopeName } from "../lib/site";
import { useHub } from "./hub";
import { isActive } from "./runReducer";

export type Scope = { kind: "site" } | { kind: "space"; id: string } | { kind: "device"; name: string };
export type WorkTab = "points" | "changes" | "tests" | "devices";
export type PhoneTab = "agent" | "site" | "changes" | "field";

export interface Draft {
  text: string;
  playbook?: string;
}

export interface Ui {
  /** The open run; null means "start a new run". */
  runId: string | null;
  /** True until the first run list decided which run to open. */
  runPending: boolean;
  selectRun: (id: string | null) => void;
  scope: Scope;
  setScope: (s: Scope) => void;
  query: string;
  setQuery: (q: string) => void;
  workTab: WorkTab;
  setWorkTab: (t: WorkTab) => void;
  phoneTab: PhoneTab;
  setPhoneTab: (t: PhoneTab) => void;
  draft: Draft;
  setDraft: (d: Draft) => void;
  /** Bumped to ask the composer to take focus. */
  focusComposer: number;
  askAgent: (text: string, playbook?: string) => void;
  fieldDevice: string | null;
  setFieldDevice: (name: string | null) => void;
}

const UiContext = createContext<Ui | null>(null);

function runFromHash(): string | undefined {
  const m = /(?:^#|&)run=([^&]+)/.exec(window.location.hash);
  return m?.[1] ? decodeURIComponent(m[1]) : undefined;
}

export function UiProvider({ children }: { children: ReactNode }) {
  const hub = useHub();
  // undefined: nothing chosen yet, pick a run when the list arrives.
  const [runId, setRunId] = useState<string | null | undefined>(runFromHash);
  const [scope, setScope] = useState<Scope>({ kind: "site" });
  const [query, setQuery] = useState("");
  const [workTab, setWorkTab] = useState<WorkTab>("points");
  const [phoneTab, setPhoneTab] = useState<PhoneTab>("agent");
  const [draft, setDraft] = useState<Draft>({ text: "" });
  const [focusComposer, setFocusComposer] = useState(0);
  const [fieldDevice, setFieldDevice] = useState<string | null>(null);
  const picked = useRef(runId !== undefined);

  const runs = hub.runs.data?.runs;
  const runsFailed = hub.runs.error !== null;
  useEffect(() => {
    if (picked.current || (!runs && !runsFailed)) return;
    picked.current = true;
    const active = runs?.find((r) => isActive(r.state));
    setRunId(active?.id ?? runs?.[0]?.id ?? null);
  }, [runs, runsFailed]);

  useEffect(() => {
    if (runId === undefined) return;
    const hash = runId ? `#run=${encodeURIComponent(runId)}` : "";
    if (window.location.hash === hash) return;
    try {
      window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}${hash}`);
    } catch {
      // Not fatal: the hash only helps reopening the same run.
    }
  }, [runId]);

  const selectRun = useCallback((id: string | null) => {
    picked.current = true;
    setRunId(id);
  }, []);

  const askAgent = useCallback((text: string, playbook?: string) => {
    setDraft(playbook ? { text, playbook } : { text });
    setFocusComposer((n) => n + 1);
    setPhoneTab("agent");
  }, []);

  const value = useMemo<Ui>(
    () => ({
      runId: runId ?? null,
      runPending: runId === undefined,
      selectRun,
      scope,
      setScope,
      query,
      setQuery,
      workTab,
      setWorkTab,
      phoneTab,
      setPhoneTab,
      draft,
      setDraft,
      focusComposer,
      askAgent,
      fieldDevice,
      setFieldDevice,
    }),
    [runId, selectRun, scope, query, workTab, phoneTab, draft, focusComposer, askAgent, fieldDevice],
  );
  return <UiContext.Provider value={value}>{children}</UiContext.Provider>;
}

export function useUi(): Ui {
  const ui = useContext(UiContext);
  if (!ui) throw new Error("useUi outside UiProvider");
  return ui;
}

/** Display name of a scope: the site description, a space name or a device name. */
export function useScopeName(scope: Scope): string | null {
  const { site } = useHub();
  return scopeName(site.data, scope);
}
