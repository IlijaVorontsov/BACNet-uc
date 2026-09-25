import type { Scope } from "../state/ui";

export interface Playbook {
  id: string;
  label: string;
  /** What the playbook does, for the phone's start screen. */
  hint: string;
  /**
   * The draft for the composer. It names only what is in scope; otherwise it
   * ends open ("Run IO checkout for ") so the user completes it.
   */
  prompt: (ctx: { scope: Scope; scopeName: string | null }) => string;
}

/** Playbook ids are sent as `POST /api/runs {"playbook"}` (DESIGN.md section 8). */
export const PLAYBOOKS: readonly Playbook[] = [
  {
    id: "onboard",
    label: "Onboard devices",
    hint: "Place and map the devices found by discovery.",
    prompt: () => "Onboard the devices found by discovery.",
  },
  {
    id: "io-checkout",
    label: "IO checkout",
    hint: "Check a board's inputs and outputs with you on site.",
    prompt: ({ scope }) => (scope.kind === "device" ? `Run IO checkout for ${scope.name}.` : "Run IO checkout for "),
  },
  {
    id: "troubleshoot",
    label: "Troubleshoot",
    hint: "Find out why a room or device misbehaves.",
    prompt: ({ scopeName }) => (scopeName ? `Troubleshoot ${scopeName}: ` : "Troubleshoot: "),
  },
];
