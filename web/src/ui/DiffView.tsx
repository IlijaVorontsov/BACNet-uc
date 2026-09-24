import { useMemo } from "react";
import { parseUnifiedDiff } from "../lib/diff";

const SIGN = { add: "+", del: "−", ctx: " ", hunk: "", meta: "" } as const;

/** A unified diff as coloured lines, with the +/− sign in the gutter. */
export function DiffView({ diff, label }: { diff: string; label?: string }) {
  const lines = useMemo(() => parseUnifiedDiff(diff), [diff]);
  if (lines.length === 0) return <p className="mute-t small">No diff for this change.</p>;
  return (
    <pre className="code diff" aria-label={label}>
      {lines.map((l, i) => (
        <span key={i} className={`ln ${l.kind}`} data-n={SIGN[l.kind]}>
          {l.kind === "add" && <span className="sr-only">added: </span>}
          {l.kind === "del" && <span className="sr-only">removed: </span>}
          {l.text || " "}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}
