# uc-hub

The site gateway and AI harness for BACnet-uc. One hub runs per building,
on the building network. It:

- talks to the devices: BACnet-uc boards over SMP, third-party BACnet/IP
  controllers, and MQTT devices (this repository's `apps/mqtt_tls` and
  generic JSON sensors);
- keeps the site manifest (`site.yaml`, the desired state) in revisions,
  and turns a draft into a plan that a person approves before it is applied;
- runs an AI agent (Z.ai GLM-5.3, or an offline scripted model) whose tools
  can search, read, edit the draft, plan, and, after approval, write under a
  lease, force IO, apply and run the acceptance tests;
- serves the HTTP API and the browser app (`../web`), and the same tools to
  MCP clients.

The safety rules (DESIGN.md section 10) are enforced in code: life-safety
points are never written, the agent writes at its own priority (never at
1 to 8), every live write and force holds a lease, permanent changes only go
through plan, approval and apply, writes are rate limited, and text from
devices reaches the model marked as data.

Design, HTTP contract and file formats: [`../docs/ai-harness/`](../docs/ai-harness/)
(`DESIGN.md`, `API.md`, `SITE.md`).

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/):

```sh
cd hub
uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
```

This installs the `uc-hub` command into `.venv/bin`. For the browser app,
build it once: `cd ../web && pnpm install && pnpm build` (the hub serves
`web/dist`).

## Run the demo

```sh
.venv/bin/uc-hub demo                 # then open http://127.0.0.1:8080/
.venv/bin/uc-hub demo --llm zai       # the real model; needs ZAI_API_KEY
.venv/bin/uc-hub demo --free-ports --port 8090   # next to another demo
export UC_HUB_DEMO_TOKEN=$(openssl rand -hex 16); echo "$UC_HUB_DEMO_TOKEN"
.venv/bin/uc-hub demo --host 0.0.0.0 --token-env UC_HUB_DEMO_TOKEN
                                      # on the LAN, for a phone: open /?token=<that token> once
```

The demo runs everything in one process, on 127.0.0.1: five simulated
BACnet-uc room controllers (`r201-ctl` to `r205-ctl`, SMP on 13201-13205),
a simulated AHU controller on BACnet/IP (47830), mosquitto on a free port
with an `apps/mqtt_tls` node (firmware 0.3.0) and a CO2 sensor (skipped
with a warning when `mosquitto` is not installed), and the hub with
`examples/demo/hub.yaml` and the scripted model. Each start uses a fresh
temporary database. `--free-ports` puts the simulated devices on free ports
instead (the web app's `pnpm e2e:real` runs one demo per test that way).
Without `--token-env` the demo runs in dev mode, which only listens on a
loopback address; with it, every request needs that bearer token (user
`demo`, role admin). Ctrl-C or SIGTERM stops the hub first (it releases the
agent's leases), then the devices and the broker, and removes the working
directory.

Four room controllers are configured already. `r204-ctl` is installed but
empty; in the agent panel try:

- **Commission room 204**: the agent finds the room's devices, sees that
  the board is not configured, places and tags it in a draft, plans (the
  upload of its IO, thermostat and AHU link), applies after your approval
  and, after a second approval (a live call), runs the site's acceptance
  tests.
- **IO checkout** (playbook) for `r201-ctl`: it forces the valve and the
  heater relay one after the other under a lease, asks you what you see,
  and releases them. "Approve for this run" covers the later live calls.
- **What is the temperature in room 201?**

`examples/demo/apps/uc-link.wasm` is a stand-in built from `uc-link.c`: the
simulator emulates uc-link by the file name. Never install it on a real
board.

## Configure a real site

1. Describe the building in a `site.yaml` (`../docs/ai-harness/SITE.md`;
   `examples/demo/site.yaml` is a complete example) and check it:
   `uc-hub validate site.yaml`.
2. Write a `hub.yaml` next to it (SITE.md lists every key): the listen
   address, `auth.tokens` (without tokens the hub runs in dev mode, where
   every request is an admin, and only listens on a loopback address), the
   drivers you need (`bacnet_uc`, `bacnet_ip`, `mqtt` with its broker and
   TLS files), and the model:

   ```yaml
   site_file: site.yaml
   data_dir: ./data
   listen: {host: 0.0.0.0, port: 8080}
   web_dir: ../web/dist
   auth:
     tokens:
       - {user: ilija, roles: [admin], token_env: UC_HUB_TOKEN_ILIJA}
   llm:
     provider: zai
     model: glm-5.3
     api_key_env: ZAI_API_KEY
   drivers:
     bacnet_uc: {uc_link_wasm: /opt/bacnet-uc/uc-link.wasm}
     bacnet_ip: {interface: 10.0.2.5/24}
   ```

3. See what the hub would change on the devices: `uc-hub plan -c hub.yaml`
   (with the hub stopped).
4. Run it: `uc-hub serve -c hub.yaml`. On the first start `site.yaml`
   becomes manifest revision 1; after that the database is authoritative
   and changes go through a draft, a plan and an approved apply.
5. Open the app once as `http://<hub>:8080/?token=<token>`.

**Z.ai key**: create one at z.ai and put it in the environment variable
that `llm.api_key_env` names (`ZAI_API_KEY` by default), for example with
a systemd `EnvironmentFile`. The key never leaves the hub; `GET /api/health`
shows whether it is configured. `llm.provider: none` runs the hub and the
tools without the agent.

## MCP

`uc-hub mcp -c hub.yaml` runs the hub (with its HTTP API and web app) and
serves the tools on stdio, for MCP clients such as Claude Code:

```sh
claude mcp add uc-hub -- /path/to/hub/.venv/bin/uc-hub mcp -c /path/to/hub.yaml
```

Calls run as `mcp.user` with `mcp.roles` (default `mcp` and `operator`).
Each connection is a run of its own, visible in the web app. Tier L and C
calls wait for a person to approve them in the web app (at most
`mcp.approval_wait_s`, 10 minutes by default); when the client stops
waiting (a timeout, a cancel), the approval expires. The hub stops when the
client closes stdin, or on Ctrl-C or SIGTERM (also while a client is
connected): calls in flight get a failed result, then the hub shuts down
in order. Run either `serve` or `mcp` for one `data_dir`, not both.

## Layout (`src/uc_hub/`)

| Package | What it holds |
|---|---|
| `core/` | Contracts shared by every module: point ids, data types, errors, the driver interface |
| `drivers/` | `bacnet_uc` (SMP over UDP), `bacnet_ip` (bacpypes3), `mqtt` (aiomqtt; mqtt_tls and generic-json profiles) |
| `manifest/` | `site.yaml`: loading, validation, JSON Patch, plan, apply with backups and rollback, acceptance test runner |
| `policy/` | Tiers and roles, life-safety and deny rules, priorities, rate limit; leases |
| `store/` | SQLite (revisions, plans, runs, approvals, questions, audit, leases, backups, tests), run event bus, live value hub |
| `llm/` | Z.ai GLM-5.3 provider and a scripted provider (`llm/scripts/demo.yaml`) |
| `runtime/` | `hub.yaml` (`config`), the running site (`site`: drivers, point model, search, history, watches), bridges, leased live control, live tests, manifest revisions and apply, questions, and `Services`, which starts and stops everything in order |
| `tools/` | The agent's tool catalogue and the `ToolRunner` (validation, policy, approval cards, audit, result handles) |
| `agent/` | Runs: the loop between the model and the tools (`loop`), approvals (`approvals`), the system prompt, the site status and the playbooks |
| `api/` | The HTTP API of API.md (FastAPI): auth, JSON endpoints, the event streams (SSE), the web app, and the server whose stop signals shut the hub down in order |
| `mcp_server.py` | The tools over MCP |
| `cli.py` | `uc-hub serve`, `validate`, `plan`, `mcp`, `demo` |
| `demo.py` | The demo: simulated devices for `examples/demo` and a hub on them |
| `sim/` | Simulated BACnet-uc nodes, a BACnet/IP AHU and MQTT devices for tests and demos |

## Tests

```sh
.venv/bin/pytest -q                   # about 1.5 minutes
ruff check src tests
```

Everything runs against the simulators on 127.0.0.1 with ephemeral ports;
no test needs the internet or a model key. The MQTT tests start a local
`mosquitto` (skipped when it is not installed). `tests/test_demo.py` drives
the demo's scenarios through the HTTP API; `cd ../web && pnpm e2e:real`
drives them through the browser app, against a `uc-hub demo` per test.
