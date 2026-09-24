/** Hooks for the user actions that call the hub: decide an approval, cancel a run, identify a device. */

import { useState } from "react";
import { errorMessage } from "../api/client";
import type { Approval, Device } from "../api/types";
import { useHub } from "./hub";

export function useDecide(approval: Approval): {
  /** Resolves to the decided approval, or null when the request failed. */
  decide: (decision: "approve" | "reject", comment?: string) => Promise<Approval | null>;
  busy: boolean;
  error: string | null;
  local: Approval;
} {
  const { client, approvals } = useHub();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [override, setOverride] = useState<Approval | null>(null);
  const local = override && override.id === approval.id && approval.state === "pending" ? override : approval;
  const decide = async (decision: "approve" | "reject", comment?: string): Promise<Approval | null> => {
    setBusy(true);
    setError(null);
    try {
      const res = await client.decide(approval.id, comment ? { decision, comment } : { decision });
      setOverride(res);
      return res;
    } catch (err) {
      setError(errorMessage(err));
      return null;
    } finally {
      setBusy(false);
      approvals.reload();
    }
  };
  return { decide, busy, error, local };
}

export function useCancel(runId: string | null): { cancel: () => void; busy: boolean; error: string | null } {
  const { client, runs } = useHub();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancel = (): void => {
    if (!runId) return;
    setBusy(true);
    setError(null);
    client
      .cancelRun(runId)
      .then(() => runs.reload())
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setBusy(false));
  };
  return { cancel, busy, error };
}

export function useIdentify(): { identify: (d: Device, seconds?: number) => Promise<boolean>; busy: string | null; error: string | null } {
  const { client } = useHub();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const identify = async (d: Device, seconds = 30): Promise<boolean> => {
    setBusy(d.name);
    setError(null);
    try {
      await client.identify(d.name, seconds);
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy(null);
    }
  };
  return { identify, busy, error };
}
