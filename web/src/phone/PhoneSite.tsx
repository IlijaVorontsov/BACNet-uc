import { useMemo, useState } from "react";
import type { Device, PointWithReading, Site, Space } from "../api/types";
import { formatClock, formatReading, formatValue } from "../lib/format";
import { childSpaces, devicesIn, PROTOCOL_LABELS, spaceTone } from "../lib/site";
import { useHub, useResource } from "../state/hub";
import { useLive, type LiveValue } from "../state/streams";
import { useUi } from "../state/ui";
import { Dot, ErrorNote } from "../ui/common";

const TEMP_TAGS = ["Zone_Air_Temperature_Sensor", "Supply_Air_Temperature_Sensor"];

function firstTagged(points: readonly PointWithReading[], tags: readonly string[]): PointWithReading | undefined {
  for (const t of tags) {
    const p = points.find((x) => x.tags.includes(t));
    if (p) return p;
  }
  return undefined;
}

interface RoomProps {
  site: Site;
  space: Space;
  points: PointWithReading[];
  live: ReadonlyMap<string, LiveValue>;
  open: boolean;
  onToggle: () => void;
}

function RoomCard({ site, space, points, live, open, onToggle }: RoomProps) {
  const ui = useUi();
  const devices = devicesIn(site, space.id, true);
  const temp = firstTagged(points, TEMP_TAGS);
  const setpoint = firstTagged(points, ["Zone_Air_Temperature_Setpoint"]);
  const tr = temp ? (live.get(temp.id)?.reading ?? temp.reading) : undefined;
  const sr = setpoint ? (live.get(setpoint.id)?.reading ?? setpoint.reading) : undefined;
  const tone = spaceTone(devices);
  const offline = devices.filter((d) => !d.online);
  const warm = typeof tr?.value === "number" && typeof sr?.value === "number" && tr.value > sr.value + 1.5;
  let status: string;
  if (devices.length === 0) status = "No devices";
  else if (offline.length === devices.length) status = `Offline since ${formatClock(offline[0]?.last_seen)}`;
  else if (warm) status = `Above setpoint ${formatValue(sr?.value)}°`;
  else if (sr) status = `Setpoint ${formatValue(sr.value, setpoint)}°`;
  else status = `${devices.length} device${devices.length === 1 ? "" : "s"}`;
  const big = tr && tr.quality !== "offline" && typeof tr.value === "number" ? `${formatValue(tr.value, temp)}°` : "—";

  return (
    <li className="roomwrap">
      <button type="button" className="room" aria-expanded={open} onClick={onToggle}>
        <b>{space.name}</b>
        <span className={`big${warm ? " warn-t" : ""}`}>{big}</span>
        <span className="s">
          <Dot tone={warm && tone === "ok" ? "warn" : tone} />
          {status}
        </span>
      </button>
      {open && (
        <div className="roomdetail">
          {devices.map((d) => (
            <DeviceBlock key={d.name} device={d} points={points.filter((p) => p.device === d.name)} live={live} onField={() => {
              ui.setFieldDevice(d.name);
              ui.setPhoneTab("field");
            }} />
          ))}
        </div>
      )}
    </li>
  );
}

function DeviceBlock({ device, points, live, onField }: { device: Device; points: PointWithReading[]; live: ReadonlyMap<string, LiveValue>; onField: () => void }) {
  return (
    <div className="devblock">
      <div className="top">
        <Dot tone={device.online ? "ok" : "off"} label={device.online ? "online" : "offline"} />
        <b className="mono">{device.name}</b>
        <span className="mute-t small">{PROTOCOL_LABELS[device.protocol]}</span>
        <button type="button" className="btn link" onClick={onField}>
          Field
        </button>
      </div>
      <ul className="clist">
        {points.map((p) => (
          <li key={p.id}>
            <span className="mono small mute-t">{p.obj}</span>
            <span>{p.name}</span>
            <span className="mono">{formatReading(live.get(p.id)?.reading ?? p.reading, p)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Spaces as room cards with the live zone temperature; a tap shows the room's devices and points. */
export function PhoneSite() {
  const hub = useHub();
  const ui = useUi();
  const site = hub.site.data;
  const [open, setOpen] = useState<string | null>(null);
  const page = useResource((s) => hub.client.points({ limit: 1000 }, s), [hub.client, site]);
  const points = useMemo(() => page.data?.points ?? [], [page.data]);

  const liveIds = useMemo(() => {
    const ids = new Set<string>();
    for (const p of points) {
      if (p.tags.some((t) => TEMP_TAGS.includes(t) || t === "Zone_Air_Temperature_Setpoint")) ids.add(p.id);
      if (open && site && p.space && devicesIn(site, open, true).some((d) => d.name === p.device)) ids.add(p.id);
    }
    return [...ids];
  }, [points, open, site]);
  const { values } = useLive({ ids: liveIds });

  if (!site) return <ErrorNote error={hub.site.error}>{hub.site.error ? undefined : "Loading the site…"}</ErrorNote>;
  const pointsIn = (space: Space): PointWithReading[] => {
    const names = new Set(devicesIn(site, space.id, true).map((d) => d.name));
    return points.filter((p) => names.has(p.device));
  };
  const unplaced = site.devices.filter((d) => d.space === null);

  return (
    <div className="pstack">
      {childSpaces(site, null).map((top) => {
        const rooms = childSpaces(site, top.id);
        const cards = rooms.length ? rooms : [top];
        return (
          <section key={top.id} aria-label={top.name} className="pstack">
            <p className="label">
              {top.name}
              {rooms.length ? ` · ${rooms.length} rooms` : ""}
            </p>
            <ul className="rooms">
              {cards.map((s) => (
                <RoomCard key={s.id} site={site} space={s} points={pointsIn(s)} live={values} open={open === s.id} onToggle={() => setOpen((o) => (o === s.id ? null : s.id))} />
              ))}
            </ul>
          </section>
        );
      })}
      {unplaced.length > 0 && (
        <section aria-label="Devices not placed" className="pstack">
          <p className="label">Not placed · {unplaced.length}</p>
          {unplaced.map((d) => (
            <DeviceBlock key={d.name} device={d} points={[]} live={values} onField={() => {
              ui.setFieldDevice(d.name);
              ui.setPhoneTab("field");
            }} />
          ))}
        </section>
      )}
    </div>
  );
}
