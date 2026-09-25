# uc-link - stock point-to-point link application

`uc-link` copies the Present_Value of a source point (local or on another
BACnet device) to a destination object on the node it runs on. The MCP
harness realises every `links:` entry of a system manifest by deploying one
`uc-link` instance per destination node (app name `link`, or `link-<n>` if
more than 8 links target one node) with generated parameters. It is also
useful on its own.

## Parameters (apps.json `params`, all values are strings)

| Key | Value |
|-----|-------|
| `count` | number of links `N`, 1..8 |
| `l0` .. `l<N-1>` | `"<src_device> <src_type> <src_instance> <dst_type> <dst_instance> <mode> <period_ms> <priority> <scale> <offset>"` |

Fields of a link, separated by single spaces:

| Field | Meaning |
|-------|---------|
| `src_device` | BACnet device instance of the source (the local instance for a local source) |
| `src_type`, `src_instance` | source object, numeric BACnet object type (0 = analog-input, ...) |
| `dst_type`, `dst_instance` | destination object on this node, numeric type |
| `mode` | `cov` (SubscribeCOV, host falls back to polling if unsupported) or `poll` |
| `period_ms` | poll period of a `poll` link, the fallback poll period of a `cov` link: 100..3600000 ms (a `cov` link may give 0 for the default 1000 ms); outside that range the link is malformed |
| `priority` | write priority 1..16 for a commandable destination (analog-output, binary-output, multi-state-output), 0 = none. Value objects (analog-value, binary-value, multi-state-value) of BACnet-uc nodes have no priority array and ignore it: use 0 for them. Priority 6 is reserved for Minimum_On/Off: the node rejects writes at 6 to outputs and to analog-value (`UC_ERR_PERM`, logged as a failed write), so never use it |
| `scale`, `offset` | destination = source * scale + offset |

Example: `l0 = "1001 0 1 2 10 cov 1000 0 1 0"` copies analog-input:1 of
device 1001 into analog-value:10 of this node on every COV notification
(priority 0: a value object). `"1001 0 1 1 1 poll 1000 8 1 0"` polls it and
commands analog-output:1 at priority 8.

## Behaviour

- `uc_app_init`: parses all links; a malformed link is logged and skipped.
  A destination object of type analog-value (2), binary-value (5) or
  multi-state-value (19) that does not exist yet is created (owned by the
  app, named `link-<i>`). Other destination types must already exist
  (typically IO-bound outputs from io.json). `cov` links subscribe with a
  lifetime of 300 s.
- `uc_app_on_cov`: writes the transformed value to the destination.
- `uc_app_tick` (period = smallest poll period, min 100 ms): polls due
  `poll` links with `uc_remote_read` (or `uc_prop_read` for a local source)
  and writes the destination when the value changed.
- On a regular stop, commandable destinations the app did not create and
  wrote with a priority are relinquished at that priority; destinations it
  created are deleted by the host.
- Required permissions: `bacnet.local`, `bacnet.remote`.
- Resources: uc-link does not allocate from the app heap; deploy it with
  `heap_kb` 0 (or 4 where a non-zero value is wanted; keep `heap_kb` a
  multiple of 4) and the default `stack_kb` 4.
