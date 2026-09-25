# Management protocol (SMP / MCUmgr)

The management plane of every BACnet-uc node is Zephyr's **MCUmgr** using
the **Simple Management Protocol (SMP) version 2**. This document is the
normative contract between the firmware (`firmware/src/mgmt/`) and every
client (the MCP harness in `harness/`, `mcumgr`, `smpmgr`, AuTerm, ...).

## Transports

| Transport | Where | Notes |
|-----------|-------|-------|
| UDP/IPv4, port **1337** | all boards with Ethernet, native_sim | `CONFIG_MCUMGR_TRANSPORT_UDP`, MTU 1024. Optional DTLS (`CONFIG_MCUMGR_TRANSPORT_UDP_DTLS`), see [security](security.md). |
| Shell UART | all boards | `CONFIG_MCUMGR_TRANSPORT_SHELL`: SMP frames multiplexed with the interactive shell on the console UART (115200 8N1). Frames start with `0x06 0x09` (first) / `0x04 0x14` (continuation), base64 body, CRC16-CCITT trailer. On native_sim the console UART is the pseudo terminal printed at start (`uart connected to pseudotty: /dev/pts/N`). |

### Size limits

| Limit | Value | Source |
|-------|-------|--------|
| SMP buffer (header + CBOR, request and response) | **1152 bytes** | `CONFIG_MCUMGR_TRANSPORT_NETBUF_SIZE`, reported by OS `mcumgr_params` as `buf_size` (`buf_count` 2) |
| UDP request datagram | **1024 bytes** | `CONFIG_MCUMGR_TRANSPORT_UDP_MTU` (receive buffer; longer datagrams are cut and fail to decode) |
| UDP response datagram | up to 1152 bytes | one SMP buffer |
| Shell request | 1152 bytes | the node buffers 16 received lines of up to 127 characters (`CONFIG_MCUMGR_TRANSPORT_SHELL_RX_BUF_COUNT`); a full request is 13 lines and may be sent without pacing |
| File path (FS group, configuration) | 95 characters | `CONFIG_MCUMGR_GRP_FS_PATH_LEN`, `UC_PATH_MAX` (96 incl. NUL) |
| Configuration document | 8192 bytes | `CONFIG_UC_CONFIG_DOC_MAX` |
| Text values (`<value>`, names) | 96 bytes in responses, 63 bytes in `prop_write` | truncated on a UTF-8 character boundary |

Clients size their requests from `buf_size` and the transport: at most 1024
bytes over UDP. Lists that may not fit one response (`uc_app list`,
`uc_io catalog`, `uc_node objects`) are paged, see below. An `uc_app install`
manifest with many or long `params` can exceed the request limit: write the
entry into `apps.json` instead (FS upload of `/lfs/cfg/apps.json.new`, then
`uc_node reload {"doc": "apps"}`).

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
| OS | 0 | `echo` (0), `reset` (5), `mcumgr_params` (6), `os info` (7); `bootloader info` (8) only in MCUboot builds |
| Image | 1 | **MCUboot builds only** (`west build --sysbuild`, or `overlay-mcuboot.conf`): firmware update (`state` 0, `upload` 1, `erase` 5) with MCUboot swap. Plain builds answer `rc` 8 (`MGMT_ERR_ENOTSUP`) |
| Statistics | 2 | stack/net statistics |
| File system | 8 | `file` (0) upload/download (files > 64 KiB supported), `status` (1), `hash` (2, `sha256` and `crc32`), `close` (4) |
| Shell | 9 | `exec` (0), remote shell commands (`uc ...`, `fs ...`, `net ...`, `kernel ...`) |

The Settings group (3) is **not** enabled: the node keeps its configuration
in the JSON documents below, not in Zephyr settings.

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

### Staged documents (`*.json.new`)

Uploading straight over the active document leaves a half-written file on
the node when the transfer is interrupted, and an invalid upload is only
detected at the reload. Clients should therefore **stage** the document:

1. FS upload to `<document>.new`, e.g. `/lfs/cfg/io.json.new`;
2. `uc_node reload {"doc": "io"}` (or `"all"`; the shell equivalent is
   `uc cfg reload io`).

For every requested document the reload first looks for `<document>.new`:

| Staged file | Result |
|-------------|--------|
| absent | the active document is re-read (previous behaviour) |
| valid (JSON syntax, schema, table limits, size <= 8192 bytes) | renamed over the active document in one atomic LittleFS commit, then used and applied |
| invalid | **deleted**; the active document and the running configuration stay; the reload of that document fails with rc `INVALID` |
| unreadable / rename failed (I/O) | left in place for another attempt; rc `IO` (or `NO_MEM`) |

A staged document that is valid but cannot be applied (e.g. an `io.json`
point on an unknown channel: rc `NOT_FOUND`) has been activated already,
like a directly uploaded one. Staged documents found at boot are activated
the same way before the configuration is loaded (an invalid one is deleted
and the active document is used). A staged `device.json` with a changed
instance, network or port still needs a reboot (`reboot_required`).

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
| 0 | read | `list` | `{"offset"?: uint, "count"?: uint}` | `{"total": uint, "apps": [<app status>...]}` |
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

`list` returns the installed apps in `apps.json` order from `offset`
(default 0): at most `count` entries (default: all) and fewer when the
response is full. `total` is the number of installed apps; a client that
receives fewer than `total - offset` entries requests the next page with
`offset + len(apps)`. An `offset` >= `total` returns an empty list.

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
| 0 | read | `catalog` | `{"offset"?: uint, "count"?: uint}` | `{"board": tstr, "total": uint, "channels": [<channel>...]}` |
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

`catalog` pages like `uc_app list`: channels in id order from `offset`
(default 0), at most `count` (default: all) and fewer when the response is
full; `total` is the number of channels of the board.

Values are raw engineering values of the channel: `di`/`do` 0 or 1, `ai`
millivolts, `ao` percent 0..100. `force` overrides what the IO scan sees
for an input (or drives an output regardless of its BACnet object) until
released; on `sim` channels it simply sets the simulated value. This is how
the harness injects stimuli in tests.

### Group 66 `uc_node` - node information and configuration

| Cmd | Op | Name | Request | Response |
|-----|----|------|---------|----------|
| 0 | read | `info` | `{}` | `<node info>` |
| 1 | write | `reload` | `{"doc": "device" \| "io" \| "apps" \| "all"}` | `{"reboot_required": bool}` (+ `"err"` on failure) |
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
"pv"?: <value>}` where owner is `"system"`, `"io"` or `"app:<name>"`
(`"app:#<slot>"` when the owning app is gone).

`objects` returns the local objects in object-list order from `offset`
(default 0): at most `count` entries (**default 8**) and fewer when the
response is full. `total` is the number of local objects; the next page
starts at `offset + len(objects)`.

#### Values

`<value>` in responses is the natural CBOR type of the BACnet value:

| BACnet datatype | CBOR |
|-----------------|------|
| REAL, DOUBLE | float (shortest exact width) |
| UNSIGNED, ENUMERATED | uint (binary present values: 0 inactive, 1 active) |
| SIGNED | int |
| BOOLEAN | bool |
| CharacterString | tstr (<= 96 bytes; non-UTF-8 character sets: ASCII, other bytes `?`) |
| OctetString | bstr (<= 96 bytes) |
| NULL | null |
| anything else (BIT STRING, DATE, TIME, BACnetObjectIdentifier, ...) | tstr: the BACnet stack's text form (`bacapp_snprintf_value`), <= 96 bytes, e.g. `"{false,false,false,false}"` for status-flags, `"(analog-input, 1)"` for an object identifier |

`prop_read` without `"index"` on a BACnetARRAY or BACnetLIST property
(object-list, priority-array, state-text, ...) returns the whole property
as a CBOR array of `<value>`; when it does not fit one response the rc is
`LIMIT`: read the size with `"index": 0` and the elements with
`"index": 1..size`. An element without an application-tagged encoding
(constructed data) gives rc `INVALID`. `"index"` on a property that is not
an array is `INVALID`.

`prop_write` takes a single `<value>` (no arrays; write one element of an
array with `"index"`):

| CBOR | Written as |
|------|------------|
| uint, int, float | the datatype of the property: REAL (finite, within the float range), DOUBLE, UNSIGNED / ENUMERATED (integral, >= 0, <= 2^32-1), SIGNED (integral, 32-bit); binary present value, relinquish-default and priority-array: 0 or 1 only |
| bool | as the number 0 / 1 |
| tstr | CharacterString (<= 63 bytes) |
| null | NULL (relinquishes the given priority) |

NaN, infinities and numbers that the datatype cannot represent exactly are
rejected with rc `INVALID`; a number for a property that is not numeric is
`INVALID` as well. The same conversion applies to the WebAssembly host API
(`uc_prop_write`, `uc_remote_write`: `UC_ERR_INVALID`).

Object types and properties may be given as the BACnet text names used by
the BACnet stack (`"analog-value"`, `"present-value"`) or as numbers.

#### reload

`reload` re-reads the named document(s), activating a staged
`<document>.new` first (see [Staged documents](#staged-documents-jsonnew)):
`io` re-creates IO-bound objects, `apps` stops removed/changed apps and
starts autostart apps, `device` applies name/description/location and
`log.level` immediately and reports `reboot_required` for network, port and
instance changes. With `"all"` every document is loaded and applied on its
own: a rejected document keeps its active configuration and does not stop
the others; the response carries `reboot_required` and, if a document
failed, the first error as `"err"`.
