import { Composer } from "../agent/Composer";
import { PLAYBOOKS } from "../agent/playbooks";
import { RunStream } from "../agent/RunStream";
import { FieldTab } from "../field/FieldTab";
import { runStateTone } from "../lib/format";
import { useCancel } from "../state/actions";
import { useHub } from "../state/hub";
import { isActive, openQuestions } from "../state/runReducer";
import { useRun } from "../state/streams";
import { useScopeName, useUi, type PhoneTab } from "../state/ui";
import { Chip, ErrorNote, Icon } from "../ui/common";
import { HealthChips } from "../ui/HealthChips";
import { TabList, TabPanel, type TabSpec } from "../ui/Tabs";
import { PhoneChanges } from "./PhoneChanges";
import { PhoneSite } from "./PhoneSite";

function NewRunStart() {
  const ui = useUi();
  const scopeName = useScopeName(ui.scope);
  return (
    <div className="pstack newrun">
      <p className="mute-t">Start a run with a playbook, or type below.</p>
      <div className="fgrid">
        {PLAYBOOKS.map((p) => (
          <button key={p.id} type="button" className="fbtn" onClick={() => ui.askAgent(p.prompt({ scope: ui.scope, scopeName }), p.id)}>
            <b>{p.label}</b>
            <small>{p.prompt({ scope: ui.scope, scopeName })}</small>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Phone layout (< 768 px): agent first, with Site, Changes and Field tabs. */
export function PhoneApp() {
  const hub = useHub();
  const ui = useUi();
  const { view, status } = useRun(ui.runId);
  const runs = hub.runs.data?.runs ?? [];
  const summary = runs.find((r) => r.id === ui.runId) ?? null;
  const state = view.state ?? summary?.state ?? null;
  const { cancel, busy, error } = useCancel(ui.runId);
  const approvals = hub.approvals.data?.approvals.length ?? 0;
  const attention = approvals + openQuestions(view).length;
  const siteName = (hub.site.data?.name ?? hub.health.data?.site ?? "").toUpperCase();

  const titles: Record<PhoneTab, [string, string]> = {
    agent: [ui.runId ? (summary?.title ?? "Run") : "New run", ui.runId ? `${siteName} · ${runStateTone(state).label}` : siteName],
    site: [hub.site.data?.description || siteName, `${hub.site.data?.summary.devices ?? 0} devices`],
    changes: ["Approvals", approvals ? `${approvals} waiting` : "Nothing waiting"],
    field: ["Field", "Scan, identify, check"],
  };
  const [title, sub] = titles[ui.phoneTab];

  const badge = (n: number) => <span className={`badge${n ? "" : " zero"}`}>{n}</span>;
  const tabs: TabSpec<PhoneTab>[] = [
    { id: "agent", label: <><Icon name="agent" size={22} />Agent</>, name: "Agent" },
    { id: "site", label: <><Icon name="site" size={22} />Site</>, name: "Site" },
    { id: "changes", label: <><Icon name="changes" size={22} />Changes{approvals > 0 && badge(approvals)}</>, name: `Changes, ${approvals} waiting` },
    { id: "field", label: <><Icon name="field" size={22} />Field</>, name: "Field" },
  ];

  return (
    <div className="app phone">
      <header className="pbar">
        <div className="ptitle">
          <h1>{title}</h1>
          <div className="sub">{sub}</div>
        </div>
        {attention > 0 ? (
          <Chip tone="warn" dot title="Waiting for you">
            {attention}
          </Chip>
        ) : (
          <HealthChips compact />
        )}
      </header>
      <TabPanel prefix="ph" id={ui.phoneTab} className={`pview tab-${ui.phoneTab}`}>
        {ui.phoneTab === "agent" && (
          <>
            <div className="prun">
              <label className="sr-only" htmlFor="ph-run">
                Run
              </label>
              <select id="ph-run" value={ui.runId ?? ""} disabled={ui.runPending} onChange={(e) => ui.selectRun(e.target.value || null)}>
                <option value="">New run</option>
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.title} · {runStateTone(r.state).label}
                  </option>
                ))}
              </select>
              {ui.runId && isActive(state) && (
                <button type="button" className="btn danger" onClick={cancel} disabled={busy} aria-label="Stop the run">
                  Stop
                </button>
              )}
            </div>
            <ErrorNote error={error ? { message: error } : null} />
            {status === "reconnecting" && <p className="small warn-t">Reconnecting…</p>}
            {ui.runPending ? (
              <p className="mute-t small">Loading runs…</p>
            ) : ui.runId ? (
              <RunStream key={ui.runId} view={view} runId={ui.runId} variant="phone" />
            ) : (
              <NewRunStart />
            )}
          </>
        )}
        {ui.phoneTab === "site" && <PhoneSite />}
        {ui.phoneTab === "changes" && <PhoneChanges />}
        {ui.phoneTab === "field" && <FieldTab />}
      </TabPanel>
      <div className="pfoot">
        {ui.phoneTab === "agent" && <Composer variant="phone" state={ui.runId ? state : null} />}
        <TabList tabs={tabs} value={ui.phoneTab} onChange={ui.setPhoneTab} label="Sections" prefix="ph" className="pnav" />
      </div>
    </div>
  );
}
