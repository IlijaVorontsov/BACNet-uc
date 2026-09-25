# Handoff: uc-hub AI harness to the site server

The AI harness (uc-hub) was built in a Claude Code cloud session
(`session_0136NrtgJ6e32dFqB8o2kPmA`) on branch
`claude/ai-harness-building-control-05nrgm`. Work continues on a PC that
becomes the first site server. This file records the state at the handoff
point and how to pick it up.

## Handoff point

The handoff is the branch head that contains this file together with the
fixes of the final whole-system review (see "Verification" below). At that
point all suites pass, the deploy files have been tested, and the cloud
session stops committing to the branch. From then on the PC is the only
writer.

## Continue on the PC

1. Install Claude Code on the PC and sign in with the same claude.ai account
   as the cloud session (`claude`, then `/login`).
2. Clone the repository and bring the session over. Teleport checks out the
   branch and loads the whole conversation:

   ```sh
   git clone https://github.com/IlijaVorontsov/BACNet-uc && cd BACNet-uc
   claude --teleport session_0136NrtgJ6e32dFqB8o2kPmA
   ```

   The working tree must be clean. The terminal gets its own copy of the
   session; work done there does not flow back into the cloud session.
3. Optional: `claude remote-control` (or `/remote-control` in the session)
   lets you steer the PC's session from the phone or claude.ai while the
   work runs on the PC.
4. Keep the development clone (above) separate from the deployment
   checkout in `/opt/uc-hub` that the service runs (`deploy/README.md`).

## First day on the PC

1. Build and test the checkout (`deploy/README.md` step 1): hub tests, web
   build, `uc-hub demo`.
2. **Run the demo against the real model**: `ZAI_API_KEY=... uc-hub demo --llm zai`.
   Everything so far was tested with the scripted model, which follows fixed
   rules. GLM-5.3 has not yet run against the tools. Try the playbooks
   (commission room 204, IO checkout, troubleshoot), and watch token use and
   whether it picks the right tools. Tune `agent/prompts.py` and
   `agent/playbooks.py` from what you see.
3. Install the service with the mosquitto broker (`deploy/README.md` steps
   2-5) and bring up the first real board: a NUCLEO-F767ZI with
   `apps/mqtt_tls` fw 0.3.0 from the MQTT branch, on mutual TLS.
4. BACnet-uc boards follow when the BACnet firmware
   (`claude/zephyr-bacnet-stm32-162k1g`, still a WIP snapshot) runs on
   hardware. Until then the hub's simulator (`uc_hub.sim`) stands in for
   them.

## Z.ai account

uc-hub uses the standard API with an API key, paid from the account balance
(`https://api.z.ai/api/paas/v4`, model `glm-5.3`). A GLM Coding Plan
subscription does not cover it: Z.ai limits that plan to its supported coding
tools and gives it a separate endpoint. If the plan is what was bought, it
can still be used for coding tools, but the hub needs a normal API key
with balance.

## What exists

| Part | Where | State |
|---|---|---|
| Design, UI proposal, API and site formats | `docs/ai-harness/` (DESIGN, API, SITE, `ui-mockup.html`, `screenshots/`) | Current |
| Gateway | `hub/` (`hub/README.md` has the package map) | Drivers (BACnet-uc over SMP, BACnet/IP, MQTT), simulators, manifest plan/apply/tests, store, policy and leases, runtime, 19 tools, GLM-5.3 agent loop, HTTP/SSE API, MCP server, CLI, demo |
| Web app | `web/` (`web/README.md`) | React PWA: desktop three-pane and phone layouts, live values, approvals (hold for tier C), IO checkout, field tab; mock backend |
| Server deployment | `deploy/` | systemd unit, production `hub.yaml`, starter `site.yaml`, mosquitto mutual-TLS config and ACL, `mqtt-pki.sh`; tested together in a staged layout |
| Firmware coordination | `docs/SESSION_NOTES.md` | MQTT M1-M3 done (fw 0.3.0). BACnet B1-B8 requested, not yet answered. |

Commands: `cd hub && .venv/bin/pytest -q`; `cd web && pnpm typecheck && pnpm test && pnpm build && pnpm e2e && pnpm e2e:real`.

## How it was built and checked

Contracts first (`hub/src/uc_hub/core`, the driver, node, LLM and tool
interfaces, `API.md`, `SITE.md`), then seven modules implemented in parallel.
Integration followed in three steps: runtime and tools; agent, API and demo;
web against the real hub. An independent agent reviewed every module and
every step, and proved each defect with a failing test before fixing it. A
final six-lens review (safety, security, protocols, agent loop, API and UI,
reliability) closed the build; each of its findings was reproduced before it
was fixed.

## Verification

Filled in at the handoff commit; see the end of this file.

## Backlog, in suggested order

1. Validate and tune the agent with the real GLM-5.3 (see above).
2. Hardware bring-up: an MQTT board on the server's broker, then BACnet-uc boards
   once the firmware runs. Compare the simulator with real firmware wherever
   they differ.
3. Firmware answers to B1-B8 (identify, device-side leases, hwid/mac, units,
   paging). The hub already uses them when present.
4. AI-written control logic: `logic_write`, `logic_build` (clang wasm32
   against `hub/third_party/bacnet_uc.h`), `logic_unit_test` with native
   stubs, then simulation. Also the "simulation first" rule (DESIGN.md
   section 10, rule 5), which is not enforced yet.
5. Firmware updates over the SMP image group, and the SMP serial and DTLS
   transports.
6. BACnet/IP across subnets (BBMD/foreign device), Sparkplug B and Home
   Assistant discovery.
7. Web: Trends and Logic tabs, command palette, nameplate photos
   (`glm-5.3-flash`), Web Push for approvals, paging for large point lists.
8. OIDC sign-in instead of static tokens; a relay for several sites.
