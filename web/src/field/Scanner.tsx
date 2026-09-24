/**
 * QR label scanner for the Field tab. Uses the Shape Detection API
 * (`BarcodeDetector`) with the rear camera where the browser has it, and
 * otherwise (or when the camera is refused) a manual device-id entry.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";

interface DetectedBarcode {
  rawValue: string;
}

interface BarcodeDetectorLike {
  detect(source: CanvasImageSource): Promise<DetectedBarcode[]>;
}

type BarcodeDetectorCtor = new (opts?: { formats?: string[] }) => BarcodeDetectorLike;

function detectorCtor(): BarcodeDetectorCtor | null {
  const ctor = (globalThis as { BarcodeDetector?: BarcodeDetectorCtor }).BarcodeDetector;
  return typeof ctor === "function" && typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia ? ctor : null;
}

function ManualEntry({ onSubmit, autoFocus = false }: { onSubmit: (text: string) => void; autoFocus?: boolean }) {
  const [text, setText] = useState("");
  const submit = (e: FormEvent): void => {
    e.preventDefault();
    if (text.trim()) onSubmit(text.trim());
  };
  return (
    <form className="manual" onSubmit={submit}>
      <label className="field">
        <span>Device id from the label</span>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="r204-ctl, 2041 or the label text"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          autoFocus={autoFocus}
        />
      </label>
      <button type="submit" className="btn primary" disabled={!text.trim()}>
        Open
      </button>
    </form>
  );
}

export function Scanner({ onResult }: { onResult: (text: string) => void }) {
  const Detector = detectorCtor();
  const [active, setActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [manual, setManual] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const onResultRef = useRef(onResult);
  onResultRef.current = onResult;

  useEffect(() => {
    if (!active || !Detector) return;
    let cancelled = false;
    let stream: MediaStream | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const stop = (): void => {
      clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
      stream = null;
    };
    (async () => {
      try {
        const detector = new Detector({ formats: ["qr_code"] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
        const el = video.current;
        if (cancelled || !el) {
          stop();
          return;
        }
        el.srcObject = stream;
        await el.play();
        const scan = async (): Promise<void> => {
          if (cancelled) return;
          try {
            const codes = await detector.detect(el);
            const hit = codes.find((c) => c.rawValue);
            if (hit) {
              stop();
              setActive(false);
              onResultRef.current(hit.rawValue);
              return;
            }
          } catch {
            // A frame that cannot be decoded yet; try the next one.
          }
          timer = setTimeout(() => void scan(), 250);
        };
        void scan();
      } catch (err) {
        if (cancelled) return;
        stop();
        setActive(false);
        setError(err instanceof DOMException && err.name === "NotAllowedError" ? "Camera access was refused." : "The camera is not available.");
      }
    })();
    return () => {
      cancelled = true;
      stop();
    };
  }, [active, Detector]);

  if (!Detector || error) {
    return (
      <div className="scanner">
        {error && <p className="mute-t small">{error} Type the id from the label instead.</p>}
        {!Detector && <p className="mute-t small">This browser cannot scan codes. Type the id from the label.</p>}
        <ManualEntry onSubmit={onResult} />
      </div>
    );
  }
  return (
    <div className="scanner">
      {active ? (
        <div className="viewfinder live">
          <video ref={video} muted playsInline aria-label="Camera preview" />
          <span className="vf-hint">Point the camera at the QR label</span>
          <button type="button" className="btn" onClick={() => setActive(false)}>
            Stop
          </button>
        </div>
      ) : (
        <button type="button" className="btn primary big" onClick={() => setActive(true)}>
          Scan QR label
        </button>
      )}
      {manual ? (
        <ManualEntry onSubmit={onResult} autoFocus />
      ) : (
        <button type="button" className="btn link" onClick={() => setManual(true)}>
          Type the id instead
        </button>
      )}
    </div>
  );
}
