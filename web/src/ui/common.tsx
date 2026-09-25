/** Small presentational pieces shared by both layouts. */

import type { ReactNode } from "react";
import type { RunState, Tier } from "../api/types";
import { runStateTone, type Tone } from "../lib/format";

const TIER_NAMES: Record<Tier, string> = {
  R: "Read",
  S: "Sandbox or draft",
  L: "Live, with a lease",
  C: "Commit, needs approval",
};

export function TierBadge({ tier }: { tier: Tier }) {
  return (
    <i className={`tier ${tier}`} title={`Tier ${tier}: ${TIER_NAMES[tier]}`} aria-label={`tier ${tier}`}>
      {tier}
    </i>
  );
}

export function Chip({ tone = "", dot, children, className = "", title }: {
  tone?: Tone["tone"];
  dot?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span className={`chip ${tone} ${className}`.trim()} title={title}>
      {dot && <i className={`dot ${tone === "acc" ? "ok" : tone || "off"}`} aria-hidden="true" />}
      {children}
    </span>
  );
}

/** `error`: the last turn ended with an error event. */
export function RunStateChip({
  state,
  reconnecting,
  error,
}: {
  state: RunState | null;
  reconnecting?: boolean;
  error?: boolean;
}) {
  if (reconnecting) {
    return (
      <Chip tone="warn" dot>
        Reconnecting…
      </Chip>
    );
  }
  const t = runStateTone(state, error);
  return (
    <Chip tone={t.tone} dot>
      {t.label}
    </Chip>
  );
}

export function Dot({ tone, label }: { tone: "ok" | "warn" | "off" | "crit"; label?: string }) {
  return <i className={`dot ${tone}`} role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}

const SIGN_IN_TEXT = "Sign-in needed: open the sign-in link your hub admin gave you once (it ends in ?token=…).";

/** An error for the user; a 401 from the hub is explained instead of quoted. */
export function ErrorNote({
  error,
  children,
}: {
  error?: { message: string; status?: number } | null;
  children?: ReactNode;
}) {
  if (!error && !children) return null;
  return (
    <p className="errnote" role="alert">
      {children ?? (error?.status === 401 ? SIGN_IN_TEXT : error?.message)}
    </p>
  );
}

const PATHS: Record<string, ReactNode> = {
  agent: (
    <>
      <path d="M4 5h16v11H9l-5 4z" />
      <path d="M8 9.5h8M8 12.5h5" />
    </>
  ),
  site: (
    <>
      <path d="M4 20V8l8-4 8 4v12z" />
      <path d="M9 20v-5h6v5M8 10h2M14 10h2" />
    </>
  ),
  changes: (
    <>
      <path d="M6 3h9l4 4v14H6z" />
      <path d="M9 12h7M12.5 8.5v7M9 17.5h7" />
    </>
  ),
  field: (
    <>
      <path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4" />
      <rect x="8" y="8" width="8" height="8" rx="1" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5l5 5" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  chevron: <path d="M6 9l6 6 6-6" />,
};

export function Icon({ name, size = 18 }: { name: keyof typeof PATHS; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      {PATHS[name]}
    </svg>
  );
}

export function SendIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M2 14l12-6L2 2v4.7L10 8l-8 1.3z" />
    </svg>
  );
}
