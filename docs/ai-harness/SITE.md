# Site manifest and hub configuration

The hub reads two files:

- **`site.yaml`** is the desired state of the building (the `Site`
  document). The agent edits it through `manifest_edit`, and it is
  versioned in the hub database.
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
    command_topic: sensors/r204/co2/set   # optional; makes points with writable: true writable

bridges:                            # cross-protocol copies, run by the gateway
  - from: r204-co2/co2              # device/obj or site/device/obj
    to: r204-ctl/analog-value:20
    max_age_s: 120                  # relinquish the destination when the source is older
    scale: 1.0
    offset: 0.0
    priority: 12                    # default policy.agent_write_priority; ignored for non-commandable destinations

tags:                               # point id (site-relative or full) -> tags
  r204-ctl/analog-input:1: [Zone_Air_Temperature_Sensor]

safety:                             # point id -> critical | life-safety
  ahu1-ctl/binary-output:9: life-safety

policy:
  agent_write_priority: 12          # 9..16; the agent never writes at 1..8
  default_lease_s: 300
  max_lease_s: 3600
  deny: [ahu1-ctl/binary-output:*]  # fnmatch patterns over point ids; read-only for the agent
  max_writes_per_minute: 30
  max_devices_per_stage: 5
  approval_ttl_s: 1800
```

Rules the schema cannot express, which the validator checks:

- Device names are unique across `system.nodes` and `external_devices`.
- Every `placement` key is a device and every value is a space.
- `parent` references resolve and have no cycles.
- `links` and `bridges` endpoints name known devices. `bridges.to` must be
  on a bacnet-uc or bacnet-ip device.
- `system.apps[].node` names a node.
- `tags` and `safety` keys parse as point ids of known devices.

## `hub.yaml`

```yaml
site_file: site.yaml                # relative to this file
data_dir: ./data                    # SQLite database (uc-hub.db) and result blobs
listen: {host: 127.0.0.1, port: 8080}
web_dir: ../web/dist                # optional; the built browser app, served at /

auth:
  tokens:                           # empty or missing = dev mode (user "dev", all roles, loopback only)
    - {user: ilija, roles: [admin], token_env: UC_HUB_TOKEN_ILIJA}
    - {user: tech1, roles: [operator], token: "literal-for-tests-only"}

llm:
  provider: zai                     # zai | scripted | none
  model: glm-5.3
  base_url: https://api.z.ai/api/paas/v4
  api_key_env: ZAI_API_KEY
  reasoning_effort: high            # sent as reasoning_effort
  max_tokens: 8192
  timeout_s: 120
  script: null                      # scripted provider: path to a YAML script (tests/demo)

agent:
  max_tool_calls: 60
  max_wall_s: 1200
  result_inline_limit: 4000         # bytes of a tool result sent to the model before it becomes a handle

drivers:
  bacnet_uc:
    timeout_s: 1.5
    retries: 3
    poll_interval_s: 2.0            # live values of watched points
    uc_link_wasm: null              # path to the stock uc-link.wasm; links are skipped with a warning when null
  mqtt:
    host: 127.0.0.1
    port: 8883
    tls: {ca: certs/ca.crt, cert: null, key: null}   # omit tls for plain MQTT (tests only)
    username: null
    password_env: null
    client_id: uc-hub
  bacnet_ip:
    interface: 0.0.0.0              # local address to bind
    port: 47808
    device_instance: 4194000        # the hub's own BACnet device
    device_name: uc-hub
```
