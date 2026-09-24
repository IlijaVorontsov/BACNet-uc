/** What the mock agent does for a message: a few scripted scenarios on the example site. */

import type { Approval, Reading } from "../types";
import { planP17, testResults } from "./data";
import type { ScriptContext } from "./runs";

export type Script = (ctx: ScriptContext) => Promise<void>;

export interface ScriptEnv {
  /** The pending approval of a plan, in any run. */
  pendingApprovalFor(planId: string): Approval | undefined;
}

const THERMOSTAT_C = `/* thermostat.c: PI control of the reheat valve */
#include "bacnet_uc.h"
UC_APP_DECLARE();
#define AI_TEMP  1
#define AO_VALVE 1
#define AV_SETPT 1
#define PRIO     12  /* below operator (8) */
static double kp, ki, integ;
UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
\tkp = uc_param_num("kp", 25.0);
\tki = uc_param_num("ki", 0.05);
\tuc_obj_create_str(UC_OBJ_ANALOG_VALUE, AV_SETPT, "R204 Setpoint");
\treturn uc_pv_write(UC_OBJ_ANALOG_VALUE, AV_SETPT, uc_param_num("setpoint", 21.5), UC_PRIORITY_NONE);
}
UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
\tdouble t, sp;
\tif (uc_pv_read(UC_OBJ_ANALOG_INPUT, AI_TEMP, &t) < 0 || uc_pv_read(UC_OBJ_ANALOG_VALUE, AV_SETPT, &sp) < 0)
\t\treturn;
\tdouble err = sp - t;
\tinteg = clamp(integ + ki * err, 0, 100);
\tuc_pv_write(UC_OBJ_ANALOG_OUTPUT, AO_VALVE, clamp(kp * err + integ, 0, 100), PRIO);
}
`;

const MANIFEST_PATCH = [
  { op: "add", path: "/system/nodes/4/io/-", value: { channel: "di0", type: "binary-input", instance: 1 } },
  { op: "add", path: "/system/apps/-", value: { name: "thermostat", node: "r204-ctl", perms: ["bacnet.local"] } },
  { op: "add", path: "/system/links/-", value: { from: "ahu1-ctl/analog-value:3", to: "r204-ctl/analog-value:10" } },
  { op: "add", path: "/bridges/-", value: { from: "r204-co2/co2", to: "r204-ctl/analog-value:20", max_age_s: 120 } },
  { op: "replace", path: "/tags/r204-ctl~1analog-input:1", value: ["Zone_Air_Temperature_Sensor"] },
  { op: "add", path: "/system/tests/-", value: { name: "Valve opens when the room is cold" } },
  { op: "add", path: "/system/tests/-", value: { name: "Valve closes above setpoint" } },
];

function clock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtTemp(r: Reading | undefined): string {
  return typeof r?.value === "number" ? `${r.value.toFixed(1)} °C` : "no value";
}

export function commission(env: ScriptEnv): Script {
  return async (ctx) => {
    const site = ctx.site;
    if (site.liveRevision >= 17) {
      await ctx.say("Room 204 is already commissioned: plan p17 is live as revision 17, and its acceptance tests passed.");
      return;
    }
    const waiting = env.pendingApprovalFor("p17");
    if (waiting && waiting.run_id !== ctx.run.summary.id) {
      await ctx.say("Plan p17 for room 204 is already waiting for approval in another run. Approve or reject it there.");
      return;
    }
    if (!site.plan) {
      await ctx.think(
        "r204-ctl is new and has no io.json yet. The window contact is on di0, the valve on ao0. AHU-1 is a third-party BACnet/IP device, so the link runs as uc-link on r204-ctl, subscribed by COV to AV3. The CO2 sensor only speaks MQTT, so it needs a gateway bridge. Write at priority 12, below the operator.",
      );
      await ctx.tool("site_search", "R", { query: "space:r204" }, 200, () => ({
        ok: true,
        summary: "1 new BACnet-uc node (r204-ctl), 1 MQTT sensor (r204-co2)",
        data: { devices: ["r204-ctl", "r204-co2"] },
      }));
      await ctx.tool("device_describe", "R", { device: "r204-ctl" }, 400, () => ({
        ok: true,
        summary: "nucleo_f767zi, 4 IO channels, no apps",
      }));
      await ctx.tool("device_describe", "R", { device: "ahu1-ctl" }, 600, () => ({
        ok: true,
        summary: 'AV3 "Supply Air Temp", COV supported',
      }));
      await ctx.tool("manifest_edit", "S", { json_patch: MANIFEST_PATCH }, 100, () => ({
        ok: true,
        summary: "Draft revision 17 is valid against site.schema.json",
      }));
      await ctx.tool("logic_write", "S", { app: "thermostat", source_c: THERMOSTAT_C }, 40, () => ({
        ok: true,
        summary: `${THERMOSTAT_C.split("\n").length} lines`,
      }));
      await ctx.tool("logic_build", "S", { app: "thermostat" }, 1800, () => ({
        ok: true,
        summary: "3.1 KB, 0 warnings",
        data: { size: 3174, warnings: 0, perms: ["bacnet.local"], abi: "1.0" },
      }));
      await ctx.tool(
        "logic_unit_test",
        "S",
        {
          app: "thermostat",
          cases: [
            "cold room opens valve",
            "warm room closes valve",
            "integrator clamps at 100 %",
            "sensor fault holds output",
            "setpoint write takes effect",
            "writes use priority 12",
          ],
        },
        900,
        () => ({ ok: true, summary: "6 of 6 passed" }),
      );
      await ctx.tool("sim_test", "S", { tests: [] }, 21000, () => {
        site.setTests(testResults("sim", "pass"));
        return { ok: true, summary: "3 of 3 tests passed on native_sim × 2" };
      });
      ctx.emit({ type: "tests.updated", results: site.tests.map((t) => ({ ...t })) });
      const draft = planP17(ctx.now());
      await ctx.tool("plan", "S", {}, 700, () => {
        site.setPlan(draft);
        return {
          ok: true,
          summary: `${draft.changes.length} changes on ${draft.targets.length} targets (plan ${draft.id})`,
          data: { plan_id: draft.id, revision: draft.revision },
        };
      });
      ctx.emit({ type: "plan.updated", plan_id: draft.id, revision: draft.revision, changes: draft.changes.length });
      await ctx.say(
        "The plan is ready. It maps the window contact, deploys `thermostat` with only the `bacnet.local` permission, links the AHU-1 supply temperature by COV, and bridges the CO2 sensor from MQTT. All three acceptance tests passed in simulation.",
      );
    } else {
      await ctx.say("Plan p17 is still the draft. I am asking for approval again.");
    }

    const plan = site.plan;
    if (!plan) return;
    const { decision } = await ctx.gatedTool(
      "apply",
      "C",
      { plan_id: plan.id },
      {
        title: `Apply plan ${plan.id}`,
        summary: [
          "r204-ctl: io.json +1 point, thermostat.wasm, 2 apps, reload",
          "gateway: 1 MQTT → BACnet bridge, 1 tag change",
          "Then 3 acceptance tests run on the live site",
        ],
        diff: plan.changes.filter((c) => c.diff).map((c) => c.diff).join("\n"),
        rollback: `Apply revision ${plan.base_revision}`,
        planId: plan.id,
      },
      3500,
      () => {
        site.applyPlan(ctx.user);
        return {
          ok: true,
          summary:
            "2 stages applied. r204-ctl: 3 uploads, sha256 verified, reload io, apps OK. Gateway: bridge running, first value 612 ppm.",
        };
      },
    );
    if (decision !== "approved") {
      await ctx.say(
        decision === "expired"
          ? "The approval expired, so nothing was applied. Ask me to request it again when you are ready."
          : "Understood, nothing was applied. Tell me what to change and I will update the plan.",
      );
      return;
    }
    ctx.emit({ type: "plan.updated", plan_id: null, revision: null, changes: 0 });
    site.setTests(testResults("live", "running"));
    ctx.emit({ type: "tests.updated", results: site.tests.map((t) => ({ ...t })) });
    await ctx.tool("test_run", "L", { tests: [], target: "live" }, 38000, () => {
      site.setTests(testResults("live", "pass"));
      return { ok: true, summary: "3 of 3 passed · forces released" };
    });
    ctx.emit({ type: "tests.updated", results: site.tests.map((t) => ({ ...t })) });
    await ctx.say(
      "Room 204 is commissioned. All 3 acceptance tests passed on the real board, and every force was released. I added the results to the handover report.",
    );
  };
}

const CHECKS = [
  {
    ch: "ao0",
    act: "Forcing the reheat valve (ao0) to 100 % for 60 s.",
    q: "Is the room 204 valve fully open?",
    force: 100,
  },
  { ch: "do0", act: "Forcing the heater relay (do0) on for 60 s.", q: "Did the relay click and is the heater on?", force: 1 },
  {
    ch: "di0",
    act: "Watching the window contact (di0). It reads closed now.",
    q: "Open the window. Did the value change to open?",
    point: "hq/r204-ctl/binary-input:1",
  },
  {
    ch: "ai0",
    act: "ai0 reads 22.4 °C. Your reference thermometer should be within ±0.5 °C.",
    q: "Does your reference read 21.9–22.9 °C?",
    point: "hq/r204-ctl/analog-input:1",
  },
] as const;

const ioCheckout: Script = async (ctx) => {
  await ctx.think(
    "The checkout covers four channels of r204-ctl: ao0, do0, di0 and ai0. Outputs are forced under a 60 s lease, inputs are read live, and the technician confirms each one on site.",
  );
  await ctx.tool("device_describe", "R", { device: "r204-ctl" }, 300, () => ({
    ok: true,
    summary: "nucleo_f767zi · IO channels ai0, ao0, di0, do0",
  }));
  const answers: string[] = [];
  for (const c of CHECKS) {
    await ctx.say(c.act);
    if ("force" in c) {
      await ctx.tool("io_force", "L", { node: "r204-ctl", channel: c.ch, value: c.force, lease_s: 60 }, 300, () => ({
        ok: true,
        summary: `Forced · auto-release at ${clock(ctx.now() + 60)}`,
      }));
    } else {
      await ctx.tool("point_read", "R", { points: [c.point] }, 200, () => ({ ok: true, summary: "Reading live" }));
    }
    answers.push(await ctx.ask(c.q, ["Yes", "No", "Skip"]));
  }
  const passed = answers.filter((a) => a === "Yes").length;
  const failed = answers.filter((a) => a === "No").length;
  const skipped = answers.length - passed - failed;
  await ctx.tool("report_generate", "R", { kind: "io-checkout", node: "r204-ctl" }, 600, () => ({
    ok: true,
    summary: "IO checkout sheet updated (4 channels)",
    handle: "result://io-checkout-r204",
  }));
  const parts = [`${passed} passed`];
  if (failed) parts.push(`${failed} failed`);
  if (skipped) parts.push(`${skipped} skipped`);
  await ctx.say(
    `Checkout finished: ${parts.join(", ")}. All forces are released. The checkout sheet is in the handover report.`,
  );
};

const troubleshoot: Script = async (ctx) => {
  const ids = ["hq/r205-ctl/analog-input:1", "hq/r205-ctl/analog-value:1", "hq/r205-ctl/analog-output:1"];
  await ctx.think("Room 205 is above its setpoint. Read temperature, setpoint and valve, then check who commands the valve.");
  await ctx.tool("point_read", "R", { points: ids }, 300, () => {
    const [t, sp, v] = ids.map((id) => ctx.site.reading(id));
    return {
      ok: true,
      summary: `Temp ${fmtTemp(t)}, setpoint ${fmtTemp(sp)}, reheat valve ${String(v?.value ?? "?")} %`,
    };
  });
  await ctx.tool("priority_array", "R", { point: ids[2] }, 250, () => ({
    ok: true,
    summary: "Priority 8 (manual operator): 100 %. Priority 12 (thermostat): 0 %.",
  }));
  await ctx.tool("point_history", "R", { point: ids[0], range: "3h", agg: "5m" }, 500, () => ({
    ok: true,
    summary: "Rose from 21.6 °C to 24.6 °C since 07:40; the valve has been at 100 % since 07:38",
  }));
  await ctx.say(
    "Room 205 is warm because its reheat valve is held at **100 %** by a manual override at priority 8, which wins over the thermostat app at priority 12. The override was set at 07:38 and the room has been heating since.\n\nI cannot release a priority-8 command. Ask the operator who set it, or release it in the BMS; the thermostat then takes over and closes the valve.",
  );
};

const temperatures: Script = async (ctx) => {
  const page = ctx.site.queryPoints({ tag: "Zone_Air_Temperature_Sensor" });
  await ctx.tool("site_search", "R", { query: "Zone_Air_Temperature_Sensor" }, 200, () => ({
    ok: true,
    summary: `${page.total} zone temperature sensors`,
  }));
  const readings = page.points.map((p) => ({ p, r: ctx.site.reading(p.id) }));
  await ctx.tool("point_read", "R", { points: page.points.map((p) => p.id) }, 300, () => ({
    ok: true,
    summary: `${readings.length} readings, ${readings.filter((x) => x.r?.quality !== "good").length} not good`,
  }));
  const warm = readings.filter((x) => x.r?.quality === "good" && typeof x.r.value === "number" && x.r.value > 24);
  const offline = readings.filter((x) => x.r?.quality === "offline");
  const lines = warm.length
    ? warm.map((x) => `- ${x.p.name}: ${fmtTemp(x.r)}`).join("\n")
    : "No room is above 24 °C right now.";
  const off = offline.length ? `\n\n${offline.map((x) => x.p.device).join(", ")} is offline, so I could not check it.` : "";
  await ctx.say(`${warm.length ? "Rooms above 24 °C:\n\n" : ""}${lines}${off}`);
};

const onboard: Script = async (ctx) => {
  const unassigned = ctx.site.devices.filter((d) => d.space === null);
  await ctx.tool("discover", "R", { protocol: null, scope: "site" }, 2500, () => ({
    ok: true,
    summary: `${unassigned.length} devices not placed: ${unassigned.map((d) => `${d.name} (${d.protocol}, ${d.address})`).join(", ")}`,
  }));
  await ctx.say(
    `I found ${unassigned.length} devices that are not placed yet: ${unassigned.map((d) => `\`${d.name}\``).join(" and ")}. Use Identify on the Field tab to find them, then tell me which room each one is in and I will add them to the draft.`,
  );
};

const fallback: Script = async (ctx) => {
  const last = [...ctx.run.events].reverse().find((e) => e.type === "message.user");
  const q = last && last.type === "message.user" ? last.text : "";
  const page = ctx.site.queryPoints({ q: q.split(/\s+/).slice(0, 2).join(" ") });
  await ctx.tool("site_search", "R", { query: q }, 250, () => ({ ok: true, summary: `${page.total} matching points` }));
  await ctx.say(
    "This demo agent only plays a few scripted scenarios: commissioning room 204, the IO checkout of r204-ctl, troubleshooting room 205, finding unplaced devices and the temperature overview. Try one of the playbook buttons.",
  );
};

const PLAYBOOK_TITLES: Record<string, string> = {
  onboard: "Onboard devices",
  "io-checkout": "IO checkout · r204-ctl",
  troubleshoot: "Troubleshoot room 205",
};

export function pickScript(message: string, playbook: string | undefined, env: ScriptEnv): Script {
  const m = message.toLowerCase();
  if (playbook === "io-checkout" || /checkout/.test(m)) return ioCheckout;
  if (/commission/.test(m)) return commission(env);
  if (playbook === "troubleshoot" || /warm|troubleshoot|r205|room 205/.test(m)) return troubleshoot;
  if (playbook === "onboard" || /unassigned|unplaced|onboard|discover|identify/.test(m)) return onboard;
  if (/above|temperature|°c|hot|cold/.test(m)) return temperatures;
  return fallback;
}

export function runTitle(message: string, playbook: string | undefined): string {
  if (playbook && PLAYBOOK_TITLES[playbook]) return PLAYBOOK_TITLES[playbook];
  const first = message.trim().split(/(?<=[.?!])\s|\n/)[0] ?? "";
  const colon = first.indexOf(":");
  const clean = (colon > 0 ? first.slice(0, colon) : first).replace(/[.]+$/, "");
  return clean.length > 60 ? `${clean.slice(0, 57)}…` : clean || "New run";
}
