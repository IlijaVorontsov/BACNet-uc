import { useState, type FormEvent } from "react";
import { errorMessage } from "../api/client";
import { useHub } from "../state/hub";
import type { QuestionItem } from "../state/runReducer";
import { ErrorNote } from "../ui/common";

const POSITIVE = /^(yes|ok|pass|passed|done|confirm)/i;

/** An `ask_user` question: options as buttons (big on phones), or free text when there are none. */
export function QuestionCard({ item, runId, active }: { item: QuestionItem; runId: string; active: boolean }) {
  const { client } = useHub();
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const answered = item.answer ?? null;
  const open = answered === null && sent === null && active;

  const answer = async (value: string): Promise<void> => {
    if (!value.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await client.answer(runId, item.questionId, value.trim());
      setSent(value.trim());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (e: FormEvent): void => {
    e.preventDefault();
    void answer(text);
  };

  return (
    <section className={`ask${answered !== null ? " answered" : ""}`} aria-label="Question from the agent">
      <p>{item.text}</p>
      {answered !== null || sent !== null ? (
        <div className="answer">
          <span className="mute-t">Answered</span> <b>{answered ?? sent}</b>
          {item.answeredBy && <span className="mute-t"> · {item.answeredBy}</span>}
        </div>
      ) : !active ? (
        <div className="mute-t small">No longer waiting for an answer.</div>
      ) : item.options.length > 0 ? (
        <div className="opts" role="group" aria-label="Answers">
          {item.options.map((o, i) => (
            <button
              key={o}
              type="button"
              className={i === 0 && POSITIVE.test(o) ? "yes" : ""}
              disabled={busy || !open}
              onClick={() => void answer(o)}
            >
              {o}
            </button>
          ))}
        </div>
      ) : (
        <form className="freeform" onSubmit={onSubmit}>
          <input aria-label="Your answer" value={text} onChange={(e) => setText(e.target.value)} disabled={busy} />
          <button type="submit" className="btn primary" disabled={busy || !text.trim()}>
            Answer
          </button>
        </form>
      )}
      <ErrorNote error={error ? { message: error } : null} />
    </section>
  );
}
