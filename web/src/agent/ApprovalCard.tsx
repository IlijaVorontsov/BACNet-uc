/**
 * Approval card in the agent stream. Tier C needs a deliberate gesture: on
 * desktop Approve only appears inside the expanded card, next to the diff;
 * on phones the card sends the user to the Changes tab, where the sheet asks
 * for a hold (see ApprovalSheet). Tier L (live and reversible, under a lease)
 * is decided in place, so an IO checkout on a phone stays in the stream.
 */

import { useState } from "react";
import type { Approval } from "../api/types";
import { formatClock, formatExpiry } from "../lib/format";
import { useDecide } from "../state/actions";
import { canApprove, useHub, usePlanWarnings } from "../state/hub";
import { useUi } from "../state/ui";
import { ErrorNote, TierBadge } from "../ui/common";
import { DiffView } from "../ui/DiffView";

export function DecisionLine({ approval }: { approval: Approval }) {
  if (approval.state === "approved") {
    return (
      <span className="done ok-t">
        Approved {approval.scope === "run" ? "for this run " : ""}by {approval.decided_by ?? "someone"} ·{" "}
        {formatClock(approval.decided_at)}
      </span>
    );
  }
  if (approval.state === "rejected") {
    return (
      <span className="mute-t">
        Rejected by {approval.decided_by ?? "someone"}
        {approval.comment ? `: ${approval.comment}` : ""}
      </span>
    );
  }
  if (approval.state === "expired") return <span className="mute-t">Expired without a decision.</span>;
  return null;
}

/** The desktop card's toggle names what it reveals: the diff, or the approve step and a comment. */
function toggleLabel(open: boolean, approval: Approval): string {
  if (approval.diff) return open ? "Hide diff" : "Review diff";
  if (open) return "Hide";
  return approval.tier === "C" ? "Review" : "Comment";
}

export function ApprovalCard({ approval, variant }: { approval: Approval; variant: "desktop" | "phone" }) {
  const { me } = useHub();
  const ui = useUi();
  const [open, setOpen] = useState(false);
  const [comment, setComment] = useState("");
  const { decide, busy, error, local } = useDecide(approval);
  const warnings = usePlanWarnings(local);
  const allowed = canApprove(me.data, local.tier);
  const pending = local.state === "pending";
  const needsExpand = local.tier === "C";

  return (
    <section className={`appr tier-${local.tier} ${local.state}`} aria-label={`Approval: ${local.title}`}>
      <h4>
        <TierBadge tier={local.tier} />
        {local.title}
      </h4>
      {local.summary.length > 0 && (
        <ul>
          {local.summary.map((s, i) => (
            <li key={i} className={warnings.has(s) ? "warn-t" : undefined}>
              {s}
            </li>
          ))}
        </ul>
      )}
      <div className="rollback">
        {local.rollback ? `Rollback: ${local.rollback}` : "No automatic rollback"}
        {pending && ` · ${formatExpiry(local.expires_at)}`}
      </div>
      {!pending && <DecisionLine approval={local} />}
      {pending && !allowed && <p className="mute-t small">Your role cannot approve tier {local.tier} changes.</p>}
      {pending && variant === "phone" && (
        <div className="row">
          {needsExpand ? (
            <button type="button" className="btn primary" onClick={() => ui.setPhoneTab("changes")}>
              Review in Changes
            </button>
          ) : (
            <>
              <button type="button" className="btn primary" disabled={busy || !allowed} onClick={() => void decide("approve")}>
                Approve
              </button>
              <button type="button" className="btn" disabled={busy || !allowed} onClick={() => void decide("approve", undefined, "run")}>
                Approve for this run
              </button>
              <button type="button" className="btn danger" disabled={busy || !allowed} onClick={() => void decide("reject")}>
                Reject
              </button>
            </>
          )}
        </div>
      )}
      {pending && variant === "desktop" && (
        <>
          {open && (
            <div className="apprbody">
              {local.diff && <DiffView diff={local.diff} label={`Diff of ${local.title}`} />}
              <label className="field">
                <span>Comment for the agent (optional)</span>
                <input value={comment} onChange={(e) => setComment(e.target.value)} disabled={busy} />
              </label>
            </div>
          )}
          <div className="row">
            {(open || !needsExpand) && (
              <button type="button" className="btn primary" disabled={busy || !allowed} onClick={() => void decide("approve", comment.trim() || undefined)}>
                {local.tool === "apply" ? "Approve and apply" : "Approve"}
              </button>
            )}
            {local.tier === "L" && (
              <button
                type="button"
                className="btn"
                title="Also approves the later live (tier L) calls of this run; commits still ask."
                disabled={busy || !allowed}
                onClick={() => void decide("approve", comment.trim() || undefined, "run")}
              >
                Approve for this run
              </button>
            )}
            <button type="button" className="btn" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {toggleLabel(open, local)}
            </button>
            {local.plan_id && (
              <button type="button" className="btn link" onClick={() => ui.setWorkTab("changes")}>
                Open in Changes
              </button>
            )}
            <button type="button" className="btn danger" disabled={busy || !allowed} onClick={() => void decide("reject", comment.trim() || undefined)}>
              Reject
            </button>
          </div>
        </>
      )}
      <ErrorNote error={error ? { message: error } : null} />
    </section>
  );
}
