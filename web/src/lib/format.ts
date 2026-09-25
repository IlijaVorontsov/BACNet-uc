/** Display formatting for values, units and times. */

import type { Point, Reading, RunState, TestStatus, Value } from "../api/types";

const UNIT_SYMBOLS: Record<string, string> = {
  "degrees-celsius": "°C",
  "degrees-fahrenheit": "°F",
  "degrees-kelvin": "K",
  percent: "%",
  "percent-relative-humidity": "%RH",
  "parts-per-million": "ppm",
  "parts-per-billion": "ppb",
  seconds: "s",
  minutes: "min",
  hours: "h",
  milliseconds: "ms",
  volts: "V",
  millivolts: "mV",
  amperes: "A",
  milliamperes: "mA",
  watts: "W",
  kilowatts: "kW",
  "kilowatt-hours": "kWh",
  pascals: "Pa",
  kilopascals: "kPa",
  "cubic-meters-per-hour": "m³/h",
  "liters-per-second": "l/s",
  hertz: "Hz",
  luxes: "lx",
};

export function unitSymbol(units: string | null | undefined): string {
  if (!units || units === "no-units") return "";
  return UNIT_SYMBOLS[units] ?? units;
}

function decimals(units: string | null): number {
  if (units === "percent" || units === "parts-per-million" || units === "seconds") return 0;
  return 1;
}

/** "22.4", "35", "active", "–" (no value). Units are separate, see `unitSymbol`. */
export function formatValue(value: Value | undefined, point?: Pick<Point, "obj" | "datatype" | "units">): string {
  if (value === null || value === undefined) return "–";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  if (point && /^binary-/.test(point.obj)) return value ? "active" : "inactive";
  if (point && (point.datatype === "int" || point.datatype === "enum")) return String(Math.round(value));
  if (!Number.isFinite(value)) return String(value);
  if (Number.isInteger(value) && point?.datatype !== "real") return String(value);
  return value.toFixed(decimals(point?.units ?? null));
}

export function formatReading(r: Reading | undefined, point: Pick<Point, "obj" | "datatype" | "units">): string {
  if (!r) return "–";
  const v = formatValue(r.value, point);
  const u = typeof r.value === "number" && !/^binary-/.test(point.obj) ? unitSymbol(point.units) : "";
  return u ? `${v} ${u}` : v;
}

export function formatClock(ts: number | null | undefined, withSeconds = false): string {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
  });
}

/** "just now", "5 min ago", "3 h ago", "2 d ago". */
export function formatAgo(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

/** "0.2 s", "1.8 s", "21 s", "1 min 5 s". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  if (ms < 10000) return `${(ms / 1000).toFixed(1)} s`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  const rest = s % 60;
  return rest ? `${m} min ${rest} s` : `${m} min`;
}

/** "expires in 12 min", "expired". */
export function formatExpiry(expiresAt: number, now = Date.now() / 1000): string {
  const s = expiresAt - now;
  if (s <= 0) return "expired";
  if (s < 90) return "expires in 1 min";
  return `expires in ${Math.round(s / 60)} min`;
}

export interface Tone {
  tone: "ok" | "warn" | "crit" | "acc" | "";
  label: string;
}

/** `stoppedByError`: the last turn ended with an error event (the run is idle, but not done). */
export function runStateTone(state: RunState | null, stoppedByError = false): Tone {
  if (state === "idle" && stoppedByError) return { tone: "crit", label: "Stopped by an error" };
  switch (state) {
    case "running":
      return { tone: "acc", label: "Working" };
    case "waiting_approval":
      return { tone: "warn", label: "Waiting for approval" };
    case "waiting_answer":
      return { tone: "warn", label: "Waiting for an answer" };
    case "idle":
      return { tone: "ok", label: "Done" };
    case "failed":
      return { tone: "crit", label: "Failed" };
    case "cancelled":
      return { tone: "", label: "Cancelled" };
    default:
      return { tone: "", label: "Loading" };
  }
}

export function testStatusTone(status: TestStatus | undefined): Tone {
  switch (status) {
    case "pass":
      return { tone: "ok", label: "Passed" };
    case "fail":
      return { tone: "crit", label: "Failed" };
    case "error":
      return { tone: "crit", label: "Error" };
    case "running":
      return { tone: "warn", label: "Running…" };
    case "skipped":
      return { tone: "warn", label: "Skipped" };
    default:
      return { tone: "", label: "Not run" };
  }
}

export function initials(name: string): string {
  const parts = name.split(/[\s._-]+/).filter(Boolean);
  const out = parts.length > 1 ? `${parts[0]![0]}${parts[1]![0]}` : name.slice(0, 2);
  return out.toUpperCase();
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
