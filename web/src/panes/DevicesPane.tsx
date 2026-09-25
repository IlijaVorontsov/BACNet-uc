import { useEffect, useState } from "react";
import type { DeviceDescription } from "../api/types";
import { formatAgo } from "../lib/format";
import { devicesIn, PROTOCOL_LABELS } from "../lib/site";
import { useIdentify } from "../state/actions";
import { useHub, useResource } from "../state/hub";
import { useUi, type Scope } from "../state/ui";
import { Chip, Dot, ErrorNote } from "../ui/common";

function DeviceDetail({ name }: { name: string }) {
  const hub = useHub();
  const desc = useResource<DeviceDescription>((s) => hub.client.device(name, s), [hub.client, name, hub.site.data]);
  const { identify, busy, error } = useIdentify();
  const [blinking, setBlinking] = useState(false);
  useEffect(() => {
    if (!blinking) return;
    const t = setTimeout(() => setBlinking(false), 30000);
    return () => clearTimeout(t);
  }, [blinking]);
  if (desc.error && !desc.data) return <ErrorNote error={desc.error} />;
  if (!desc.data) return <p className="mute-t">Loading {name}…</p>;
  const { device: d, apps, extra, points } = desc.data;
  const onIdentify = async (): Promise<void> => {
    if (await identify(d)) setBlinking(true);
  };
  return (
    <div className="devdetail">
      <div className="devcols">
        <div className="side-card">
          <h4>Device</h4>
          <dl className="kv">
            <dt>protocol</dt>
            <dd>{PROTOCOL_LABELS[d.protocol]}</dd>
            <dt>address</dt>
            <dd>{d.address}</dd>
            <dt>model</dt>
            <dd>{d.model || "–"}</dd>
            <dt>firmware</dt>
            <dd>{d.firmware || "–"}</dd>
            <dt>instance</dt>
            <dd>{d.instance ?? "–"}</dd>
            <dt>hwid</dt>
            <dd>{d.hwid || "–"}</dd>
            <dt>managed</dt>
            <dd>{d.managed ? "yes, by the hub" : "no"}</dd>
            <dt>last seen</dt>
            <dd>{formatAgo(d.last_seen)}</dd>
            <dt>points</dt>
            <dd>{points.length}</dd>
          </dl>
          <div className="row">
            <button type="button" className="btn" onClick={() => void onIdentify()} disabled={busy !== null || !d.online}>
              {blinking ? "Blinking for 30 s" : busy ? "Identifying…" : "Identify"}
            </button>
            {!d.online && <span className="mute-t small">Offline</span>}
          </div>
          <ErrorNote error={error ? { message: error } : null} />
        </div>
        <div className="side-card">
          <h4>Details</h4>
          {Object.keys(extra).length ? <pre className="code small">{JSON.stringify(extra, null, 2)}</pre> : <p className="mute-t small">Nothing reported.</p>}
        </div>
      </div>
      {d.protocol === "bacnet-uc" && (
        <div className="side-card">
          <h4>Apps</h4>
          {apps.length === 0 ? (
            <p className="mute-t small">No WASM apps installed.</p>
          ) : (
            <table className="pts">
              <thead>
                <tr>
                  <th scope="col">App</th>
                  <th scope="col">State</th>
                  <th scope="col">Permissions</th>
                  <th scope="col" className="r">
                    Ticks
                  </th>
                  <th scope="col" className="r">
                    Errors
                  </th>
                </tr>
              </thead>
              <tbody>
                {apps.map((a) => (
                  <tr key={a.name}>
                    <td className="obj">{a.name}</td>
                    <td>
                      <span className={a.state === "running" ? "ok-t" : a.state === "failed" ? "crit-t" : "mute-t"}>{a.state}</span>
                      {a.last_error && <div className="crit-t small">{a.last_error}</div>}
                    </td>
                    <td>
                      {a.perms.map((p) => (
                        <span key={p} className="tag">
                          {p}
                        </span>
                      ))}
                    </td>
                    <td className="v">{a.ticks}</td>
                    <td className="v">{a.errors}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

/** Device detail when a device is selected, otherwise the devices of the selected space. */
export function DevicesPane({ scope }: { scope: Scope }) {
  const hub = useHub();
  const ui = useUi();
  const site = hub.site.data;
  // Keyed so that another device never shows this one's details or identify state.
  if (scope.kind === "device") return <DeviceDetail key={scope.name} name={scope.name} />;
  if (!site) return <p className="mute-t">Loading devices…</p>;
  const devices = scope.kind === "space" ? devicesIn(site, scope.id, true) : site.devices;
  if (devices.length === 0) return <p className="mute-t">No devices in this space.</p>;
  return (
    <ul className="devgrid">
      {devices.map((d) => (
        <li key={d.name}>
          <button type="button" className="devcard" onClick={() => ui.setScope({ kind: "device", name: d.name })}>
            <span className="top">
              <Dot tone={d.online ? "ok" : "off"} label={d.online ? "online" : "offline"} />
              <b className="mono">{d.name}</b>
              {d.space === null && <Chip tone="warn" className="mini">not placed</Chip>}
            </span>
            <span className="mute-t small">
              {PROTOCOL_LABELS[d.protocol]} · {d.address}
            </span>
            <span className="mute-t small">
              {d.model || "unknown model"} · {d.points ?? 0} points
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
