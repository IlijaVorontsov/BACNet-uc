import { useState } from "react";
import type { Approval } from "../api/types";
import { formatClock, formatExpiry } from "../lib/format";
import { canApprove, useHub } from "../state/hub";
import { DecisionLine } from "../agent/ApprovalCard";
import { useDecide } from "../state/actions";
import { Dot, ErrorNote, TierBadge } from "../ui/common";
import { DiffView } from "../ui/DiffView";
import { HoldButton } from "../ui/HoldButton";

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function splitLine(line: string): [string, string] {
  const i = line.indexOf(": ");
  return i > 0 && i < 32 ? [line.slice(0, i), line.slice(i + 2)] : [line, ""];
}

/** Phone approval: per-target summary, the diff one tap away, and a hold for tier C. */
export function ApprovalSheet({ approval, onDecided }: { approval: Approval; onDecided: (a: Approval) => void }) {
  const { me } = useHub();
  const [showDiff, setShowDiff] = useState(false);
  const { decide, busy, error, local } = useDecide(approval);
  const allowed = canApprove(me.data, local.tier);
  const pending = local.state === "pending";
  const verb = local.tool === "apply" ? "apply" : "approve";

  /** Resolves to false when the request failed, so the hold can be repeated. */
  const run = async (decision: "approve" | "reject"): Promise<boolean> => {
    const res = await decide(decision);
    if (res) onDecided(res);
    return res !== null;
  };

  return (
    <section className={`sheet ${local.state}`} aria-label={`Approval: ${local.title}`}>
      <h4>
        <TierBadge tier={local.tier} />
        {local.title}
      </h4>
      <ul className="tl">
        {local.summary.map((line, i) => {
          const [head, rest] = splitLine(line);
          return (
            <li key={i}>
              <Dot tone="ok" />
              <span>{head}</span>
              {rest && <small>{rest}</small>}
            </li>
          );
        })}
      </ul>
      {local.diff && (
        <button type="button" className="btn" aria-expanded={showDiff} onClick={() => setShowDiff((s) => !s)}>
          {showDiff ? "Hide the diff" : "Show full diff"}
        </button>
      )}
      {showDiff && <DiffView diff={local.diff} label={`Diff of ${local.title}`} />}
      {pending ? (
        <>
          {!allowed && <p className="mute-t small">Your role cannot approve tier {local.tier} changes.</p>}
          {local.tier === "C" ? (
            <HoldButton
              label={`Hold to ${verb}`}
              doneLabel={verb === "apply" ? "Applying…" : "Approving…"}
              disabled={!allowed || busy}
              onConfirm={() => run("approve")}
            />
          ) : (
            <button type="button" className="btn primary big" disabled={!allowed || busy} onClick={() => void run("approve")}>
              Approve
            </button>
          )}
          <button type="button" className="btn danger big" disabled={!allowed || busy} onClick={() => void run("reject")}>
            Reject
          </button>
        </>
      ) : (
        <div className={`decided ${local.state}`}>
          <DecisionLine approval={local} />
          {local.state === "approved" && <div className="small mute-t">The agent continues on the Agent tab.</div>}
        </div>
      )}
      <p className="mute-t small">
        Requested by {local.requested_by} at {formatClock(local.requested_at)}.{pending ? ` ${capitalize(formatExpiry(local.expires_at))}.` : ""}
        {local.rollback ? ` Rollback: ${local.rollback}.` : ""}
      </p>
      <ErrorNote error={error ? { message: error } : null} />
    </section>
  );
}
