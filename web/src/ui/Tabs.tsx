/** WAI-ARIA tabs: arrow keys, Home and End move between tabs (automatic activation). */

import { useRef, type KeyboardEvent, type ReactNode } from "react";

export interface TabSpec<T extends string> {
  id: T;
  label: ReactNode;
  /** Accessible name when `label` is not plain text. */
  name?: string;
  disabled?: boolean;
}

function tabIds(prefix: string, id: string): { tab: string; panel: string } {
  return { tab: `${prefix}-tab-${id}`, panel: `${prefix}-panel-${id}` };
}

export function TabList<T extends string>({ tabs, value, onChange, label, prefix, className }: {
  tabs: readonly TabSpec<T>[];
  value: T;
  onChange: (id: T) => void;
  label: string;
  prefix: string;
  className?: string;
}) {
  const refs = useRef(new Map<T, HTMLButtonElement>());
  const enabled = tabs.filter((t) => !t.disabled);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>): void => {
    const i = enabled.findIndex((t) => t.id === value);
    let next: TabSpec<T> | undefined;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = enabled[(i + 1) % enabled.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = enabled[(i - 1 + enabled.length) % enabled.length];
    else if (e.key === "Home") next = enabled[0];
    else if (e.key === "End") next = enabled[enabled.length - 1];
    if (!next) return;
    e.preventDefault();
    onChange(next.id);
    refs.current.get(next.id)?.focus();
  };

  return (
    <div role="tablist" aria-label={label} className={className} onKeyDown={onKeyDown}>
      {tabs.map((t) => {
        const ids = tabIds(prefix, t.id);
        const selected = t.id === value;
        return (
          <button
            key={t.id}
            ref={(el) => {
              if (el) refs.current.set(t.id, el);
              else refs.current.delete(t.id);
            }}
            type="button"
            role="tab"
            id={ids.tab}
            aria-controls={ids.panel}
            aria-selected={selected}
            aria-label={t.name}
            tabIndex={selected ? 0 : -1}
            disabled={t.disabled}
            onClick={() => onChange(t.id)}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ prefix, id, className, children }: { prefix: string; id: string; className?: string; children: ReactNode }) {
  const ids = tabIds(prefix, id);
  return (
    <div role="tabpanel" id={ids.panel} aria-labelledby={ids.tab} className={className} tabIndex={0}>
      {children}
    </div>
  );
}
