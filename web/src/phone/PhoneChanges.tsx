import { useState } from "react";
import type { Approval } from "../api/types";
import { plural } from "../lib/format";
import { PlanNotes } from "../panes/ChangesPane";
import { useHub } from "../state/hub";
import { useUi } from "../state/ui";
import { Chip, ErrorNote } from "../ui/common";
import { DiffView } from "../ui/DiffView";
import { ApprovalSheet } from "./ApprovalSheet";

export function PhoneChanges() {
  const hub = useHub();
  const ui = useUi();
  const [recent, setRecent] = useState<Approval[]>([]);
  const [showPlan, setShowPlan] = useState(false);
  const pending = hub.approvals.data?.approvals ?? [];
  const shown = [...recent.filter((r) => !pending.some((p) => p.id === r.id)), ...pending];
  const plan = hub.plan.data?.plan ?? null;
  const live = hub.manifest.data?.live_revision;

  return (
    <div className="pstack">
      <ErrorNote error={hub.approvals.data ? null : hub.approvals.error} />
      {shown.map((a) => (
        <ApprovalSheet
          key={a.id}
          approval={a}
          onDecided={(d) => {
            setRecent((prev) => [d, ...prev.filter((x) => x.id !== d.id)]);
            // Follow the run that continues, so its progress and plan updates arrive.
            if (d.state === "approved") ui.selectRun(d.run_id);
          }}
        />
      ))}
      {hub.plan.data &&
        (plan ? (
          <section className="runcard" aria-label="Draft plan">
            <div className="top">
              <b>
                Plan {plan.id} · revision {plan.revision}
              </b>
              <span>{plural(plan.changes.length, "change")}</span>
            </div>
            <PlanNotes plan={plan} />
            <ul className="clist">
              {plan.changes.map((c) => (
                <li key={c.id}>
                  <i className={`tier ${c.tier}`}>{c.tier}</i>
                  <span>
                    <span className="mono">{c.target}</span> {c.summary}
                  </span>
                  <span className="st mute-t">{c.kind}</span>
                </li>
              ))}
            </ul>
            {pending.length === 0 && (
              <p className="mute-t small">
                {Object.keys(plan.blocked).length > 0
                  ? "This plan cannot be applied. Plan again once every target answers."
                  : "No approval is requested yet. Ask the agent to apply the plan."}
              </p>
            )}
            <button type="button" className="btn" aria-expanded={showPlan} onClick={() => setShowPlan((s) => !s)}>
              {showPlan ? "Hide diffs" : "Show all diffs"}
            </button>
            {showPlan && plan.changes.filter((c) => c.diff).map((c) => <DiffView key={c.id} diff={c.diff} label={c.summary} />)}
          </section>
        ) : (
          <section className="runcard" aria-label="Draft plan">
            <div className="top">
              <b>No pending changes</b>
              {live !== undefined && <Chip tone="ok">Revision {live} live</Chip>}
            </div>
            <p className="mute-t small">The draft matches the live site.</p>
          </section>
        ))}
    </div>
  );
}
