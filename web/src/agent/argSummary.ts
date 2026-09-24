/** One-line summaries of tool arguments for the collapsed tool card. */

const ARRAY_NOUNS: Record<string, [string, string]> = {
  json_patch: ["JSON-patch op", "JSON-patch ops"],
  patch: ["JSON-patch op", "JSON-patch ops"],
  cases: ["case", "cases"],
  points: ["point", "points"],
  ids: ["point", "points"],
  tests: ["test", "tests"],
  options: ["option", "options"],
};

const MAX_PARTS = 4;
const MAX_TEXT = 48;

function short(s: string): string {
  return s.length > MAX_TEXT ? `${s.slice(0, MAX_TEXT - 1)}…` : s;
}

export function argSummary(args: Record<string, unknown>, argsText = ""): string {
  const entries = Object.entries(args);
  if (entries.length === 0) {
    const t = argsText.replace(/\s+/g, " ").trim();
    if (t === "{}") return "";
    return t.length > MAX_TEXT ? `…${t.slice(-(MAX_TEXT - 1))}` : t;
  }
  const parts: string[] = [];
  for (const [k, v] of entries) {
    if (parts.length >= MAX_PARTS) break;
    if (typeof v === "string") {
      if (!v) continue;
      parts.push(v.includes("\n") ? `${v.split("\n").length} lines` : short(v));
    } else if (typeof v === "number" || typeof v === "boolean") {
      parts.push(k === "lease_s" ? `lease ${v} s` : k === "seconds" ? `${v} s` : `${k} ${String(v)}`);
    } else if (Array.isArray(v)) {
      if (v.length === 1 && typeof v[0] === "string" && v[0].length <= MAX_TEXT) {
        parts.push(v[0]);
      } else if (v.length === 0 && k === "tests") {
        parts.push("all tests");
      } else {
        const noun = ARRAY_NOUNS[k];
        parts.push(`${v.length} ${noun ? noun[v.length === 1 ? 0 : 1] : k}`);
      }
    } else if (v !== null && typeof v === "object") {
      parts.push(`${k} {…}`);
    }
  }
  return parts.join(" · ");
}
