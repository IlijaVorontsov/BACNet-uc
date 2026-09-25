import type { Change, Plan } from "../api/types";
import { formatClock, plural } from "../lib/format";
import { PROTOCOL_LABELS } from "../lib/site";
import { useHub } from "../state/hub";
import { useUi } from "../state/ui";
import { Chip, ErrorNote, TierBadge } from "../ui/common";
import { DiffView } from "../ui/DiffView";

function groupByTarget(plan: Plan): [string, Change[]][] {
  const out = new Map<string, Change[]>();
  for (const t of plan.targets) out.set(t, []);
  for (const c of plan.changes) {
    const list = out.get(c.target) ?? [];
    list.push(c);
    out.set(c.target, list);
  }
  return [...out.entries()].filter(([, list]) => list.length > 0);
}

/** Targets that keep the plan from being applied, and the plan's warnings. */
export function PlanNotes({ plan }: { plan: Plan }) {
  const blocked = Object.entries(plan.blocked);
  return (
    <>
      {blocked.length > 0 && (
        <ul className="warnings" aria-label="Blocked targets">
          {blocked.map(([target, reason]) => (
            <li key={target}>
              <span className="warn-t">Cannot be applied:</span> {target} could not be planned ({reason})
            </li>
          ))}
        </ul>
      )}
      {plan.warnings.length > 0 && (
        <ul className="warnings" aria-label="Warnings">
          {plan.warnings.map((w, i) => (
            <li key={i}>
              <span className="warn-t">Warning:</span> {w}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** The draft's plan: per-target changes with their diffs, warnings and the pending approval. */
export function ChangesPane() {
  const hub = useHub();
  const ui = useUi();
  const plan = hub.plan.data?.plan ?? null;
  const live = hub.manifest.data?.live_revision;
  const devices = hub.site.data?.devices ?? [];
  const approval = plan ? hub.approvals.data?.approvals.find((a) => a.plan_id === plan.id && a.state === "pending") : undefined;

  if (hub.plan.error && !hub.plan.data) return <ErrorNote error={hub.plan.error} />;
  if (!hub.plan.data) return <p className="mute-t">Loading the plan…</p>;

  if (!plan) {
    return (
      <div className="plan">
        <div className="plan-head">
          <h3>No pending changes</h3>
          {live !== undefined && <Chip tone="ok">Revision {live} live</Chip>}
        </div>
        <p className="rollback">
          The draft matches the live site. When the agent edits the manifest, the plan to review appears here.
        </p>
      </div>
    );
  }

  const blocked = Object.keys(plan.blocked).length > 0;
  const needsApproval = !blocked && plan.changes.some((c) => c.tier === "C");
  return (
    <div className="plan">
      <div className="plan-head">
        <h3>
          Plan {plan.id} · draft revision {plan.revision}
        </h3>
        <Chip>{plural(plan.changes.length, "change")} on {plural(plan.targets.length, "target")}</Chip>
        {blocked && <Chip tone="crit">Cannot be applied</Chip>}
        {needsApproval && (
          <Chip tone="crit">
            <TierBadge tier="C" />
            Needs approval
          </Chip>
        )}
      </div>
      {approval && (
        <div className="pendingnote">
          <span>
            Approval requested by {approval.requested_by} at {formatClock(approval.requested_at)}.
          </span>
          <button type="button" className="btn" onClick={() => ui.selectRun(approval.run_id)}>
            Review in the agent panel
          </button>
        </div>
      )}
      <PlanNotes plan={plan} />
      {groupByTarget(plan).map(([target, changes]) => {
        const d = devices.find((x) => x.name === target);
        return (
          <section key={target} className="target" aria-label={`Changes on ${target}`}>
            <header>
              <b>{target === "gateway" ? "uc-hub gateway" : target}</b>
              <span className="mute-t">{d ? `${PROTOCOL_LABELS[d.protocol]} · ${d.address}` : target === "gateway" ? "bridges, tags" : ""}</span>
              <Chip>{plural(changes.length, "change")}</Chip>
            </header>
            {changes.map((c) => (
              <div key={c.id} className="change">
                <div className="chead">
                  <TierBadge tier={c.tier} />
                  <span>{c.summary}</span>
                  <span className="tag">{c.kind}</span>
                </div>
                {c.diff && <DiffView diff={c.diff} label={`Diff of ${c.summary}`} />}
              </div>
            ))}
          </section>
        );
      })}
      <p className="rollback">Rollback: apply revision {plan.base_revision}.</p>
    </div>
  );
}
