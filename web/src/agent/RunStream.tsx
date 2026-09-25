import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useHub } from "../state/hub";
import { acceptsAnswers, type RunView, type ToolItem } from "../state/runReducer";
import { ApprovalCard } from "./ApprovalCard";
import { AssistantMessage, ErrorLine, Notice, ThinkingBlock, ToolCard, UserMessage } from "./items";
import { QuestionCard } from "./QuestionCard";

/** Screen-reader announcements for what needs the user, not for every streamed token. */
function useAnnouncement(view: RunView): string {
  const [text, setText] = useState("");
  const last = useRef<string>("");
  useEffect(() => {
    let msg = "";
    if (view.state === "waiting_approval") {
      const a = [...view.items].reverse().find((i) => i.kind === "approval");
      msg = a && a.kind === "approval" ? `The agent asks for approval: ${a.approval.title}` : "The agent waits for an approval.";
    } else if (view.state === "waiting_answer") {
      const q = [...view.items].reverse().find((i) => i.kind === "question" && i.answer === null);
      msg = q && q.kind === "question" ? `The agent asks: ${q.text}` : "The agent waits for an answer.";
    } else if (view.state === "idle") {
      msg = view.turnError ? `The agent stopped: ${view.turnError}` : "The agent finished.";
    } else if (view.state === "failed") {
      msg = "The run failed.";
    } else if (view.state === "cancelled") {
      msg = "The run was cancelled.";
    }
    if (msg && msg !== last.current) {
      last.current = msg;
      setText(msg);
    }
  }, [view.state, view.items, view.turnError]);
  return text;
}

export function RunStream({ view, runId, variant }: { view: RunView; runId: string; variant: "desktop" | "phone" }) {
  const { me } = useHub();
  const ref = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const announcement = useAnnouncement(view);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [view.items]);

  const onScroll = (): void => {
    const el = ref.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const questionsOpen = acceptsAnswers(view.state);
  const blocks: ReactNode[] = [];
  let group: ToolItem[] = [];
  const flush = (): void => {
    if (group.length === 0) return;
    blocks.push(
      <div key={`g-${group[0]!.key}`} className="toolgroup">
        {group.map((t) => (
          <ToolCard key={t.key} item={t} />
        ))}
      </div>,
    );
    group = [];
  };
  for (const it of view.items) {
    if (it.kind === "tool") {
      // ask_user calls are shown as their question card instead.
      if (it.questionId === null) group.push(it);
      continue;
    }
    flush();
    switch (it.kind) {
      case "user":
        blocks.push(<UserMessage key={it.key} item={it} me={me.data?.user ?? null} />);
        break;
      case "assistant":
        blocks.push(<AssistantMessage key={it.key} item={it} />);
        break;
      case "thinking":
        blocks.push(<ThinkingBlock key={it.key} item={it} />);
        break;
      case "approval":
        blocks.push(<ApprovalCard key={it.key} approval={it.approval} variant={variant} />);
        break;
      case "question":
        blocks.push(<QuestionCard key={it.key} item={it} runId={runId} active={questionsOpen} />);
        break;
      case "error":
        blocks.push(<ErrorLine key={it.key} item={it} />);
        break;
      case "notice":
        blocks.push(<Notice key={it.key} item={it} />);
        break;
    }
  }
  flush();

  return (
    <div className="stream" ref={ref} onScroll={onScroll} aria-label="Agent conversation" data-testid="run-stream">
      {blocks.length === 0 && <p className="mute-t small">Loading the run…</p>}
      {blocks}
      <div className="sr-only" aria-live="polite">
        {announcement}
      </div>
    </div>
  );
}
