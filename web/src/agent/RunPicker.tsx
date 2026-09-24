import { useEffect, useId, useRef, useState } from "react";
import type { RunSummary } from "../api/types";
import { formatAgo, runStateTone } from "../lib/format";
import { useHub } from "../state/hub";
import { useUi } from "../state/ui";
import { Icon } from "../ui/common";

/** Disclosure listing the runs, newest first; picking one opens it. */
export function RunPicker({ current }: { current: RunSummary | null }) {
  const hub = useHub();
  const ui = useUi();
  const [open, setOpen] = useState(false);
  const listId = useId();
  const root = useRef<HTMLDivElement>(null);
  const runs = hub.runs.data?.runs ?? [];

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent): void => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const title = ui.runPending ? "Loading…" : ui.runId ? (current?.title ?? "Run") : "New run";
  return (
    <div className="runpick" ref={root}>
      <button
        type="button"
        className="runbtn"
        aria-expanded={open}
        aria-controls={listId}
        disabled={ui.runPending}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="t">{title}</span>
        <Icon name="chevron" size={14} />
        <span className="sr-only">, show runs</span>
      </button>
      {open && (
        <ul id={listId} className="runlist" aria-label="Runs">
          <li>
            <button
              type="button"
              className="runitem new"
              onClick={() => {
                ui.selectRun(null);
                setOpen(false);
              }}
            >
              <Icon name="plus" size={14} /> New run
            </button>
          </li>
          {runs.map((r) => {
            const tone = runStateTone(r.state);
            return (
              <li key={r.id}>
                <button
                  type="button"
                  className="runitem"
                  aria-current={r.id === ui.runId ? "true" : undefined}
                  onClick={() => {
                    ui.selectRun(r.id);
                    setOpen(false);
                  }}
                >
                  <span className="t">{r.title}</span>
                  <span className={`chip ${tone.tone}`}>{tone.label}</span>
                  <span className="when">
                    {r.created_by} · {formatAgo(r.updated_at)}
                  </span>
                </button>
              </li>
            );
          })}
          {runs.length === 0 && <li className="mute-t small empty">No runs yet.</li>}
        </ul>
      )}
    </div>
  );
}
