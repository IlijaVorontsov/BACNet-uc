/**
 * Example site "hq" for the in-browser mock backend: Floor 2 with rooms
 * r201..r205 (BACnet-uc room controllers), AHU-1 (third-party BACnet/IP) in
 * the plant room, an MQTT CO2 sensor in room 204 and two devices found by
 * discovery that are not placed yet. Room 204 is being commissioned: plan p17
 * adds its IO, apps, link and CO2 bridge.
 */

import type { Change, Device, Plan, Point, Reading, Space, TestResult, Value } from "../types";

export const SITE = "hq";

export const SPACES: Space[] = [
  { id: "f2", name: "Floor 2", parent: null },
  { id: "r201", name: "Room 201", parent: "f2" },
  { id: "r202", name: "Room 202", parent: "f2" },
  { id: "r203", name: "Room 203", parent: "f2" },
  { id: "r204", name: "Room 204", parent: "f2" },
  { id: "r205", name: "Room 205", parent: "f2" },
  { id: "plant", name: "Plant room", parent: null },
];

interface RoomSpec {
  room: string;
  ip: string;
  instance: number;
  temp: number;
  setpoint: number;
  valve: number;
  online: boolean;
  /** The valve is overridden by an operator at priority 8 (the room 205 story). */
  override?: boolean;
}

const ROOMS: RoomSpec[] = [
  { room: "r201", ip: "10.0.2.41", instance: 2011, temp: 21.2, setpoint: 21.0, valve: 18, online: true },
  { room: "r202", ip: "10.0.2.42", instance: 2021, temp: 21.8, setpoint: 21.5, valve: 22, online: true },
  { room: "r203", ip: "10.0.2.43", instance: 2031, temp: 20.9, setpoint: 21.0, valve: 40, online: false },
  { room: "r204", ip: "10.0.2.51", instance: 2041, temp: 22.4, setpoint: 21.5, valve: 0, online: true },
  { room: "r205", ip: "10.0.2.52", instance: 2051, temp: 24.6, setpoint: 21.5, valve: 100, online: true, override: true },
];

export interface SeedPoint {
  point: Point;
  value: Value;
  priority?: number;
  /** Random-walk step and bounds for live values; absent = constant. */
  walk?: { step: number; min: number; max: number };
}

function point(
  device: string,
  obj: string,
  name: string,
  p: Partial<Omit<Point, "id" | "device" | "obj" | "name">> = {},
): Point {
  return {
    id: `${SITE}/${device}/${obj}`,
    device,
    obj,
    name,
    kind: p.kind ?? "value",
    datatype: p.datatype ?? "real",
    units: p.units ?? null,
    writable: p.writable ?? false,
    commandable: p.commandable ?? false,
    tags: p.tags ?? [],
    safety: p.safety ?? "normal",
    description: p.description ?? "",
    source: p.source ?? "",
    space: p.space ?? null,
  };
}

function label(room: string): string {
  return `R${room.slice(1)}`;
}

function roomPoints(r: RoomSpec): SeedPoint[] {
  const dev = `${r.room}-ctl`;
  const L = label(r.room);
  const space = r.room;
  const temp: SeedPoint = {
    point: point(dev, "analog-input:1", `${L} Temp`, {
      kind: "input",
      units: "degrees-celsius",
      tags: ["Zone_Air_Temperature_Sensor"],
      source: "io:ai0",
      space,
    }),
    value: r.temp,
    walk: { step: 0.06, min: r.temp - 0.8, max: r.temp + 0.8 },
  };
  const valve: SeedPoint = {
    point: point(dev, "analog-output:1", `${L} Reheat valve`, {
      kind: "output",
      units: "percent",
      writable: true,
      commandable: true,
      tags: ["Reheat_Valve_Command"],
      source: r.room === "r204" ? "io:ao0" : "app:thermostat",
      space,
    }),
    value: r.valve,
    ...(r.room === "r204" ? {} : { priority: r.override ? 8 : 12 }),
    ...(r.override || r.room === "r204" ? {} : { walk: { step: 1.5, min: Math.max(0, r.valve - 12), max: r.valve + 12 } }),
  };
  if (r.room === "r204") return [temp, valve];
  return [
    temp,
    {
      point: point(dev, "analog-value:1", `${L} Setpoint`, {
        units: "degrees-celsius",
        writable: true,
        commandable: true,
        tags: ["Zone_Air_Temperature_Setpoint"],
        source: "app:thermostat",
        space,
      }),
      value: r.setpoint,
      priority: 16,
    },
    valve,
    {
      point: point(dev, "binary-input:1", `${L} Window contact`, {
        kind: "input",
        datatype: "enum",
        tags: ["Window_Status"],
        source: "io:di0",
        space,
      }),
      value: 0,
    },
  ];
}

/** Points that plan p17 adds to r204-ctl. */
export function r204PlanPoints(): SeedPoint[] {
  const dev = "r204-ctl";
  const space = "r204";
  return [
    {
      point: point(dev, "analog-value:1", "R204 Setpoint", {
        units: "degrees-celsius",
        writable: true,
        commandable: true,
        tags: ["Zone_Air_Temperature_Setpoint"],
        source: "app:thermostat",
        space,
      }),
      value: 21.5,
      priority: 16,
    },
    {
      point: point(dev, "binary-input:1", "R204 Window contact", {
        kind: "input",
        datatype: "enum",
        tags: ["Window_Status"],
        source: "io:di0",
        space,
      }),
      value: 0,
    },
    {
      point: point(dev, "analog-value:10", "AHU-1 supply temp", {
        units: "degrees-celsius",
        tags: ["Supply_Air_Temperature_Sensor"],
        source: "link:ahu1-ctl/analog-value:3",
        space,
      }),
      value: 16.8,
      walk: { step: 0.05, min: 16.2, max: 17.4 },
    },
    {
      point: point(dev, "analog-value:20", "R204 CO2", {
        units: "parts-per-million",
        writable: true,
        commandable: true,
        tags: ["Zone_Air_CO2_Sensor"],
        source: "bridge:hq/r204-co2/co2",
        space,
      }),
      value: 612,
      priority: 12,
      walk: { step: 7, min: 560, max: 700 },
    },
  ];
}

export function seedDevices(now: number): Device[] {
  const rooms: Device[] = ROOMS.map((r) => ({
    name: `${r.room}-ctl`,
    protocol: "bacnet-uc",
    address: `${r.ip}:1337`,
    online: r.online,
    managed: true,
    model: "nucleo_f767zi",
    firmware: "0.1.0",
    hwid: `0x3a${r.instance.toString(16)}`,
    instance: r.instance,
    space: r.room,
    last_seen: r.online ? now - 2 : now - 7400,
  }));
  return [
    ...rooms,
    {
      name: "ahu1-ctl",
      protocol: "bacnet-ip",
      address: "10.0.2.10:47808",
      online: true,
      managed: false,
      model: "AHU controller",
      firmware: "4.2",
      hwid: "",
      instance: 100,
      space: "plant",
      last_seen: now - 1,
    },
    {
      name: "r204-co2",
      protocol: "mqtt",
      address: "sensors/r204/co2",
      online: true,
      managed: false,
      model: "CO2 sensor (generic JSON)",
      firmware: "",
      hwid: "",
      instance: null,
      space: "r204",
      last_seen: now - 4,
    },
    {
      name: "node-2099",
      protocol: "bacnet-uc",
      address: "10.0.2.60:1337",
      online: true,
      managed: true,
      model: "frdm_mcxn947",
      firmware: "0.1.0",
      hwid: "0x5c0de2099",
      instance: 2099,
      space: null,
      last_seen: now - 3,
    },
    {
      name: "mqtt-7f3a",
      protocol: "mqtt",
      address: "bacnet-uc/zephyr-7f3a0c11",
      online: true,
      managed: false,
      model: "mqtt_tls node (nucleo_h563zi)",
      firmware: "0.1.0",
      hwid: "7f3a0c11",
      instance: null,
      space: null,
      last_seen: now - 6,
    },
  ];
}

export function seedPoints(): SeedPoint[] {
  const out: SeedPoint[] = ROOMS.flatMap(roomPoints);
  const ahu = "ahu1-ctl";
  out.push(
    {
      point: point(ahu, "analog-value:3", "Supply Air Temp", {
        units: "degrees-celsius",
        tags: ["Supply_Air_Temperature_Sensor"],
        source: "bacnet-ip",
        space: "plant",
      }),
      value: 16.8,
      walk: { step: 0.05, min: 16.2, max: 17.4 },
    },
    {
      point: point(ahu, "analog-input:2", "Return Air Temp", {
        kind: "input",
        units: "degrees-celsius",
        tags: ["Return_Air_Temperature_Sensor"],
        source: "bacnet-ip",
        space: "plant",
      }),
      value: 22.1,
      walk: { step: 0.04, min: 21.6, max: 22.6 },
    },
    {
      point: point(ahu, "analog-output:4", "Cooling valve", {
        kind: "output",
        units: "percent",
        writable: true,
        commandable: true,
        tags: ["Cooling_Valve_Command"],
        source: "bacnet-ip",
        space: "plant",
      }),
      value: 35,
      priority: 12,
      walk: { step: 1, min: 28, max: 42 },
    },
    {
      point: point(ahu, "binary-output:1", "Supply fan", {
        kind: "output",
        datatype: "enum",
        writable: true,
        commandable: true,
        tags: ["Supply_Fan_Command"],
        safety: "critical",
        source: "bacnet-ip",
        space: "plant",
      }),
      value: 1,
      priority: 8,
    },
    {
      point: point(ahu, "binary-output:9", "Smoke damper", {
        kind: "output",
        datatype: "enum",
        commandable: true,
        tags: ["Smoke_Damper_Command"],
        safety: "life-safety",
        description: "Controlled by the fire panel",
        source: "bacnet-ip",
        space: "plant",
      }),
      value: 1,
      priority: 1,
    },
    {
      point: point("r204-co2", "co2", "R204 CO2 sensor", {
        kind: "input",
        units: "parts-per-million",
        tags: ["Zone_Air_CO2_Sensor"],
        source: "mqtt:sensors/r204/co2 $.ppm",
        space: "r204",
      }),
      value: 612,
      walk: { step: 7, min: 560, max: 700 },
    },
    {
      point: point("r204-co2", "battery", "R204 CO2 battery", {
        kind: "input",
        units: "percent",
        source: "mqtt:sensors/r204/co2 $.bat.pct",
        space: "r204",
      }),
      value: 87,
    },
    {
      point: point("node-2099", "analog-input:1", "AI 1", { kind: "input", source: "io:ai0" }),
      value: 0.41,
      walk: { step: 0.01, min: 0.35, max: 0.47 },
    },
    {
      point: point("mqtt-7f3a", "telemetry.temp_c", "Board temp", {
        kind: "input",
        units: "degrees-celsius",
        source: "mqtt:bacnet-uc/zephyr-7f3a0c11/telemetry $.temp_c",
      }),
      value: 31.5,
      walk: { step: 0.1, min: 30.5, max: 32.5 },
    },
    {
      point: point("mqtt-7f3a", "telemetry.uptime_s", "Uptime", {
        kind: "input",
        datatype: "int",
        units: "seconds",
        source: "mqtt:bacnet-uc/zephyr-7f3a0c11/telemetry $.uptime_s",
      }),
      value: 86112,
    },
  );
  return out;
}

export function seedReading(p: SeedPoint, online: boolean, ts: number): Reading {
  return {
    id: p.point.id,
    value: p.value,
    ts,
    quality: online ? "good" : "offline",
    ...(p.priority !== undefined ? { priority: p.priority } : {}),
  };
}

// ----------------------------------------------------------------- plan

const IO_DIFF = `--- a/io.json
+++ b/io.json
@@ -1,4 +1,5 @@
 [
   {"channel": "ai0", "type": "analog-input", "instance": 1, "name": "R204 Temp", "units": "degrees-celsius"},
-  {"channel": "ao0", "type": "analog-output", "instance": 1, "name": "R204 Reheat valve", "units": "percent"}
+  {"channel": "ao0", "type": "analog-output", "instance": 1, "name": "R204 Reheat valve", "units": "percent"},
+  {"channel": "di0", "type": "binary-input", "instance": 1, "name": "R204 Window contact", "debounce_ms": 200}
 ]`;

const WASM_DIFF = `--- /dev/null
+++ b/lfs/apps/thermostat.wasm
@@ -0,0 +1 @@
+thermostat.wasm: 3174 bytes, sha256 9f3c2b7e…a41e, built from thermostat.c (41 lines)`;

const APPS_DIFF = `--- a/apps.json
+++ b/apps.json
@@ -1 +1,10 @@
-[]
+[
+  {"name": "thermostat", "file": "/lfs/apps/thermostat.wasm",
+   "perms": ["bacnet.local"], "period_ms": 1000,
+   "params": {"setpoint": "21.5", "kp": "25", "ki": "0.05"},
+   "sha256": "9f3c2b7e…a41e"},
+  {"name": "link", "file": "/lfs/apps/uc-link.wasm",
+   "perms": ["bacnet.local", "bacnet.remote"],
+   "params": {"count": "1", "l0": "100 2 3 2 10 cov 1000 0 1 0"}}
+]`;

const BRIDGE_DIFF = `--- a/site.yaml
+++ b/site.yaml
@@ -41 +41,4 @@
-bridges: []
+bridges:
+  - from: r204-co2/co2          # MQTT sensors/r204/co2 $.ppm
+    to: r204-ctl/analog-value:20
+    max_age_s: 120`;

const TAGS_DIFF = `--- a/site.yaml
+++ b/site.yaml
@@ -58 +61 @@ tags:
-  r204-ctl/analog-input:1: [space:r204]
+  r204-ctl/analog-input:1: [Zone_Air_Temperature_Sensor]`;

export function planP17(now: number): Plan {
  const changes: Change[] = [
    { id: "c1", target: "r204-ctl", kind: "upload-doc", summary: "io.json: +1 point (di0 window contact)", diff: IO_DIFF, tier: "C" },
    { id: "c2", target: "r204-ctl", kind: "upload-file", summary: "thermostat.wasm (3.1 KB)", diff: WASM_DIFF, tier: "C" },
    { id: "c3", target: "r204-ctl", kind: "upload-doc", summary: "apps.json: +2 apps (thermostat, link)", diff: APPS_DIFF, tier: "C" },
    { id: "c4", target: "r204-ctl", kind: "reload", summary: "reload io, apps", diff: "", tier: "C" },
    { id: "c5", target: "gateway", kind: "bridge-add", summary: "bridge r204-co2/co2 → r204-ctl/analog-value:20", diff: BRIDGE_DIFF, tier: "C" },
    { id: "c6", target: "gateway", kind: "tags", summary: "tags: 1 point", diff: TAGS_DIFF, tier: "C" },
  ];
  return {
    id: "p17",
    revision: 17,
    base_revision: 16,
    created_at: now,
    targets: ["r204-ctl", "gateway"],
    warnings: ["app link requests permission bacnet.remote (subscribes to ahu1-ctl by COV)"],
    changes,
    blocked: {},
  };
}

const ACCEPTANCE_TESTS: { name: string; steps: string }[] = [
  { name: "Valve opens when the room is cold", steps: "force ai0 650 mV (15 °C) → expect AO1 > 50 % within 5 s" },
  { name: "Valve closes above setpoint", steps: "force ai0 750 mV (25 °C) → expect AO1 = 0 % within 30 s" },
  { name: "AHU supply temp arrives by COV", steps: "write ahu1-ctl AV3 = 18 → expect AV10 ≈ 18 within 3 s" },
];

export function testResults(target: "sim" | "live", status: TestResult["status"]): TestResult[] {
  const durations = [4210, 11820, 2050];
  return ACCEPTANCE_TESTS.map((t, i) => ({
    name: t.name,
    status,
    duration_ms: status === "pass" || status === "fail" ? (durations[i] ?? 1000) : 0,
    failed_step: null,
    detail: t.steps,
    target,
  }));
}

export const MANIFEST_YAML = `apiVersion: bacnet-uc/v1
kind: Site
metadata:
  name: hq
  description: HQ building
spaces:
  - {id: f2, name: Floor 2}
  - {id: r201, name: Room 201, parent: f2}
  - {id: r202, name: Room 202, parent: f2}
  - {id: r203, name: Room 203, parent: f2}
  - {id: r204, name: Room 204, parent: f2}
  - {id: r205, name: Room 205, parent: f2}
  - {id: plant, name: Plant room}
placement:
  r201-ctl: r201
  r202-ctl: r202
  r203-ctl: r203
  r204-ctl: r204
  r205-ctl: r205
  ahu1-ctl: plant
  r204-co2: r204
external_devices:
  - {name: ahu1-ctl, protocol: bacnet-ip, address: 10.0.2.10, device_instance: 100}
  - name: r204-co2
    protocol: mqtt
    profile: generic-json
    topic: sensors/r204/co2
    points:
      - {id: co2, path: $.ppm, units: parts-per-million, datatype: real}
      - {id: battery, path: $.bat.pct, units: percent}
policy:
  agent_write_priority: 12
  approval_ttl_s: 1800
`;

export const DRAFT_YAML_EXTRA = `bridges:
  - from: r204-co2/co2
    to: r204-ctl/analog-value:20
    max_age_s: 120
tags:
  r204-ctl/analog-input:1: [Zone_Air_Temperature_Sensor]
`;
