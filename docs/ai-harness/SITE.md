# Site manifest and hub configuration

The hub reads two files:

- **`site.yaml`** is the desired state of the building (the `Site`
  document). On the first start the hub imports it as manifest revision 1
  and makes it live; from then on the database is authoritative (the agent
  edits a draft through `manifest_edit`, `plan` and `apply` make a revision
  live), and a `site.yaml` that differs from the live revision only gets a
  warning in the log.
- **`hub.yaml`** is how this gateway runs: listen address, credentials, LLM,
  broker. It is not versioned and never shown to the model.

## `site.yaml` (`apiVersion: bacnet-uc/v1`, `kind: Site`)

JSON Schema: `hub/src/uc_hub/manifest/schemas/site.schema.json`. It embeds
`system.schema.json` (vendored unchanged from the BACnet branch, together
with `device`, `io` and `apps` schemas).

```yaml
apiVersion: bacnet-uc/v1
kind: Site
metadata:
  name: hq                          # ^[a-z0-9][a-z0-9-]{0,62}$, first part of every point id
  description: HQ building

spaces:                             # tree via parent; ids ^[a-z0-9][a-z0-9-]{0,30}$
  - {id: f2, name: Floor 2}
  - {id: r204, name: Room 204, parent: f2}
  - {id: plant, name: Plant room}

placement:                          # device name -> space id (any protocol)
  r204-ctl: r204
  ahu1-ctl: plant
  r204-co2: r204

system:                             # optional; a System document, unchanged
  apiVersion: bacnet-uc/v1
  kind: System
  metadata: {name: hq-uc}
  nodes:                            # BACnet-uc nodes, managed by the hub (driver bacnet-uc)
    - name: r204-ctl
      board: nucleo_f767zi
      transport: {kind: udp, host: 10.0.2.51, port: 1337}
      device: {instance: 2041, name: R204 Controller, location: Room 204}
      io:
        - {channel: ai0, type: analog-input, instance: 1, name: R204 Temp, units: degrees-celsius}
  apps:
    - {name: thermostat, node: r204-ctl, wasm: apps/thermostat.wasm, perms: [bacnet.local],
       params: {setpoint: 21.5}}
  links:
    - {from: ahu1-ctl/analog-value:3, to: r204-ctl/analog-value:10}
  tests:
    - name: valve opens when cold
      steps:
        - force: {node: r204-ctl, channel: ai0, value: 650}
        - expect: {point: r204-ctl/analog-output:1, op: gt, value: 50, within_ms: 5000}
        - release: {node: r204-ctl, channel: ai0}

external_devices:                   # devices the hub maps but does not configure
  - name: ahu1-ctl
    protocol: bacnet-ip
    address: 10.0.2.10              # host or host:port (default port 47808)
    device_instance: 100
    points:                         # optional allow-list; default = every object in object-list (max 256)
      - {obj: analog-value:3, name: Supply Air Temp}
  - name: r204-node
    protocol: mqtt
    profile: mqtt_tls               # this repository's apps/mqtt_tls
    client_id: zephyr-0a1b2c3d      # topic prefix <topic_root>/<client_id>/
    topic_root: bacnet-uc           # default bacnet-uc
  - name: r204-co2
    protocol: mqtt
    profile: generic-json
    topic: sensors/r204/co2
    points:
      - {id: co2, path: $.ppm, units: parts-per-million, datatype: real}
      - {id: battery, path: $.bat.pct, units: percent}
      # trust_retained: true            # the device publishes this point retained, on change only
    command_topic: sensors/r204/co2/set   # optional; makes points with writable: true writable

bridges:                            # cross-protocol copies, run by the gateway
  - from: r204-co2/co2              # device/obj or site/device/obj
    to: r204-ctl/analog-value:20
    max_age_s: 120                  # default 300; relinquish the destination when the source is older
    scale: 1.0
    offset: 0.0
    priority: 12                    # default policy.agent_write_priority, never above it (a lower number);
                                    # ignored for non-commandable destinations

tags:                               # point id (site-relative or full) -> tags
  r204-ctl/analog-input:1: [Zone_Air_Temperature_Sensor]

safety:                             # point id -> critical | life-safety
  ahu1-ctl/binary-output:9: life-safety

policy:
  agent_write_priority: 12          # 9..16; the agent never writes at 1..8
  default_lease_s: 300             # 1..86400
  max_lease_s: 3600                 # 1..604800
  deny: [ahu1-ctl/binary-output:*]  # fnmatch patterns over point ids; read-only for the agent
  max_writes_per_minute: 30
  max_devices_per_stage: 5          # apply stage size; a plan with more targets needs an admin
  approval_ttl_s: 1800              # 60..604800
```

Every key the schema accepts is shown above, except these:

- `spaces[]`: `id`, `name`, `parent`.
- `external_devices[]` of any protocol: `name`, `protocol`, `description`,
  `equipment`. BACnet/IP: `address`, `device_instance`, `points[]` with
  `obj`, `name`, `units`, `description`. MQTT `mqtt_tls`: `client_id`,
  `topic_root`. MQTT `generic-json`: `topic` (+ and # allowed),
  `command_topic`, `points[]` with `id`, `path` (JSONPath, default
  `$['<id>']`), `name`, `units`, `datatype` (`real`, `int`, `bool`, `enum`,
  `string`; default `real`), `kind`, `writable`, `command_topic`,
  `trust_retained`, `description`.
- `system`: the `System` document of `system.schema.json`, unchanged
  (`nodes`, `apps`, `links`, `tests`).

Rules the schema cannot express, which the validator checks:

- Device names are unique across `system.nodes` and `external_devices`.
- Every `placement` key is a device and every value is a space.
- `parent` references resolve and have no cycles.
- `links` and `bridges` endpoints name known devices. `bridges.to` must be
  on a bacnet-uc or bacnet-ip device.
- `system.apps[].node` names a node.
- `tags` and `safety` keys parse as point ids of known devices.
- Bridges and test writes use a priority from `policy.agent_write_priority`
  to 16: the gateway writes under the agent's rules.

How the hub uses the manifest:

- **Bridges** copy `value * scale + offset` to the destination when the
  source changes and every `max_age_s / 2`, under the agent's write rules
  (life-safety points, deny patterns, priorities) but outside its rate
  limit. BACnet-uc destinations also get `max_age_s` as a lease of their
  own. The destination is relinquished when the source is older than
  `max_age_s`, not of good quality, or only known from a retained MQTT
  message (the broker's stored copy, of unknown age) until the device sends
  a live one; points with `trust_retained: true` are current when retained.
  It is also relinquished when the policy stops admitting the value (a
  later revision adds a deny pattern over the destination). The bridge set
  is relinquished when the hub stops.
- **Tags**: the points listed under `tags` are watched all the time, so
  their values are in `point_history`.
- **Safety**: the stricter of a point's own class and this map counts. Only
  an admin may add or remove a `life-safety` mark in the draft, and only an
  admin may approve a plan that does.
- **Tests** run on the live site as agent actions: every write and force
  goes through the policy and holds a lease, and only present values are
  written.

## `hub.yaml`

Loaded by `uc_hub.runtime.config`. Unknown keys are refused, so a misspelt
setting fails at startup. `uc-hub serve --host/--port` override `listen`
and are checked the same way. One hub runs per `data_dir`: a second process
on the same directory (`serve`, `mcp` or `plan` while a hub runs) is refused. Relative paths (`site_file`, `data_dir`,
`web_dir`, the MQTT TLS files, `drivers.bacnet_uc.uc_link_wasm` and
`llm.script`) are relative to the directory of `hub.yaml`; `~` is expanded.
Secrets come from a literal value or an environment variable (`*_env`),
are never logged and never appear in error messages. Only the keys the file
sets are passed to a driver, so every driver keeps its own defaults (listed
in its module docstring). Without `auth.tokens` (dev mode) `listen.host`
must be a loopback address (`127.0.0.1`, `::1` or `localhost`); anything
else is refused at startup unless `--insecure-listen` is given. A driver runs when its section is present or the
manifest has a device of its protocol; the bacnet-uc driver always runs.

```yaml
site_file: site.yaml                # imported as revision 1 on the first start
data_dir: ./data                    # SQLite database (uc-hub.db) and result blobs
listen: {host: 127.0.0.1, port: 8080}
web_dir: ../web/dist                # optional; the built browser app, served at /

auth:
  tokens:                           # empty or missing = dev mode (user "dev", all roles, loopback only)
    - {user: ilija, roles: [admin], token_env: UC_HUB_TOKEN_ILIJA}
    - {user: tech1, roles: [operator], token: "literal-for-tests-only"}   # exactly one of token, token_env

llm:
  provider: zai                     # zai | scripted | none (default none)
  model: glm-5.3
  base_url: https://api.z.ai/api/paas/v4
  api_key_env: ZAI_API_KEY          # or api_key: <literal>
  reasoning_effort: high            # sent as reasoning_effort
  max_tokens: 8192
  temperature: null
  thinking: true
  clear_thinking: null
  timeout_s: 120
  connect_timeout_s: 10
  max_retries: 3
  script: null                      # scripted provider: a YAML script path, "demo" or an inline script
  delay_s: 0.015                    # scripted provider: pause per streamed chunk
  chunk_size: 12                    # scripted provider: characters per chunk

agent:
  max_tool_calls: 60                # per turn (one user message and what follows)
  max_wall_s: 1200                  # per turn, not counting the time waiting for approvals and answers
  result_inline_limit: 4000         # bytes of a tool result sent to the model before it becomes a handle

mcp:                                # uc-hub mcp: who MCP clients act as
  user: mcp
  roles: [operator]                 # tier L and C calls still need a person's approval in the web app
  approval_wait_s: 600              # how long an MCP call waits for that decision; then the approval expires

drivers:
  bacnet_uc:
    timeout_s: 1.5                  # per SMP request
    retries: 3
    mtu: null                       # SMP buffer size; default: the node's mcumgr_params, at most 1024
    max_inflight: 4
    objects_page: 8
    poll_interval_s: 2.0            # live values of watched points
    refresh_s: 30                   # every watched value is published again this often
    heartbeat_s: 15                 # nodes without watched points are probed this often
    read_concurrency: 8
    units_cache_s: 300
    discover_broadcast: 255.255.255.255:1337   # a list, or empty to disable
    sim_addresses: {}               # node name -> host:port for transport: sim nodes
    uc_link_wasm: null              # path to the stock uc-link.wasm; links are skipped with a warning when null
  mqtt:
    host: 127.0.0.1
    port: 8883                      # default 8883 with tls, 1883 without
    tls: {ca: certs/ca.crt, cert: null, key: null, insecure: false}   # true = system trust store; omit for plain MQTT
    username: null
    password_env: null              # or password: <literal>
    client_id: uc-hub
    keepalive_s: 30
    timeout_s: 10
    reconnect_min_s: 0.5
    reconnect_max_s: 30
    stale_after_s: 300              # a periodic value older than this reads stale
    command_timeout_s: 5
    max_payload_bytes: 262144
  bacnet_ip:
    interface: 0.0.0.0              # local address to bind; a.b.c.d/nn also sets the broadcast address
    port: 47808
    broadcast: null                 # host[:port] for the global Who-Is; null disables it
    discover_targets: []            # addresses that also get a directed Who-Is
    device_instance: 4194000        # the hub's own BACnet device
    device_name: uc-hub
    vendor_identifier: 999
    timeout_s: 2.0
    retries: 1
    poll_interval_s: 5              # points whose COV subscription is refused
    refresh_s: 60
    cov: true
    cov_lifetime_s: 300
    cov_confirmed: false
    cov_process_id: 1
    max_objects: 256
    rpm_max_properties: 64
    device_concurrency: 2
```
