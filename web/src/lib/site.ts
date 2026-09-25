/** Helpers over the `Site` document: space tree, device placement, health. */

import type { Device, Protocol, Site, Space } from "../api/types";
import type { Scope } from "../state/ui";

export const PROTOCOL_LABELS: Record<Protocol, string> = {
  "bacnet-uc": "BACnet-uc",
  "bacnet-ip": "BACnet/IP",
  mqtt: "MQTT",
};

export function childSpaces(site: Site, parent: string | null): Space[] {
  return site.spaces.filter((s) => s.parent === parent);
}

/**
 * The space that holds all others when a site is one building (a single
 * top-level space with children). The UI shows it as the site itself, so the
 * tree and the breadcrumbs do not name the building twice.
 */
export function buildingSpace(site: Site): Space | null {
  const top = childSpaces(site, null);
  const only = top.length === 1 ? top[0]! : null;
  return only && childSpaces(site, only.id).length > 0 ? only : null;
}

/** The spaces listed right under the site: the building's floors and wings, or the top-level spaces. */
export function topSpaces(site: Site): Space[] {
  return childSpaces(site, buildingSpace(site)?.id ?? null);
}

/** `spacePath` without the building space, for breadcrumbs that start at the site. */
export function crumbPath(site: Site, id: string | null): Space[] {
  const building = buildingSpace(site);
  return spacePath(site, id).filter((s) => s.id !== building?.id);
}

/** The space and its ancestors, root first. Stops on cycles. */
export function spacePath(site: Site, id: string | null): Space[] {
  const out: Space[] = [];
  const seen = new Set<string>();
  let cur = id;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const s = site.spaces.find((x) => x.id === cur);
    if (!s) break;
    out.unshift(s);
    cur = s.parent;
  }
  return out;
}

function subtreeIds(site: Site, id: string): Set<string> {
  const out = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const s of site.spaces) {
      if (s.parent !== null && out.has(s.parent) && !out.has(s.id)) {
        out.add(s.id);
        grew = true;
      }
    }
  }
  return out;
}

export function devicesIn(site: Site, spaceId: string, recursive: boolean): Device[] {
  const ids = recursive ? subtreeIds(site, spaceId) : new Set([spaceId]);
  return site.devices.filter((d) => d.space !== null && ids.has(d.space));
}

/** Health of a space: "ok" when all its devices are online, "off" when none is, "warn" otherwise. */
export function spaceTone(devices: readonly Device[]): "ok" | "warn" | "off" {
  if (devices.length === 0) return "off";
  const offline = devices.filter((d) => !d.online).length;
  if (offline === 0) return "ok";
  return offline === devices.length ? "off" : "warn";
}

export function scopeName(site: Site | null, scope: Scope): string | null {
  if (!site) return null;
  if (scope.kind === "device") return scope.name;
  if (scope.kind === "space") return site.spaces.find((s) => s.id === scope.id)?.name ?? scope.id;
  return site.description || site.name;
}

/** Finds a device from what a QR label or a technician may give: name, instance, hwid or a URL. */
export function resolveDevice(input: string, devices: readonly Device[]): Device | null {
  let text = input.trim();
  if (!text) return null;
  try {
    const url = new URL(text);
    const param = url.searchParams.get("device") ?? url.searchParams.get("d");
    if (param) text = param;
    else {
      const m = /\/devices\/([^/?#]+)/.exec(url.pathname);
      if (m?.[1]) text = decodeURIComponent(m[1]);
    }
  } catch {
    // Not a URL.
  }
  text = text.replace(/^(uc|device|bacnet):/i, "").trim();
  const lower = text.toLowerCase();
  const byName = devices.find((d) => d.name.toLowerCase() === lower);
  if (byName) return byName;
  if (/^\d+$/.test(text)) {
    const inst = Number(text);
    const byInstance = devices.find((d) => d.instance === inst);
    if (byInstance) return byInstance;
  }
  return devices.find((d) => d.hwid && d.hwid.toLowerCase() === lower) ?? null;
}
