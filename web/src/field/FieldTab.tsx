import { useEffect, useMemo, useState } from "react";
import type { Device } from "../api/types";
import { formatReading } from "../lib/format";
import { PROTOCOL_LABELS, resolveDevice } from "../lib/site";
import { useIdentify } from "../state/actions";
import { useHub, useResource } from "../state/hub";
import { useLive } from "../state/streams";
import { useUi } from "../state/ui";
import { ErrorNote } from "../ui/common";
import { Scanner } from "./Scanner";

function DevicePoints({ device }: { device: Device }) {
  const { client } = useHub();
  const page = useResource((s) => client.points({ device: device.name, limit: 8 }, s), [client, device.name]);
  const { values } = useLive({ device: device.name });
  const points = page.data?.points ?? [];
  if (points.length === 0) return null;
  return (
    <section className="pstack" aria-label={`Live values of ${device.name}`}>
      <p className="label">Live values</p>
      <ul className="clist">
        {points.map((p) => (
          <li key={p.id}>
            <span className="mono small mute-t">{p.obj}</span>
            <span>{p.name}</span>
            <span className="mono">{formatReading(values.get(p.id)?.reading ?? p.reading, p)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Field tools: pick or scan a device, make it blink, see its live values. */
export function FieldTab() {
  const hub = useHub();
  const ui = useUi();
  const devices = useMemo(() => hub.site.data?.devices ?? [], [hub.site.data]);
  const selected = devices.find((d) => d.name === ui.fieldDevice) ?? devices[0] ?? null;
  const { identify, busy, error } = useIdentify();
  const [blinkUntil, setBlinkUntil] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [scanNote, setScanNote] = useState<string | null>(null);
  const blinking = blinkUntil > now && selected?.name === ui.fieldDevice;

  useEffect(() => {
    if (blinkUntil <= Date.now()) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [blinkUntil]);

  const onIdentify = async (): Promise<void> => {
    if (!selected) return;
    ui.setFieldDevice(selected.name);
    if (await identify(selected, 30)) {
      setNow(Date.now());
      setBlinkUntil(Date.now() + 30000);
    }
  };

  const onScan = (text: string): void => {
    const d = resolveDevice(text, devices);
    if (d) {
      ui.setFieldDevice(d.name);
      setBlinkUntil(0);
      setScanNote(`Opened ${d.name}.`);
    } else {
      setScanNote(`No device matches “${text}”.`);
    }
  };

  if (!hub.site.data) return <ErrorNote error={hub.site.error}>{hub.site.error ? undefined : "Loading devices…"}</ErrorNote>;
  const left = Math.max(0, Math.ceil((blinkUntil - now) / 1000));

  return (
    <div className="pstack">
      <label className="field">
        <span>Device</span>
        <select
          value={selected?.name ?? ""}
          onChange={(e) => {
            ui.setFieldDevice(e.target.value);
            setBlinkUntil(0);
          }}
        >
          {devices.map((d) => (
            <option key={d.name} value={d.name}>
              {d.name}
              {d.space ? "" : " (not placed)"}
              {d.online ? "" : " · offline"}
            </option>
          ))}
        </select>
      </label>
      {selected && (
        <div className="board" data-testid="field-device">
          <span className={`led${blinking ? " blink" : ""}`} aria-hidden="true" />
          <div>
            <b className="mono">{selected.name}</b>
            <div className="mute-t small">
              {selected.model || PROTOCOL_LABELS[selected.protocol]} · {selected.address}
            </div>
          </div>
          <span className={`small ${selected.online ? "ok-t" : "mute-t"}`}>{selected.online ? "online" : "offline"}</span>
        </div>
      )}
      <div className="fgrid">
        <button type="button" className="fbtn" onClick={() => void onIdentify()} disabled={!selected || !selected.online || busy !== null}>
          <b>Identify</b>
          <small aria-live="polite">{blinking ? `Blinking now · ${left} s` : busy ? "Asking the device…" : "Blink the board LED for 30 s"}</small>
        </button>
      </div>
      <ErrorNote error={error ? { message: error } : null} />
      <section aria-label="Scan a label" className="pstack">
        <p className="label">Find a device by its label</p>
        <Scanner onResult={onScan} />
        {scanNote && (
          <p className="small" role="status">
            {scanNote}
          </p>
        )}
      </section>
      {selected && selected.online && <DevicePoints device={selected} />}
    </div>
  );
}
