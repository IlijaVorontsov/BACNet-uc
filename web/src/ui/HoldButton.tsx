/**
 * Press-and-hold confirmation for tier C approvals on touch screens: a pocket
 * tap or a stray click must not apply a plan. Pointer or keyboard (Space,
 * Enter) must be held for `durationMs`; releasing early cancels.
 */

import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

export interface HoldButtonProps {
  label: string;
  holdingLabel?: string;
  doneLabel?: string;
  durationMs?: number;
  disabled?: boolean;
  /**
   * Runs when the hold completes. A promise resolving to false (or rejecting)
   * means the action failed: the button returns to idle so it can be held again.
   */
  onConfirm: () => void | Promise<boolean>;
  className?: string;
}

type Phase = "idle" | "holding" | "done";

export function HoldButton({
  label,
  holdingLabel = "Keep holding…",
  doneLabel = "Confirmed",
  durationMs = 1500,
  disabled = false,
  onConfirm,
  className = "",
}: HoldButtonProps) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [hint, setHint] = useState(false);
  const fill = useRef<HTMLElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const raf = useRef(0);
  const started = useRef(0);
  const phaseRef = useRef<Phase>("idle");
  const mounted = useRef(false);
  const onConfirmRef = useRef(onConfirm);
  onConfirmRef.current = onConfirm;
  const hintId = useId();

  const setFill = (p: number): void => {
    if (fill.current) fill.current.style.width = `${Math.round(p * 1000) / 10}%`;
  };

  const stopTimers = (): void => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    if (raf.current) cancelAnimationFrame(raf.current);
    raf.current = 0;
  };

  const setPhaseBoth = (p: Phase): void => {
    phaseRef.current = p;
    setPhase(p);
  };

  const start = useCallback(() => {
    if (disabled || phaseRef.current !== "idle") return;
    setHint(false);
    setPhaseBoth("holding");
    started.current = performance.now();
    const frame = (now: number): void => {
      setFill(Math.min(1, (now - started.current) / durationMs));
      raf.current = requestAnimationFrame(frame);
    };
    raf.current = requestAnimationFrame(frame);
    timer.current = setTimeout(() => {
      stopTimers();
      setFill(1);
      setPhaseBoth("done");
      const failed = (): void => {
        if (!mounted.current || phaseRef.current !== "done") return;
        setFill(0);
        setPhaseBoth("idle");
      };
      onConfirmRef.current()?.then((ok) => {
        if (!ok) failed();
      }, failed);
    }, durationMs);
  }, [disabled, durationMs]);

  const cancel = useCallback(() => {
    if (phaseRef.current !== "holding") return;
    stopTimers();
    setFill(0);
    setPhaseBoth("idle");
    setHint(true);
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stopTimers();
    };
  }, []);
  useEffect(() => {
    if (disabled) cancel();
  }, [disabled, cancel]);

  const onPointerDown = (e: PointerEvent<HTMLButtonElement>): void => {
    if (e.button !== 0) return;
    e.preventDefault();
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      // Synthetic events have no active pointer to capture.
    }
    start();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>): void => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    if (!e.repeat) start();
  };

  const onKeyUp = (e: KeyboardEvent<HTMLButtonElement>): void => {
    if (e.key !== " " && e.key !== "Enter") return;
    e.preventDefault();
    cancel();
  };

  const text = phase === "done" ? doneLabel : phase === "holding" ? holdingLabel : label;
  const seconds = (durationMs / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 });
  return (
    <div className="holdwrap">
      <button
        type="button"
        className={`hold ${phase} ${className}`.trim()}
        disabled={disabled || phase === "done"}
        aria-describedby={hintId}
        onPointerDown={onPointerDown}
        onPointerUp={cancel}
        onPointerCancel={cancel}
        onLostPointerCapture={cancel}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        onBlur={cancel}
        onContextMenu={(e) => e.preventDefault()}
      >
        <i ref={fill} aria-hidden="true" />
        <span>{text}</span>
      </button>
      <p id={hintId} className={`holdhint ${hint ? "show" : ""}`} aria-live="polite">
        {hint ? `Hold for ${seconds} s to confirm. A short tap does nothing.` : `Press and hold for ${seconds} s.`}
      </p>
    </div>
  );
}
