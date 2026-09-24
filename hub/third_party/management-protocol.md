# Management protocol (SMP / MCUmgr)

The management plane of every BACnet-uc node is Zephyr's **MCUmgr** using
the **Simple Management Protocol (SMP) version 2**. This document is the
normative contract between the firmware (`firmware/src/mgmt/`) and every
client (the MCP harness in `harness/`, `mcumgr`, `smpmgr`, AuTerm, ...).

## Transports

| Transport | Where | Notes |
|-----------|-------|-------|
| UDP/IPv4, port **1337** | all boards with Ethernet, native_sim | `CONFIG_MCUMGR_TRANSPORT_UDP`, MTU 1024. Optional DTLS (`CONFIG_MCUMGR_TRANSPORT_UDP_DTLS`), see [security](security.md). |
| Shell UART | all boards | `CONFIG_MCUMGR_TRANSPORT_SHELL`: SMP frames multiplexed with the interactive shell on the console UART (115200 8N1). Frames start with `0x06 0x09` (first) / `0x04 0x14` (continuation), base64 body, CRC16-CCITT trailer. |

## Frame format

Every request and response is an 8-byte header followed by a CBOR map.

| Byte | Field | Value |
|------|-------|-------|
| 0 | `Res(3) Ver(2) Op(3)` | `Op`: 0 read, 1 read-rsp, 2 write, 3 write-rsp; `Ver` = 1 (SMP v2) |
| 1 | Flags | 0 |
| 2-3 | Length | CBOR payload length, big endian |
| 4-5 | Group ID | big endian |
| 6 | Sequence | echoed in the response |
| 7 | Command ID | |

Errors (SMP v2): the response map contains
`{"err": {"group": <group id>, "rc": <group rc>}}`. Transport/generic
failures use the legacy `{"rc": <mcumgr_err_t>}` form.

## Standard groups enabled

| Group | ID | Used for |
|-------|----|----------|
| OS | 0 | `echo` (0), `reset` (5), `mcumgr_params` (6), `os info` (7), `bootloader info` (8) |
| Image | 1 | firmware update (`state` 0, `upload` 1, `erase` 5) with MCUboot swap |
| Statistics | 2 | stack/net statistics |
| Settings | 3 | Zephyr settings keys (`bacnet/...` from the BACnet settings subsystem) |
| File system | 8 | `file` (0) upload/download, `status` (1), `hash` (2, sha256), `close` (4) |
| Shell | 9 | `exec` (0), remote shell commands |

File system layout on the node (mount point `/lfs`):

```
/lfs/cfg/device.json   device identity, network, BACnet options   (schemas/device.schema.json)
/lfs/cfg/io.json       IO point -> BACnet object mapping          (schemas/io.schema.json)
/lfs/cfg/apps.json     installed WebAssembly applications         (schemas/apps.schema.json)
/lfs/apps/<name>.wasm  application binaries (.aot for AOT builds)
/lfs/data/<app>/<key>  per-application key/value store
/lfs/log/log.NNNN      rotating log files (Zephyr LOG_BACKEND_FS)
```

A configuration change is: upload the document with the FS group, then send
`uc_node reload` for that document.

## Custom groups

Group IDs start at `MGMT_GROUP_ID_PERUSER` (64). All keys are CBOR text
strings; "uint"/"int"/"float"/"bool"/"tstr"/"bstr" are CBOR major types
(`float` accepts any CBOR float width or an integer on input).

### Group rc values (all custom groups)

| rc | Name | Meaning |
|----|------|---------|
| 0 | `OK` | |
| 1 | `UNKNOWN` | unexpected internal error |
| 2 | `INVALID` | malformed or missing request field |
| 3 | `NOT_FOUND` | app, channel, object, file or document not found |
| 4 | `EXISTS` | name/object already in use |
| 5 | `BUSY` | resource busy, retry |
| 6 | `NO_MEM` | out of memory / WASM pool exhausted |
| 7 | `IO` | file system or hardware error |
| 8 | `STATE` | operation not valid in the current state |
| 9 | `VERIFY` | hash mismatch, invalid module, ABI version mismatch |
| 10 | `PERM` | operation not permitted |
| 11 | `UNSUPPORTED` | not built into this firmware |
| 12 | `LIMIT` | table full (apps, points, subscriptions) |

### Group 64 `uc_app` - WebAssembly applications

| Cmd | Op | Name | Request | Response |
|-----|----|------|---------|----------|
| 0 | read | `list` | `{}` | `{"apps": [<app status>...]}` |
| 1 | write | `install` | `<app manifest>` | `{}` |
| 2 | write | `start` | `{"name": tstr}` | `{}` |
| 3 | write | `stop` | `{"name": tstr}` | `{}` |
| 4 | write | `remove` | `{"name": tstr, "delete_file"?: bool}` | `{}` |
| 5 | read | `status` | `{"name": tstr}` | `<app status>` |

`<app manifest>` (same fields as an `apps.json` entry, but `params` is a map):

```
{
  "name": tstr,            ; 1..23 chars [a-z0-9_-], unique on the node
  "file": tstr,            ; absolute path of an uploaded module, e.g. "/lfs/apps/pid.wasm"
  "autostart"?: bool,      ; default true
  "period_ms"?: uint,      ; tick period, default 1000, 0 = no ticks
  "heap_kb"?: uint,        ; WASM app heap (libc-builtin malloc), default 8
  "stack_kb"?: uint,       ; WASM operand/aux stack, default 4
  "perms"?: [tstr],        ; subset of "bacnet.local", "bacnet.remote", "io", "kv"
  "params"?: {tstr: tstr}, ; deployment parameters, read with uc_param_get()
  "sha256"?: bstr,         ; 32 bytes; verified against the file when present
  "restart"?: bool         ; stop a running instance of the same name and start again
}
```

`install` validates the file (exists, size <= `CONFIG_UC_APP_MAX_FILE_SIZE`,
optional sha256, WASM/AOT magic), stores the entry in `/lfs/cfg/apps.json`
and starts it when `autostart` is true (or restarts it with `restart`).
Installing an existing name replaces its entry (rc `STATE` if it is running
and `restart` is not true).

`<app status>`:

```
{
  "name": tstr, "file": tstr,
  "state": "stopped" | "starting" | "running" | "failed",
  "autostart": bool, "period_ms": uint, "heap_kb": uint, "stack_kb": uint,
  "perms": [tstr],
  "ticks": uint,           ; completed uc_app_tick calls since start
  "events": uint,          ; delivered cov/write events since start
  "errors": uint,          ; host-call errors + traps since start
  "last_error": tstr,      ; "" or the last trap / load error message
  "uptime_ms": uint        ; 0 when not running
}
```

### Group 65 `uc_io` - IO channels

| Cmd | Op | Name | Request | Response |
|-----|----|------|---------|----------|
| 0 | read | `catalog` | `{}` | `{"board": tstr, "channels": [<channel>...]}` |
| 1 | read | `read` | `{"name"?: tstr}` | `{"values": {tstr: float}}` (one or all channels) |
| 2 | write | `write` | `{"name": tstr, "value": float}` | `{}` (outputs only, rc `PERM` for inputs) |
| 3 | write | `force` | `{"name": tstr, "value": float}` or `{"name": tstr, "release": true}` | `{}` |

`<channel>`:

```
{
  "id": uint, "name": tstr, "desc": tstr,
  "kind": "di" | "do" | "ai" | "ao",
  "hw": "gpio" | "adc" | "pwm" | "sim",
  "forced": bool,
  "object"?: {"type": tstr, "instance": uint}   ; present when bound in io.json
}
```

Values are raw engineering values of the channel: `di`/`do` 0 or 1, `ai`
millivolts, `ao` percent 0..100. `force` overrides what the IO scan sees
for an input (or drives an output regardless of its BACnet object) until
released; on `sim` channels it simply sets the simulated value. This is how
the harness injects stimuli in tests.

### Group 66 `uc_node` - node information and configuration

| Cmd | Op | Name | Request | Response |
|-----|----|------|---------|----------|
| 0 | read | `info` | `{}` | `<node info>` |
| 1 | write | `reload` | `{"doc": "device" \| "io" \| "apps" \| "all"}` | `{"reboot_required": bool}` |
| 2 | read | `objects` | `{"offset"?: uint, "count"?: uint}` | `{"total": uint, "objects": [<object>...]}` |
| 3 | read | `prop_read` | `{"type": tstr\|uint, "instance": uint, "prop": tstr\|uint, "index"?: int}` | `{"value": <value>}` |
| 4 | write | `prop_write` | `{"type": tstr\|uint, "instance": uint, "prop": tstr\|uint, "value": <value>, "priority"?: uint, "index"?: int}` | `{}` |

`<node info>`:

```
{
  "fw": tstr,              ; firmware version, e.g. "0.1.0"
  "board": tstr,           ; Zephyr board target
  "api": uint,             ; UC_API_VERSION implemented by the host
  "device": {"instance": uint, "name": tstr},
  "net": {"ipv4": tstr, "bacnet_port": uint},
  "uptime_s": uint,
  "fs": {"ready": bool, "total": uint, "free": uint},
  "bacnet": {"packets": uint, "objects": uint},
  "apps": {"installed": uint, "running": uint},
  "wasm": {"interp": bool, "aot": bool, "aot_target": tstr,
           "pool_total": uint, "pool_free": uint}
}
```

`<object>`: `{"type": tstr, "instance": uint, "name": tstr, "owner": tstr,
"pv"?: <value>}` where owner is `"system"`, `"io"` or `"app:<name>"`.

`<value>` is the natural CBOR type of the BACnet value: float for REAL and
DOUBLE, uint/int for UNSIGNED/SIGNED/ENUMERATED (binary PVs are 0/1), bool
for BOOLEAN, tstr for CharacterString, null for NULL. `prop_write` encodes a
CBOR number into the datatype the property expects, a tstr into a
CharacterString and null into a NULL (relinquish when a priority is given).
Object types and properties may be given as the BACnet text names used by
the BACnet stack (`"analog-value"`, `"present-value"`) or as numbers.

`reload` re-reads the named document(s): `io` re-creates IO-bound objects,
`apps` stops removed/changed apps and starts autostart apps, `device`
applies name/description/location immediately and reports
`reboot_required` for network, port and instance changes.
