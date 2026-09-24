import type { Scope } from "../state/ui";

export interface Playbook {
  id: string;
  label: string;
  prompt: (ctx: { scope: Scope; scopeName: string | null }) => string;
}

/** Playbook ids are sent as `POST /api/runs {"playbook"}` (DESIGN.md section 8). */
export const PLAYBOOKS: readonly Playbook[] = [
  { id: "onboard", label: "Onboard devices", prompt: () => "Onboard the devices found by discovery." },
  {
    id: "io-checkout",
    label: "IO checkout",
    prompt: ({ scope }) => (scope.kind === "device" ? `Run IO checkout for ${scope.name}.` : "Run IO checkout for r204-ctl."),
  },
  {
    id: "troubleshoot",
    label: "Troubleshoot",
    prompt: ({ scope, scopeName }) =>
      scope.kind !== "site" && scopeName ? `Troubleshoot ${scopeName}: ` : "Why is room 205 warmer than its setpoint?",
  },
];
