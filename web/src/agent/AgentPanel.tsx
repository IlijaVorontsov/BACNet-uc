import { formatAgo, runStateTone } from "../lib/format";
import { useCancel } from "../state/actions";
import { useHub } from "../state/hub";
import { isActive } from "../state/runReducer";
import { useRun } from "../state/streams";
import { useUi } from "../state/ui";
import { ErrorNote, Icon, RunStateChip } from "../ui/common";
import { Composer } from "./Composer";
import { RunPicker } from "./RunPicker";
import { RunStream } from "./RunStream";

function NewRunIntro() {
  const hub = useHub();
  const ui = useUi();
  const runs = hub.runs.data?.runs ?? [];
  const llm = hub.health.data?.llm;
  return (
    <div className="stream intro-stream">
      <div className="newrun">
        <h4>New run</h4>
        <p className="mute-t">
          Describe the task, or pick a playbook below. The run continues on the gateway if you close this page.
        </p>
        {llm && !llm.configured && (
          <p className="warn-t small">The hub has no LLM configured; the agent cannot answer until an admin sets one up.</p>
        )}
      </div>
      {runs.length > 0 && (
        <div className="recent">
          <p className="label">Recent runs</p>
          <ul>
            {runs.slice(0, 6).map((r) => {
              const t = runStateTone(r.state);
              return (
                <li key={r.id}>
                  <button type="button" className="runitem" onClick={() => ui.selectRun(r.id)}>
                    <span className="t">{r.title}</span>
                    <span className={`chip ${t.tone}`}>{t.label}</span>
                    <span className="when">{formatAgo(r.updated_at)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

/** Right-hand pane of the desktop layout. */
export function AgentPanel() {
  const hub = useHub();
  const ui = useUi();
  const { view, status } = useRun(ui.runId);
  const summary = hub.runs.data?.runs.find((r) => r.id === ui.runId) ?? null;
  const state = view.state ?? summary?.state ?? null;
  const { cancel, busy, error } = useCancel(ui.runId);
  const model = summary?.model ?? hub.health.data?.llm.model ?? "";

  return (
    <aside className="agent" aria-label="Agent">
      <div className="ahead">
        <h3>Agent</h3>
        {model && <span className="model">{model}</span>}
        {ui.runId && (
          <RunStateChip state={state} reconnecting={status === "reconnecting"} error={view.turnError !== null} />
        )}
      </div>
      <div className="runbar">
        <RunPicker current={summary} />
        {ui.runId && isActive(state) && (
          <button type="button" className="btn sm danger" onClick={cancel} disabled={busy} title="Cancel the run">
            Stop
          </button>
        )}
        {ui.runId && (
          <button type="button" className="ibtn" onClick={() => ui.selectRun(null)} aria-label="New run" title="New run">
            <Icon name="plus" size={16} />
          </button>
        )}
      </div>
      <ErrorNote error={error ? { message: error } : null} />
      {status === "failed" && <ErrorNote>Cannot open the run's event stream. Check that you are signed in.</ErrorNote>}
      {ui.runPending ? (
        <p className="stream mute-t small">Loading runs…</p>
      ) : ui.runId ? (
        <RunStream key={ui.runId} view={view} runId={ui.runId} variant="desktop" />
      ) : (
        <NewRunIntro />
      )}
      <Composer variant="desktop" state={ui.runId ? state : null} />
    </aside>
  );
}
