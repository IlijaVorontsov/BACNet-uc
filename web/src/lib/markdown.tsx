/**
 * A deliberately small Markdown subset for assistant messages: paragraphs,
 * "- " lists, `code` and **bold**. It builds React elements (never HTML
 * strings), so model output cannot inject markup.
 */

import { Fragment, type ReactNode } from "react";

const INLINE_RE = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;

function renderInline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const tok = m[0];
    if (tok.startsWith("`")) out.push(<code key={i++} className="mono">{tok.slice(1, -1)}</code>);
    else out.push(<strong key={i++}>{tok.slice(2, -2)}</strong>);
    last = start + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const LIST_ITEM = /^\s*[-*] /;

export function Markdown({ text }: { text: string }): ReactNode {
  const blocks = text.replace(/\r\n?/g, "\n").split(/\n{2,}/).filter((b) => b.trim() !== "");
  return (
    <>
      {blocks.map((block, bi) => {
        const lines = block.split("\n");
        if (lines.every((l) => LIST_ITEM.test(l))) {
          return (
            <ul key={bi}>
              {lines.map((l, li) => (
                <li key={li}>{renderInline(l.replace(LIST_ITEM, ""))}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={bi}>
            {lines.map((l, li) => (
              <Fragment key={li}>
                {li > 0 && <br />}
                {renderInline(l)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </>
  );
}
