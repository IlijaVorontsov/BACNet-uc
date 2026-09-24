import { useState } from "react";
import type { Device, Protocol, Site, Space } from "../api/types";
import { childSpaces, devicesIn, PROTOCOL_LABELS, spaceTone } from "../lib/site";
import { useHub } from "../state/hub";
import { useUi, type Scope } from "../state/ui";
import { Dot, ErrorNote } from "../ui/common";

const PROTOCOLS: Protocol[] = ["bacnet-uc", "bacnet-ip", "mqtt"];

function Count({ n }: { n: number }) {
  return (
    <span className="cnt">
      <span className="sr-only">, </span>
      {n}
      <span className="sr-only"> {n === 1 ? "device" : "devices"}</span>
    </span>
  );
}

function same(a: Scope, b: Scope): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === "space" && b.kind === "space") return a.id === b.id;
  if (a.kind === "device" && b.kind === "device") return a.name === b.name;
  return true;
}

function DeviceNode({ d, depth }: { d: Device; depth: number }) {
  const ui = useUi();
  const scope: Scope = { kind: "device", name: d.name };
  return (
    <li>
      <button
        type="button"
        className={`tnode l${depth}`}
        aria-current={same(ui.scope, scope) ? "true" : undefined}
        onClick={() => ui.setScope(scope)}
      >
        <Dot tone={d.online ? "ok" : "off"} label={d.online ? "online" : "offline"} />
        <span className="name mono">{d.name}</span>
        <span className="cnt">{PROTOCOL_LABELS[d.protocol]}</span>
      </button>
    </li>
  );
}

function SpaceNode({ site, space, depth, visible }: { site: Site; space: Space; depth: number; visible: (d: Device) => boolean }) {
  const ui = useUi();
  const [open, setOpen] = useState(true);
  const scope: Scope = { kind: "space", id: space.id };
  const children = childSpaces(site, space.id);
  const own = site.devices.filter((d) => d.space === space.id && visible(d));
  const all = devicesIn(site, space.id, true).filter(visible);
  const hasKids = children.length > 0 || own.length > 0;
  return (
    <li>
      <div className="trow2">
        <button
          type="button"
          className="caret"
          aria-label={`${open ? "Collapse" : "Expand"} ${space.name}`}
          aria-expanded={open}
          disabled={!hasKids}
          onClick={() => setOpen((o) => !o)}
        >
          {hasKids ? (open ? "▾" : "▸") : ""}
        </button>
        <button
          type="button"
          className={`tnode l${depth}`}
          aria-current={same(ui.scope, scope) ? "true" : undefined}
          onClick={() => ui.setScope(scope)}
        >
          {children.length === 0 && all.length > 0 && <Dot tone={spaceTone(all)} />}
          <span className="name">{space.name}</span>
          <Count n={all.length} />
        </button>
      </div>
      {open && hasKids && (
        <ul>
          {children.map((c) => (
            <SpaceNode key={c.id} site={site} space={c} depth={depth + 1} visible={visible} />
          ))}
          {own.map((d) => (
            <DeviceNode key={d.name} d={d} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

/** Left pane: spaces and their devices with health dots, protocol filters and unplaced devices. */
export function SiteTree() {
  const hub = useHub();
  const ui = useUi();
  const [hidden, setHidden] = useState<ReadonlySet<Protocol>>(new Set());
  const site = hub.site.data;
  const visible = (d: Device): boolean => !hidden.has(d.protocol);
  const toggle = (p: Protocol): void =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });

  return (
    <nav className="tree" aria-label="Site tree">
      <div className="filters" role="group" aria-label="Show protocols">
        {PROTOCOLS.map((p) => (
          <button key={p} type="button" className={`chip ${hidden.has(p) ? "" : "acc"}`} aria-pressed={!hidden.has(p)} onClick={() => toggle(p)}>
            {PROTOCOL_LABELS[p]}
          </button>
        ))}
      </div>
      <ErrorNote error={site ? null : hub.site.error} />
      {site && (
        <ul className="tgroup">
          <li>
            <button
              type="button"
              className="tnode l0 root"
              aria-current={ui.scope.kind === "site" ? "true" : undefined}
              onClick={() => ui.setScope({ kind: "site" })}
            >
              <span className="name">{site.name.toUpperCase()}</span>
              <Count n={site.devices.filter(visible).length} />
            </button>
            <ul>
              {childSpaces(site, null).map((s) => (
                <SpaceNode key={s.id} site={site} space={s} depth={1} visible={visible} />
              ))}
            </ul>
          </li>
        </ul>
      )}
      {site && <Unplaced site={site} visible={visible} />}
    </nav>
  );
}

function Unplaced({ site, visible }: { site: Site; visible: (d: Device) => boolean }) {
  const ui = useUi();
  const devices = site.devices.filter((d) => d.space === null && visible(d));
  if (devices.length === 0) return null;
  return (
    <div className="tnote">
      <b>
        {devices.length} {devices.length === 1 ? "device is" : "devices are"} not placed
      </b>
      <ul className="tgroup">
        {devices.map((d) => (
          <DeviceNode key={d.name} d={d} depth={0} />
        ))}
      </ul>
      <button
        type="button"
        className="btn link"
        onClick={() => {
          ui.selectRun(null);
          ui.askAgent(`Identify the unplaced devices (${devices.map((d) => d.name).join(", ")}) and suggest where they belong.`, "onboard");
        }}
      >
        Ask the agent
      </button>
    </div>
  );
}
