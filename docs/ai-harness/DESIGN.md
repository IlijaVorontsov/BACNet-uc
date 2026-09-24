# BACnet-uc AI harness: design proposal

Status: **proposal**, not implemented yet. The interactive UI mockup is in
[`ui-mockup.html`](ui-mockup.html). Open it in a browser and use the
Desktop and Phone buttons to switch views.

## 1. Goal

A browser application where a commissioning engineer or facility operator
programs and commissions a **whole building** from one place, working with an
AI agent (Z.ai **GLM-5.3**). "Whole building" means:

- BACnet-uc boards (this repository's Zephyr firmware, managed over SMP);
- third-party BACnet/IP devices (controllers, VAV boxes, meters), reached
  through standard BACnet services;
- MQTT devices: this repository's `apps/mqtt_tls` nodes and generic MQTT
  sensors and actuators.

The agent handles the tedious work: discovery, naming, point mapping, writing
control logic, links between devices, acceptance tests and the handover
report. A human stays in control of every change that reaches equipment.

## 2. What already exists and what the harness builds on

| Asset | Where | What the harness uses it for |
|---|---|---|
| SMP management plane (UDP 1337, custom groups 64 `uc_app`, 65 `uc_io`, 66 `uc_node`) | `docs/management-protocol.md` (BACnet branch) | Driver for BACnet-uc nodes: info, objects, property read/write, IO catalog/force, app install, file upload |
| `System` manifest (`bacnet-uc/v1`) with `nodes`, `apps`, `links`, `tests` | `schemas/system.schema.json` | **The desired-state format.** The agent edits the manifest; it never makes one-off pushes to devices. The harness plans, applies and tests it. |
| `device.json`, `io.json`, `apps.json` schemas | `schemas/` | Validation of every per-node document before upload |
| WASM guest SDK `bacnet_uc.h` (ABI 1.0) and the stock `uc-link` app | `wasm/` | Target for AI-written control logic. `links:` compile to `uc-link` parameters. |
| `transport: sim` + `native_sim` boards | schemas + `firmware/boards/` | Simulation-first: every plan can be applied to a simulated copy of the site before it touches hardware |
| Planned Python MCP server | `harness/` (reserved in the BACnet session notes, not started) | The gateway lives in `hub/` so the branches don't conflict. It exposes its tool registry as an MCP server too, so Claude Code and other MCP clients can use the same tools. |
| MQTT/TLS app topics (`<root>/<id>/status\|info\|telemetry\|cmd\|event`) | `apps/mqtt_tls/README.md` | First-class MQTT driver profile |

The two firmware branches use different Zephyr versions (3.7.2 LTS for MQTT
on the H563, 4.4.2 for BACnet on the F767/MCXN947). This does not affect the
harness, which only talks to the devices over the network.

## 3. Architecture

```
 ┌───────────────────────────── Browser (desktop / phone PWA) ─────────────────────────────┐
 │  Site tree · Points · Logic · Changes (plan/diff) · Tests · Trends · Agent panel         │
 │  REST for commands, SSE for the event stream (resumable with Last-Event-ID), Web Push    │
 └───────────────────────────────────────────┬──────────────────────────────────────────────┘
                                             │ HTTPS (OIDC session)
 ┌───────────────────────────────────────────▼──────────────────────────────────────────────┐
 │ uc-hub  (site gateway: Python 3.12, asyncio, FastAPI; one per site, runs on the LAN)      │
 │                                                                                           │
 │  Agent runtime ──► LLM provider adapter ──────────────────────────────► Z.ai GLM-5.3      │
 │   · run/turn state machine, persisted        (OpenAI-compatible,          api.z.ai/api/   │
 │   · approval gates                            tool_stream, thinking)      paas/v4         │
 │   · context builder                                                                       │
 │        │ tool calls                                                                       │
 │  Tool registry (same tools also exposed as an MCP server)                                 │
 │        │                                                                                  │
 │  Policy engine ── risk tier, role, point deny-list, priority rules, rate limits           │
 │        │                                                                                  │
 │  Desired-state engine: validate ─► plan (diff vs live) ─► apply ─► verify (tests)         │
 │        │                                     ▲                                            │
 │  Building model (SQLite/Postgres): sites, spaces, equipment, devices, points, tags,       │
 │  manifests + revisions, runs, approvals, audit log, trend store                           │
 │        │                                                                                  │
 │  Drivers ─┬─ bacnet-uc  (SMP/UDP 1337 or serial; smpclient + CBOR)                        │
 │           ├─ bacnet-ip  (bacpypes3: Who-Is/I-Am, RP/RPM, WP, COV, priority arrays)       │
 │           └─ mqtt       (aiomqtt; profiles: bacnet-uc mqtt_tls, Sparkplug B, HA discovery,│
 │                          generic JSON with JSONPath mapping)                              │
 │                                                                                           │
 │  Sim farm: native_sim nodes on a private network, used for "apply to simulation"          │
 │  WASM toolchain sandbox: clang --target=wasm32 + host-side unit tests with native stubs   │
 └──────────────┬─────────────────────────┬──────────────────────────┬──────────────────────┘
                │ UDP 47808 (BACnet/IP)   │ UDP 1337 (SMP, DTLS)     │ TLS 8883 (MQTT)
         third-party BACnet        BACnet-uc boards           MQTT broker ◄─ mqtt_tls nodes
```

### 3.1 Why the agent runs in the gateway, not in the browser

A browser alone cannot do this job:

1. **Browsers cannot send UDP.** BACnet/IP (47808) and SMP (1337) are UDP.
   Browsers can speak MQTT only over WebSockets. Something on the building LAN
   has to translate.
2. **The API key must not reach the browser.** An API key held in the
   browser can be read by anyone who can open the page.
3. **Runs must outlive the tab.** Commissioning a floor can take the agent
   30 minutes of tool calls. A phone going to sleep must not stop it, and
   the operator must be able to resume on another device.
4. **One audit trail.** Every tool call, approval and device write is recorded
   centrally, together with the user who approved it.

So the browser is a thin client. The gateway owns the agent loop, the tools
and all device I/O. A cloud relay for remote access is optional (§11) and is
not needed for a first version.

## 4. Building model

The agent needs **one namespace across protocols**. Every point gets a
stable ID, whatever protocol serves it:

```
<site>/<device>/<object>
hq/ahu1-ctl/analog-input:1              BACnet (bacnet-uc or third party)
hq/room-204-sensor/telemetry.temp_c     MQTT (JSONPath into the payload)
```

| Entity | Key fields |
|---|---|
| Site, building, floor, space (room/zone) | Hierarchy for navigation and scoping ("everything on floor 2") |
| Equipment | AHU, VAV, FCU, meter, luminaire group... has points, lives in a space |
| Device | Protocol, address, driver, firmware, online state, `managed: true` for BACnet-uc boards |
| Point | ID, kind (input/output/value), datatype, units, writable, commandable (has priority array), **semantic tags**, safety class |
| Tags | [Brick Schema](https://brickschema.org) classes (`Zone_Air_Temperature_Sensor`, `Supply_Fan_Command`), with Haystack-style marker tags as aliases |
| Safety class | `normal`, `critical` (e.g. freeze stat), `life-safety` (smoke control, fire dampers). Life-safety points are **read-only** for the agent, with no exceptions. |

Tags let the agent and the user work in building terms ("all zone
temperature setpoints on floor 2") instead of object numbers. Most of the
tagging is also done by the agent: it proposes tags from object names, units
and descriptions, and the user confirms them in bulk.

## 5. Desired state: extending the `System` manifest

The existing `bacnet-uc/v1 System` manifest already describes BACnet-uc nodes,
apps, links and tests. The harness adds a `Site` kind that **embeds**
`System` unchanged and adds the parts it does not cover:

```yaml
apiVersion: bacnet-uc/v1
kind: Site
metadata: {name: hq, description: "HQ building, floors 1-3"}
spaces:
  - {id: f2, name: "Floor 2"}
  - {id: r204, name: "Room 204", parent: f2}
system:                          # the existing System document, unchanged
  apiVersion: bacnet-uc/v1
  kind: System
  metadata: {name: hq-uc}
  nodes:
    - name: r204-ctl
      board: nucleo_f767zi
      transport: {kind: udp, host: 10.0.2.51}
      device: {instance: 2041, name: "R204 Controller", location: "Room 204"}
      io:
        - {channel: ai0, type: analog-input, instance: 1, name: "R204 Temp",
           units: degrees-celsius, scale: 0.1, offset: -50}
        - {channel: ao0, type: analog-output, instance: 1, name: "R204 Valve", units: percent}
  apps:
    - name: thermostat
      node: r204-ctl
      source: logic/thermostat.c
      perms: [bacnet.local]
      params: {setpoint: 21.5}
  links:
    - {from: ahu1-ctl/analog-value:3, to: r204-ctl/analog-value:10}
  tests:
    - name: valve opens when cold
      steps:
        - force: {node: r204-ctl, channel: ai0, value: 650}   # 15 °C
        - expect: {point: r204-ctl/analog-output:1, op: gt, value: 50, within_ms: 5000}
        - release: {node: r204-ctl, channel: ai0}
external_devices:                # not managed by the harness, only mapped
  - name: ahu1-ctl
    protocol: bacnet-ip
    address: 10.0.2.10
    device_instance: 100
    equipment: ahu1
  - name: r204-co2
    protocol: mqtt
    profile: generic-json
    topic: sensors/r204/co2
    points:
      - {id: co2, path: "$.ppm", units: parts-per-million, tags: [Zone_Air_CO2_Sensor]}
bridges:                         # cross-protocol links, run in the gateway
  - {from: hq/r204-co2/co2, to: r204-ctl/analog-value:20, max_age_s: 120}
tags:
  hq/r204-ctl/analog-input:1: [Zone_Air_Temperature_Sensor, space:r204]
policy:
  agent_write_priority: 12
  deny: ["hq/ahu1-ctl/binary-output:9"]     # smoke damper
```

Design choices:

- **The manifest is the source of truth and is versioned.** Every agent change
  is a new revision with a diff, author "agent (approved by <user>)" and a
  link to the run. Rollback means applying the previous revision.
- **Links between BACnet-uc nodes stay on the devices** (the `uc-link` WASM
  app, as the existing design specifies). Only links that cross protocols
  (MQTT to BACnet) run in the gateway as `bridges`, and each bridge has a
  staleness limit (`max_age_s`). When the source goes stale, the gateway
  relinquishes the destination value.
- **Tests are part of the manifest.** Commissioning is complete when the
  acceptance tests pass on the real site. The agent writes the tests together
  with the logic.

## 6. Drivers

A driver implements one interface:

```python
class Driver(Protocol):
    async def discover(self, scope: Scope) -> list[DeviceRecord]
    async def describe(self, device: DeviceRef) -> DeviceDescription   # objects/points
    async def read(self, points: list[PointRef]) -> list[Reading]
    async def write(self, point: PointRef, value: Value, priority: int | None,
                    lease: Lease | None) -> WriteResult
    async def relinquish(self, point: PointRef, priority: int) -> None
    async def subscribe(self, points: list[PointRef]) -> AsyncIterator[Reading]
    async def plan(self, desired: DeviceSpec, live: DeviceDescription) -> list[Change]
    async def apply(self, change: Change) -> ChangeResult
```

| Driver | Transport | Discovery | Plan/apply |
|---|---|---|---|
| `bacnet-uc` | SMP over UDP 1337 (DTLS optional) or serial | BACnet Who-Is, then SMP `uc_node info` to recognise BACnet-uc boards | Full: uploads `device.json`/`io.json`, uploads WASM with sha256, runs `uc_app install` and `uc_node reload`, updates firmware through the SMP image group (MCUboot) |
| `bacnet-ip` | BACnet/IP 47808, BBMD/foreign device registration for other subnets | Who-Is/I-Am, then `object-list` with ReadPropertyMultiple | Only writable properties: names, setpoints, schedules, trend logs, COV increments. No logic upload: the agent cannot program third-party controllers, only configure and supervise them. |
| `mqtt` | TLS 8883, per-site broker | Retained `+/+/info` and `status` topics (the mqtt_tls profile), Sparkplug `NBIRTH`/`DBIRTH`, Home Assistant `homeassistant/+/+/config` | Profile-dependent: mqtt_tls supports commands (`ping`, `led ...`), generic JSON is mapped read/write through JSONPath |

**Suggested firmware follow-ups** (for the firmware branches, not the harness):
the mqtt_tls app should publish a machine-readable capabilities document on
`info` and accept JSON commands, and BACnet-uc boards should expose an
"identify" command (blink an LED) through `uc_node`, so the phone UI can say
"the board that is blinking now is R204".

## 7. Tools the agent can call

All tools share one registry. They are exposed to GLM-5.3 as OpenAI-style
function definitions and to MCP clients as MCP tools. Each tool has a **risk
tier** that the policy engine enforces, whatever the model asks for.

| Tier | Meaning | Gate |
|---|---|---|
| **R** read | No side effects | Runs automatically |
| **S** sandbox | Changes only the draft manifest, the simulation or the build sandbox | Runs automatically |
| **L** live, reversible | Changes the live site within a lease (a value that expires) | Approval once per run, or per call, depending on role and policy |
| **C** commit | Applies a plan or changes firmware | Always an explicit approval showing the diff; hold-to-confirm on phones |

| Tool | Tier | Purpose |
|---|---|---|
| `site_search(query, filters)` | R | Search devices, points and equipment by text, tags and space. **The main way the agent sees the building**; the model never receives the whole point list. |
| `site_tree(space?)` | R | Spaces, equipment and device counts |
| `device_describe(device)` | R | Objects/points, firmware, online state, installed apps |
| `point_read(points[])` | R | Current values with quality and age |
| `point_history(point, range, agg)` | R | Trends, aggregated on the server to under 200 samples |
| `priority_array(point)` | R | Who commands a point, at which priority |
| `discover(protocol, scope)` | R | Who-Is / MQTT discovery sweep, returns only new or changed devices |
| `manifest_get(path?)` | R | Read the current or draft manifest (or a part of it) |
| `manifest_edit(json_patch)` | S | RFC 6902 patch on the **draft**. Validated against the schemas at once; errors go back to the model. |
| `tags_propose(points[], tags[])` | S | Bulk tagging suggestions for the user to confirm |
| `logic_write(app, source_c)` | S | Save WASM app C source in the draft |
| `logic_build(app)` | S | Compile in the sandbox (`clang --target=wasm32`, see §9) and return diagnostics |
| `logic_unit_test(app, cases)` | S | Run the app natively against stubbed `bacnet_uc.h` imports |
| `plan()` | S | Diff the draft against the live site: a list of changes per device, each with risk and rollback |
| `sim_apply()` / `sim_test(tests?)` | S | Apply the plan to the sim farm and run the manifest tests there |
| `point_write(point, value, lease_s)` | L | Write at the agent's priority with an expiry, relinquished automatically when the lease ends |
| `io_force(node, channel, value, lease_s)` | L | IO checkout: force an output, or simulate an input, then release |
| `device_identify(device)` | L | Blink the device so a technician can find it |
| `apply(plan_id)` | C | Apply an approved plan in stages, verifying after each device and stopping on the first failure |
| `test_run(tests?)` | L | Run the manifest tests on the live site (tests use forces and writes under leases) |
| `firmware_update(device, image)` | C | SMP image upload and MCUboot swap; confirmed after a health check |
| `report_generate(kind)` | R | Commissioning report, point list, IO checkout sheet |
| `ask_user(question, options)` | R | Structured question to the user, shown as buttons |

## 8. Agent loop

```
user message
  └─► build context
        system prompt: role, safety rules, site summary (≈2k tokens),
                       manifest outline, active skill playbook
        conversation + compacted earlier tool results
  └─► GLM-5.3 (stream, thinking on, tools, tool_stream)
        ├─ text / thinking deltas ──► SSE to browser
        └─ tool_calls
              for each call:
                policy.check(tool, args, user, site)  → allow | needs_approval | deny
                needs_approval → persist run as WAITING, send approval card + Web Push,
                                 resume when the user decides (from any device)
                execute → result (truncated/summarised to a token budget) → append
        loop until the model answers without tool calls, or a budget is hit
            (max 60 tool calls, 20 min wall clock, token budget per run)
```

**Context strategy.** GLM-5.3 has a 1M-token context, but a building with
10,000 points still should not be pasted into the prompt. It would be slow
and expensive, and the model gets worse at finding the relevant parts in a
long context. Instead:

- The prompt carries a **site summary**: counts per space and equipment type,
  protocols, unhealthy devices, and the draft's pending changes.
- The model reaches everything else through `site_search` and
  `device_describe`, which return compact tables.
- Large tool results are stored server-side. The model gets a summary and a
  handle (`result://r42`) it can page through.
- Older turns are compacted. Keep the static prefix (system prompt and tool
  definitions) stable so the provider's context cache can reuse it.

**Skills (playbooks).** These are task-specific instructions, loaded when the
user picks a workflow or the agent recognises one:

| Playbook | Steps |
|---|---|
| Onboard devices | discover → identify/locate → assign instance and name → propose tags → plan → apply |
| IO checkout | For each IO point: force/read, ask the technician to confirm on site (phone), record the result in the checkout sheet |
| Write control sequence | Clarify the sequence of operations → `logic_write` → build → unit tests → sim test → plan |
| Link devices | Resolve points by tags → `links:` or `bridges:` → sim test → plan |
| Troubleshoot | Trends, priority arrays, app status and logs → hypothesis → propose a fix as a plan, never as silent writes |
| Handover | Run all tests → generate the report → freeze the manifest revision |

## 9. AI-written control logic (WASM apps)

This is where the agent does real "programming". The pipeline gives the model
compiler and test feedback before anything reaches a board:

1. **Write.** The model writes C against `bacnet_uc.h`. The system prompt
   carries the header's API summary and the `uc-link` example.
2. **Build.** The sandbox (container, no network, CPU and memory limits,
   30 s timeout) runs the documented clang recipe. Compiler diagnostics go
   back to the model.
3. **Unit test.** The header already supports host builds (`UC_IMPORT` is
   empty when not compiling for wasm). The harness provides a stub
   implementation of the imports, so `logic_unit_test` can feed inputs,
   advance time and check outputs natively. The model writes these tests.
4. **Simulate.** Deploy to `native_sim` nodes in the sim farm, run the
   manifest `tests:`, and check the watchdog, heap and error counters from
   `uc_app status`.
5. **Plan and approve.** The upload is a plan entry with the sha256, the
   permissions (`perms`) it asks for, and the source diff.
6. **Deploy and verify.** Upload and install, then the tests run on the real
   node.

The WASM sandbox on the device already contains a faulty app: the host checks
permissions, the watchdog stops runaway callbacks, and the app's objects are
removed when it stops. The harness adds checks before deployment: an app that
requests `bacnet.remote` or `io` gets a warning in the approval card.

## 10. Safety model

Building control is safety-relevant. A wrong valve command costs energy or
comfort; a wrong smoke-control command can endanger lives. These rules are
enforced in the gateway code, **not by the system prompt**:

1. **Life-safety points are never writable** by the agent, whatever the user
   approves. Only a site admin can mark a point life-safety, and only a site
   admin can remove the mark.
2. **The agent writes with its own BACnet priority** (default 12), which is
   lower than manual operator (8), critical equipment control (5) and
   life safety (1, 2). It never writes at priorities 1 to 7.
3. **Every live write has a lease.** When the lease ends, or the gateway
   loses contact, the value is relinquished. Nothing the agent writes stays
   in the priority array by accident. Permanent changes are only made through
   `apply`.
4. **All permanent changes go through plan → approve → apply**, with a diff
   per device, and the diff is what the user approves. Apply runs in stages,
   verifies after each stage, and stops and offers a rollback on the first
   failure.
5. **Simulation first.** Changes to logic or links can only be applied live
   after `sim_test` passed for the same manifest revision (configurable per
   site).
6. **Rate limits and blast radius.** There is a limit on writes per minute
   and on devices per plan stage. A plan that touches more than N devices
   needs an admin.
7. **Treat device data as untrusted input.** Object names, descriptions and
   MQTT payloads are set by whoever configured the device, and can contain
   text that looks like instructions. The gateway passes them to the model
   marked as data, and policy decisions never depend on what the model
   concluded from them.
8. **Audit.** Every tool call, its arguments, the result summary, the approver
   and the timing are stored and exportable.

## 11. GLM-5.3 integration

| Item | Value |
|---|---|
| Endpoint | `POST https://api.z.ai/api/paas/v4/chat/completions` (OpenAI-compatible; Anthropic-compatible `.../api/anthropic` also exists) |
| Model | `glm-5.3` (text only, 1M context, up to 128K output) |
| Reasoning | Always on for GLM-5.3; use `reasoning_effort` `high` for planning and logic, `low` for quick lookups |
| Tools | OpenAI `tools` format with `type: function`; `tool_choice` supports only `auto` |
| Streaming | `stream: true` plus `tool_stream: true`, so tool-call arguments arrive incrementally and the UI can show "Writing manifest patch..." at once |
| Vision (optional) | `glm-5.3-flash` accepts images. Use it for photos of nameplates, wiring labels or panel schedules taken with the phone camera; it returns structured fields that the main agent then uses. |

The provider sits behind a small adapter (`chat(messages, tools, stream) →
events`), so the harness does not depend on one vendor. Other adapters are
the Anthropic-compatible endpoint, a self-hosted GLM (the weights are open, but
at 753B parameters this is data-centre hardware, not an edge box) or other
providers for comparison.

**Data governance.** Point names, values, network addresses and floor plans are
sent to the model provider. Before production use, check Z.ai's data retention
and processing-location terms against the building owner's requirements. The
gateway can pseudonymise IP addresses and device names before sending, and a
site can be configured "LLM off" (the UI and tools keep working without the
agent).

**Tool-call robustness.** Every tool argument is validated against its JSON
Schema. Validation errors go back to the model as tool results, with no
retry by the gateway. Two identical failing calls in a row end the loop and
hand the task to the user.

## 12. Gateway ↔ browser protocol

- `POST /api/runs` starts a run; `POST /api/runs/{id}/messages` sends a
  message; `POST /api/approvals/{id}` approves or rejects (with an optional
  comment for the model).
- `GET /api/runs/{id}/events` is an SSE stream. Events are numbered, so a
  phone that reconnects sends `Last-Event-ID` and gets what it missed.

| Event | Payload |
|---|---|
| `thinking.delta` | Reasoning text (collapsed in the UI by default) |
| `message.delta` | Assistant text |
| `tool.call` | Tool, arguments (streamed), tier |
| `tool.result` | Summary, a handle to the full result, duration |
| `approval.request` | Plan diff, affected points, tier, rollback, expiry |
| `plan.updated` | Draft revision and change count |
| `run.state` | `running`, `waiting_approval`, `done`, `failed`, `cancelled` |

- `GET /api/live?points=...` is a separate SSE stream for live values (COV,
  MQTT) behind the Points and Trends views.
- Web Push notifies about approvals and alarms when the PWA is closed.

## 13. Security

- OIDC login. Roles: **viewer** (read, chat with the agent in read-only
  mode), **operator** (tier-L approvals), **commissioner** (tier-C
  approvals on assigned sites), **admin** (policy, safety classes, users).
- The gateway stores the Z.ai key, broker credentials, the SMP DTLS PSKs
  and certificates encrypted at rest. The browser never receives them.
- Network: the gateway is the only host that needs the BMS VLAN. The UI can
  be reached on the LAN, or remotely through the site VPN or an outbound
  tunnel. No inbound port from the internet is needed.
- Device plane: use SMP over DTLS for BACnet-uc boards
  (`CONFIG_MCUMGR_TRANSPORT_UDP_DTLS`). BACnet/IP itself has no
  authentication, so the harness cannot secure it. Treat the BMS VLAN as the
  security boundary and plan BACnet/SC as a later driver.

## 14. UI proposal

The interactive mockup is [`ui-mockup.html`](ui-mockup.html). It uses the
same layout ideas on both form factors: **the building on one side, the
agent on the other, and a change is always shown as a reviewable diff**.

### Desktop (≥ 1024 px): three panes

```
┌──────────┬────────────────────────────────────────┬─────────────────────┐
│ Site     │ [Overview][Points][Logic][Changes 3]   │ Agent               │
│ tree     │ [Tests][Trends]                        │                     │
│          │                                        │ chat, streamed      │
│ HQ       │  Context-dependent workspace:          │ tool-call cards,    │
│ ▸ F1     │  point table with live values,         │ approval cards with │
│ ▾ F2     │  C editor with build output,           │ diff, "why?" link   │
│   R204 ● │  plan diff per device,                 │                     │
│   R205 ● │  test results, trend charts            │ [Commission R204 ▾] │
│ ▸ Plant  │                                        │ ┌─────────────────┐ │
│          │                                        │ │ Ask the agent…  │ │
│ ⚠ 2 off. │                                        │ └─────────────────┘ │
└──────────┴────────────────────────────────────────┴─────────────────────┘
```

- **Left:** site tree by space and equipment, with device health dots and
  filters by protocol and by tags.
- **Centre:** the workspace. The agent's actions are reflected here: when it
  searches points, the point table filters; when it edits the manifest, the
  Changes tab count goes up; when it builds logic, the Logic tab shows the
  compiler output.
- **Right:** the agent panel. Tool calls are shown as compact cards (tool,
  target, tier colour, duration) that expand to arguments and result.
  Approval cards show the diff and a rollback note, with Approve and Reject
  buttons.
- **Command palette** (Ctrl+K): search points, start a playbook, or ask the
  agent.

### Phone (< 768 px): agent first, for the technician on site

```
┌──────────────────────┐
│ HQ · Floor 2    ⚠ 1  │
├──────────────────────┤
│ Agent                │
│ ┌──────────────────┐ │
│ │ Forcing R204     │ │
│ │ valve to 100 %.  │ │
│ │ Is it open?      │ │
│ │ [Yes] [No] [Skip]│ │
│ └──────────────────┘ │
│ ▸ io_force 0.3 s  L  │
│                      │
│ [📷] Ask…        [➤] │
├──────────────────────┤
│ Agent Site Changes ⌖ │
└──────────────────────┘
```

- **Bottom tabs:** Agent, Site (spaces and devices), Changes (approvals),
  Field (scan and checkout).
- **Agent tab:** the same stream as on desktop. `ask_user` questions are
  shown as large buttons, so the IO checkout ("Is the valve open?") can be
  answered with one thumb while the technician stands at the valve.
- **Approvals:** a bottom sheet with a per-device summary; the full diff is
  one tap away. Tier-C changes need **hold-to-confirm** (1.5 s), not a
  single tap.
- **Field mode:** scan a QR code on the board to open the device; the camera
  button sends a nameplate photo to the vision model; Identify makes the
  board blink.
- **Resilience:** it is a PWA, so the shell and the last site snapshot work
  offline. Runs continue on the gateway; on reconnect the event stream
  resumes from the last event ID.

## 15. Proposed repository layout and stack

```
hub/                          Python 3.12 (not harness/, which the BACnet branch reserved)
  src/uc_hub/
    api/                      FastAPI routes, SSE
    agent/                    run loop, context builder, playbooks/
    llm/                      provider adapters (zai.py, anthropic_compat.py)
    tools/                    tool registry + JSON Schemas; mcp_server.py
    policy/                   tiers, roles, priority/lease rules
    model/                    building model, manifest store, revisions
    drivers/                  bacnet_uc/ (smp), bacnet_ip/ (bacpypes3), mqtt/ (aiomqtt)
    engine/                   plan / apply / verify
    sim/                      native_sim farm management
    build/                    wasm toolchain sandbox, native stub harness
  tests/                      unit tests + e2e against native_sim nodes and mosquitto
web/                          TypeScript, React + Vite, PWA
  src/panes/  src/agent/  src/field/
schemas/site.schema.json      new Site kind (embeds system.schema.json)
```

Python matches the MCP server the BACnet session notes plan for
`harness/`, and has mature BACnet (bacpypes3), SMP (smpclient) and MQTT
(aiomqtt) libraries.

## 16. Milestones

| # | Deliverable | Proof it works |
|---|---|---|
| M1 | Gateway skeleton, building model, `bacnet-uc` + `mqtt` drivers (read-only), MCP server | Point list of two native_sim nodes and one mqtt_tls node, in Claude Code through MCP |
| M2 | GLM-5.3 adapter, agent loop, SSE, desktop UI with read-only tools | "Which rooms are above 24 °C?" answered correctly from live data |
| M3 | Draft manifest, `manifest_edit`, `plan`, sim farm, approvals, `apply` for bacnet-uc | The agent onboards a new native_sim node end to end |
| M4 | WASM logic pipeline (build, unit tests, sim), `tests:` on the live site | The agent writes and deploys a thermostat app on nucleo_f767zi, and the tests pass |
| M5 | Phone PWA, field mode, Web Push, `bacnet-ip` driver for third-party devices | IO checkout of a board from a phone; supervising a third-party controller |
| M6 | Leases, rate limits, audit export, OIDC roles, report generation | Handover report of a test site |

## 17. Open questions

1. **Where does the gateway run?** Options: a small Linux box per site
   (Raspberry Pi 5 class or an industrial PC), a VM on the building's server,
   or a container next to the MQTT broker. The design assumes one per site.
2. **Multi-site:** is one UI across several buildings needed early? That
   would need a cloud relay in front of the site gateways.
3. **Z.ai data terms:** acceptable for the target customers, or is a
   self-hosted or alternative model needed for some sites?
4. **Which third-party device families** matter most? That decides the depth
   of the `bacnet-ip` driver (schedules, trend logs, alarms/event
   enrollment).
5. **Should the MQTT boards also run WASM logic**, or stay as sensor/actuator
   endpoints behind gateway bridges?
