/** Stream items other than approvals and questions. */

import { useState } from "react";
import { formatDuration } from "../lib/format";
import { Markdown } from "../lib/markdown";
import type { AssistantItem, ErrorItem, NoticeItem, ThinkingItem, ToolItem, UserItem } from "../state/runReducer";
import { TierBadge } from "../ui/common";
import { argSummary } from "./argSummary";

export function UserMessage({ item, me }: { item: UserItem; me: string | null }) {
  return (
    <div className="msg-user">
      {me !== null && item.user !== me && <span className="who">{item.user}</span>}
      {item.text}
    </div>
  );
}

export function AssistantMessage({ item }: { item: AssistantItem }) {
  return (
    <div className={`msg-ai${item.streaming ? " streaming" : ""}`}>
      <Markdown text={item.text} />
    </div>
  );
}

export function ThinkingBlock({ item }: { item: ThinkingItem }) {
  const [open, setOpen] = useState(false);
  const secs = Math.max(1, Math.round(item.endTs - item.ts));
  return (
    <div className="thinkwrap">
      <button type="button" className="think" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        {item.streaming ? "Thinking…" : `Reasoned for ${secs} s`}
      </button>
      {open && <p className="think-body">{item.text}</p>}
    </div>
  );
}

function pretty(v: unknown): string {
  try {
    return JSON.stringify(v, null, 2) ?? String(v);
  } catch {
    return String(v);
  }
}

export function ToolCard({ item }: { item: ToolItem }) {
  const [open, setOpen] = useState(false);
  const running = item.status === "running";
  const arg = argSummary(item.args, item.argsText);
  let dur = "";
  if (item.result) dur = formatDuration(item.result.durationMs);
  else if (item.status === "approval") dur = "needs approval";
  else if (item.status === "question") dur = "waiting for answer";
  const hasArgs = Object.keys(item.args).length > 0;
  return (
    <div className={`tool ${item.status}`}>
      <TierBadge tier={item.tier} />
      <button type="button" className="fn" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {item.tool || "tool"} <span className="arg">{arg}</span>
      </button>
      <span className="dur">
        {dur}
        {running && <span className="spin" role="img" aria-label="running" />}
      </span>
      {item.result ? (
        <div className={`res${item.result.ok ? "" : " crit-t"}`}>{item.result.summary}</div>
      ) : running ? (
        <div className="res mute-t">{item.argsComplete || hasArgs || !item.argsText ? "Running…" : "Writing arguments…"}</div>
      ) : null}
      {open && (
        <div className="tooldetail">
          <div className="label-sm">Arguments</div>
          <pre className="code small">{hasArgs ? pretty(item.args) : item.argsText || "{}"}</pre>
          {item.result?.data !== undefined && (
            <>
              <div className="label-sm">Result</div>
              <pre className="code small">{pretty(item.result.data)}</pre>
            </>
          )}
          {item.result?.handle && (
            <div className="small mute-t">
              Full result: <span className="mono">{item.result.handle}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ErrorLine({ item }: { item: ErrorItem }) {
  return (
    <div className="msg-err" role="alert">
      <b>Error{item.code ? ` (${item.code})` : ""}:</b> {item.message}
    </div>
  );
}

export function Notice({ item }: { item: NoticeItem }) {
  return (
    <div className={`notice ${item.state}`}>
      Run {item.state}
      {item.reason ? `: ${item.reason}` : ""}
    </div>
  );
}
