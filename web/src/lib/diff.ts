/**
 * Unified diff parsing for display. Plans carry per-change diffs
 * (`Change.diff`, `Approval.diff`); some are real unified diffs, others a
 * short before/after in the same +/- notation, so hunk headers are optional.
 */

export type DiffLineKind = "meta" | "hunk" | "add" | "del" | "ctx";

export interface DiffLine {
  kind: DiffLineKind;
  /** The line without its +/-/space prefix (meta and hunk lines are kept whole). */
  text: string;
  oldNo: number | null;
  newNo: number | null;
}

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

export function parseUnifiedDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldNo: number | null = null;
  let newNo: number | null = null;
  let inHunk = false;
  const lines = diff.replace(/\r\n?/g, "\n").split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  for (const line of lines) {
    const hunk = HUNK_RE.exec(line);
    if (line.startsWith("@@")) {
      inHunk = true;
      oldNo = hunk ? Number(hunk[1]) : null;
      newNo = hunk ? Number(hunk[2]) : null;
      out.push({ kind: "hunk", text: line, oldNo: null, newNo: null });
      continue;
    }
    if (
      (!inHunk && (line.startsWith("--- ") || line.startsWith("+++ "))) ||
      line.startsWith("diff ") ||
      line.startsWith("index ") ||
      line.startsWith("\\")
    ) {
      out.push({ kind: "meta", text: line, oldNo: null, newNo: null });
      continue;
    }
    const sign = line[0];
    if (sign === "+") {
      out.push({ kind: "add", text: line.slice(1), oldNo: null, newNo });
      if (newNo !== null) newNo++;
    } else if (sign === "-") {
      out.push({ kind: "del", text: line.slice(1), oldNo, newNo: null });
      if (oldNo !== null) oldNo++;
    } else {
      out.push({ kind: "ctx", text: sign === " " ? line.slice(1) : line, oldNo, newNo });
      if (oldNo !== null) oldNo++;
      if (newNo !== null) newNo++;
    }
  }
  return out;
}

export function diffStats(lines: readonly DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const l of lines) {
    if (l.kind === "add") added++;
    else if (l.kind === "del") removed++;
  }
  return { added, removed };
}
