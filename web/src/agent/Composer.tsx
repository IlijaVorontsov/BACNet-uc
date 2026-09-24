import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { errorMessage } from "../api/client";
import type { RunState } from "../api/types";
import { useHub } from "../state/hub";
import { useScopeName, useUi } from "../state/ui";
import { ErrorNote, SendIcon } from "../ui/common";
import { PLAYBOOKS } from "./playbooks";

const BLOCKED: Partial<Record<RunState, string>> = {
  running: "The agent is working. Wait for it, or cancel the run.",
  waiting_approval: "Decide on the approval first.",
  waiting_answer: "Answer the question first.",
};

/** Message box: starts a new run, or continues the open one when it is idle. */
export function Composer({ variant, state }: { variant: "desktop" | "phone"; state: RunState | null }) {
  const hub = useHub();
  const ui = useUi();
  const scopeName = useScopeName(ui.scope);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement & HTMLInputElement>(null);
  const text = ui.draft.text;
  const blocked = ui.runId && state ? BLOCKED[state] : undefined;

  useEffect(() => {
    if (ui.focusComposer > 0) input.current?.focus();
  }, [ui.focusComposer]);

  const send = async (): Promise<void> => {
    const message = text.trim();
    if (!message || busy || blocked) return;
    setBusy(true);
    setError(null);
    try {
      if (ui.runId) {
        await hub.client.sendMessage(ui.runId, message);
      } else {
        const run = await hub.client.createRun(ui.draft.playbook ? { message, playbook: ui.draft.playbook } : { message });
        ui.selectRun(run.id);
      }
      ui.setDraft({ text: "" });
      hub.runs.reload();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (e: FormEvent): void => {
    e.preventDefault();
    void send();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const setText = (value: string): void => ui.setDraft({ ...ui.draft, text: value });
  const placeholder = ui.runId ? "Reply to the agent…" : "Ask the agent to start a new run…";
  const canSend = !!text.trim() && !busy && !blocked;

  if (variant === "phone") {
    return (
      <form className="pcomp" onSubmit={onSubmit}>
        <input
          ref={input}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={blocked ?? placeholder}
          aria-label="Message to the agent"
          enterKeyHint="send"
        />
        <button type="submit" className="send" aria-label="Send" disabled={!canSend}>
          <SendIcon />
        </button>
        {error && <p className="errnote pcomp-err" role="alert">{error}</p>}
      </form>
    );
  }

  return (
    <form className="composer" onSubmit={onSubmit}>
      <div className="pb" role="group" aria-label="Playbooks">
        {PLAYBOOKS.map((p) => (
          <button
            key={p.id}
            type="button"
            aria-pressed={ui.draft.playbook === p.id}
            onClick={() => {
              ui.selectRun(null);
              ui.askAgent(p.prompt({ scope: ui.scope, scopeName }), p.id);
            }}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="cbox">
        <textarea
          ref={input}
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          aria-label="Message to the agent"
        />
        <button type="submit" className="send" aria-label="Send" disabled={!canSend}>
          <SendIcon />
        </button>
      </div>
      {blocked && <p className="mute-t small">{blocked}</p>}
      <ErrorNote error={error ? { message: error } : null} />
    </form>
  );
}
