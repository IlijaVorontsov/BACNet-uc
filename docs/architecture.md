# Architecture

This document describes how a BACnet-uc node is built: its place in a
building automation network, the software layers of the firmware, the threads
and who owns what, the main data flows, the boot sequence, the memory budget
of each board and how failures are handled.

Source of truth: the module APIs in [`firmware/include/uc/`](../firmware/include/uc/)
and the implementation in [`firmware/src/`](../firmware/src/). Items marked
**Planned** are not implemented.

## 1. System context

```mermaid
flowchart LR
    subgraph site["Building network (IPv4 subnet)"]
        n1["BACnet-uc node<br/>NUCLEO-F767ZI"]
        n2["BACnet-uc node<br/>FRDM-MCXN947"]
        n3["third-party BACnet/IP<br/>controllers"]
        bbmd["BBMD<br/>(other subnets)"]
    end
    bms["BMS / operator workstation<br/>(BACnet client: YABE, BMS head end)"]
    dev["Development host<br/>MCP harness (Python)<br/>west + Zephyr SDK, wasm SDK"]
    agent["AI agent<br/>(MCP client)"]
    syslog["syslog server<br/>(optional)"]

    bms -- "BACnet/IP UDP 47808<br/>RP/RPM/WP/WPM, COV, Who-Is" --> n1
    bms -- "BACnet/IP" --> n2
    n1 <-- "BACnet/IP: app-to-app<br/>RP, WP, SubscribeCOV" --> n2
    n2 <-- "BACnet/IP" --> n3
    n1 -- "foreign device registration" --> bbmd
    dev -- "SMP over UDP 1337<br/>config, files, apps, IO, logs" --> n1
    dev -- "SMP over UDP 1337 or<br/>console UART (shell transport)" --> n2
    dev -- "BACnet/IP (verification)" --> n1
    agent -- "MCP (stdio)" --> dev
    n1 -. "RFC 5424 UDP 514" .-> syslog
```

| Actor | Talks to the node over | Purpose |
|-------|------------------------|---------|
| BMS / operator workstation | BACnet/IP, UDP 47808 (configurable) | Supervision: read/write points, COV, discovery |
| Other BACnet devices, other BACnet-uc nodes | BACnet/IP | Peer data exchange initiated by WebAssembly applications (client side) |
| Development harness (MCP server) | SMP over UDP 1337 or serial; BACnet/IP | Configuration, file transfer, application deployment, IO forcing, log retrieval, verification |
| AI agent | MCP to the harness only | Plans and applies changes through harness tools; never talks to nodes directly |
| Engineer at the bench | Console UART (115200 8N1): shell `uc ...`, log output | Diagnosis, recovery |
| syslog server | UDP 514 (build option `overlay-syslog.conf`) | Central log collection |
| Debug probe | SWD (ST-LINK/V2-1, MCU-Link) | Flashing, debugging |

The harness side is described in [harness-mcp.md](harness-mcp.md) and
[distributed-apps.md](distributed-apps.md); this document covers the node.

### Network interfaces of a node

| Port | Protocol | Direction | Configured by | Notes |
|------|----------|-----------|---------------|-------|
| UDP 47808 | BACnet/IP (Annex J) | in/out, unicast + local broadcast | `device.json` `bacnet.udp_port` | two sockets (unicast, broadcast) opened by bacnet-stack `bip_init()` |
| UDP 1337 | SMP v2 | in | `CONFIG_MCUMGR_TRANSPORT_UDP_PORT` | no authentication in the default build, see [security.md](security.md) |
| UDP 67/68 | DHCPv4 client | out | `device.json` `network.dhcp` | |
| UDP 514 | syslog | out | `overlay-syslog.conf` | optional |
| console UART | shell + SMP shell transport + log output | in/out | devicetree `zephyr,console` / `zephyr,shell-uart` | |

## 2. Software layers

```mermaid
flowchart TB
    subgraph L5["WebAssembly applications (untrusted, uploaded)"]
        a1["app.wasm / app.aot<br/>imports module bacnet_uc"]
    end
    subgraph L4["BACnet-uc modules (firmware/src)"]
        apps["uc_apps<br/>WAMR manager, host API"]
        mgmt["uc_mgmt<br/>SMP groups 64/65/66"]
        shell["uc_shell<br/>'uc' commands"]
        bn["uc_bacnet<br/>node, local objects,<br/>client, COV"]
        io["uc_io<br/>channels, points, scan"]
        cfg["uc_config<br/>JSON documents"]
        net["uc_net<br/>IPv4 bring-up"]
        st["uc_storage<br/>/lfs"]
        cm["uc_common<br/>errors, names, values"]
    end
    subgraph L3["Libraries (west modules)"]
        bstack["bacnet-stack<br/>basic server, BACnet/IP,<br/>services, objects"]
        wamr["WAMR 2.4.5<br/>fast interpreter,<br/>libc-builtin, thread mgr"]
    end
    subgraph L2["Zephyr subsystems"]
        netstack["IPv4/UDP, sockets,<br/>DHCPv4, conn mgr"]
        fs["VFS + LittleFS"]
        log["logging<br/>UART, FS, net backends"]
        mcumgr["MCUmgr / SMP<br/>UDP + shell transports,<br/>OS, FS, stat, shell, img"]
        zsh["shell"]
        misc["JSON, zcbor, PSA SHA-256"]
    end
    subgraph L1["Zephyr kernel and drivers"]
        kern["kernel: threads, timers,<br/>msgq, heap, MPU"]
        drv["GPIO, ADC, PWM, flash,<br/>SPI NOR / FlexSPI NOR,<br/>Ethernet MAC + PHY"]
    end
    a1 --> apps
    apps --> bn & io & cfg & st
    mgmt --> apps & io & bn & cfg & st
    shell --> apps & io & bn & cfg & st
    bn --> bstack & io & cfg & net
    io --> bn & drv
    cfg --> st
    apps --> wamr
    mgmt --> mcumgr
    bstack --> netstack
    st --> fs
    L2 --> L1
```

### Firmware modules

| Module | Sources | Responsibility | Runs in |
|--------|---------|----------------|---------|
| `uc_common` | `src/common/uc_common.c` | errno ↔ `UC_ERR_*` ↔ management rc mapping, BACnet text names ↔ enums, value conversion, name validation, permissions | caller |
| `uc_storage` | `src/storage/uc_storage.c` | mount `/lfs` (format on failure), directory layout, atomic file replace and rename, SHA-256 of files | main, callers |
| `uc_config` | `src/config/uc_config.c` | activate staged `<doc>.json.new` documents, parse `device.json`, `io.json`, `apps.json` into a RAM cache, defaults, runtime log level, `apps.json` encoder | main, SMP work queue, shell |
| `uc_net` | `src/net/uc_net.c` | static IPv4 or DHCPv4 on the default interface; loopback/static address on `native_sim` (plus closing inherited host sockets before a `native_sim` restart) | main |
| `uc_bacnet` | `src/bacnet/uc_bn_{node,local,client,cov}.c` | BACnet thread, stack initialisation, BACnet/IP datalink, executor, local objects with owner table, CreateObject/DeleteObject policy, ReinitializeDevice/DCC password, blocking client, COV subscriptions for applications | BACnet thread (+ thread-safe wrappers) |
| `uc_io` | `src/io/uc_io.c` | devicetree channel table, GPIO/ADC/PWM/simulated channels, forcing, `io.json` point binding, IO scan | BACnet thread (scan), any thread (channel API) |
| `uc_apps` | `src/apps/uc_app_mgr.c`, `uc_app_host_api.c` | WAMR runtime and pool, application slots and threads, events, watchdog, host functions of import module `bacnet_uc`, key/value store | app threads, manager callers |
| `uc_mgmt` | `src/mgmt/uc_mgmt_{app,io,node,util}.c` | SMP groups 64 `uc_app`, 65 `uc_io`, 66 `uc_node` ([management-protocol.md](management-protocol.md)) | MCUmgr work queue |
| `uc_shell` | `src/shell/uc_shell.c` | `uc info`, `uc app ...`, `uc io ...`, `uc obj list`, `uc cfg show/reload` | shell thread, SMP shell group |
| `main` | `src/main.c` | boot sequence | main thread |

The modules talk only through the headers in `firmware/include/uc/`. Two
rules keep them decoupled:

1. **The BACnet stack is single-threaded.** bacnet-stack keeps global state
   (object tables, TSM, address cache, COV lists) without locks. Only the
   BACnet thread calls into it. Other threads use `uc_bn_exec()` /
   `uc_bn_post()` (executor) or the thread-safe wrappers
   (`uc_bn_obj_create()`, `uc_bn_prop_read()`, `uc_bn_remote_read()`, ...)
   which marshal into the BACnet thread themselves. Functions with the
   `_locked` suffix run in the BACnet thread only.
2. **Errors are negative errno values inside the firmware** and are mapped
   only at the boundaries: to `UC_ERR_*` for WebAssembly applications and to
   the group rc of the management protocol (table in
   [`uc_common.h`](../firmware/include/uc/uc_common.h)).

## 3. Threads and priorities

Zephyr priorities: negative = cooperative (not preempted by other threads),
0..14 = preemptible, lower number = higher priority. The table lists the
threads of the default build (`nucleo_f767zi` values; the other boards differ
only in the Ethernet driver thread).

| Thread | Priority | Stack | Owner | Role |
|--------|---------:|------:|-------|------|
| `rx_q[0]` (net RX traffic class) | -16 (coop) | 2048 | Zephyr net | IP/UDP receive processing, delivery to sockets |
| `stm_eth` (F767) / `ENETQOS_RX` work queue (MCXN947) | -14 / -13 (coop) | 1500 / 1024 | Ethernet driver | receive DMA descriptor service (none on `native_sim`: host sockets) |
| `sysworkq` | -1 (coop) | 4096 | Zephyr | system work queue: application watchdog work items, driver work |
| net_mgmt events, conn mgr monitor | -1 (coop) | 800 / 512 | Zephyr net | interface and address events (DHCP lease, link) |
| `main` | 0 | 4096 | `main.c` | boot sequence, then returns |
| SMP UDP receive | 0 | 1024 | MCUmgr | receives SMP datagrams, queues them to the SMP work queue |
| `mcumgr smp` work queue | 3 | 4096 | MCUmgr | runs every SMP command handler, including the custom groups; file uploads, JSON parsing of reloads, SHA-256 of app modules |
| `bacnet` | 5 (`CONFIG_UC_BACNET_THREAD_PRIORITY`) | 8192 | `uc_bacnet` | owns bacnet-stack; loop every `CONFIG_UC_BACNET_POLL_MS` (5 ms) or when woken |
| `uc_app0` .. `uc_app3` | 10 (`CONFIG_UC_APP_THREAD_PRIORITY`) | 8192 each | `uc_apps` | one per application slot; the only thread that executes that slot's WebAssembly instance |
| shell (UART) | 14 | 4096 | Zephyr shell | console, `uc` commands, SMP shell transport |
| shell (dummy backend) | 14 | 4096 | Zephyr shell | executes the commands of the SMP shell group (`exec`) |
| logging | 14 | 2048 | Zephyr logging | formats deferred log messages for UART, FS and net backends; `fs_sync()` after each batch |
| `idle` | 15 | 320 | kernel | |

Consequences of this assignment:

- Network reception and the BACnet thread preempt applications: a busy or
  looping application cannot delay BACnet responses or IO scanning.
- Management commands (prio 3) preempt the BACnet thread while a handler
  computes: JSON parsing on `reload`, SHA-256 of a module on `install`
  (256 KiB at most) delay the BACnet loop by their CPU time. Waiting for the
  flash (erase, program) blocks the handler and frees the CPU. Handlers that
  touch the BACnet stack go through the executor and wait for the BACnet
  thread.
- Logging and the shell run at the lowest application priority: under load,
  log output is delayed (deferred mode, 4 KiB buffer, the oldest messages are
  overwritten when it is full), never the control path.
- Applications may block in host calls (remote requests wait up to
  `timeout_ms`); the watchdog is paused while they block, so only execution
  time counts against `CONFIG_UC_APP_WATCHDOG_MS`. On `native_sim` simulated
  time stands still while a callback computes, so the watchdog cannot fire
  there; a budget of interpreted instructions per callback
  (`CONFIG_WAMR_INSTRUCTION_LIMIT`, 100 000 000 on `native_sim`, off on the
  boards) stops busy loops instead ([wasm-runtime.md](wasm-runtime.md#6-scheduling-and-timing)).

### The BACnet thread loop

```mermaid
flowchart TD
    W["wait: k_sem_take(wake, CONFIG_UC_BACNET_POLL_MS)<br/>(woken early by the executor)"] --> D{"datalink up?"}
    D -- no --> S["try bip_init() once per second<br/>when an IPv4 address exists"]
    D -- yes --> R["receive: bacnet_basic_task() up to 16x<br/>while packets arrive; bacnet_port_task()"]
    S --> X
    R --> X["drain executor queue<br/>(uc_bn_exec / uc_bn_post)"]
    X --> T["TSM timers, address cache timer,<br/>foreign device re-registration,<br/>ReinitializeDevice task"]
    T --> C["client slots: bind, send,<br/>timeouts (uc_bn_client_tick)"]
    C --> V["COV subscriptions: SubscribeCOV,<br/>renewals, polling, local change<br/>detection (uc_bn_cov_tick)"]
    V --> IO["uc_io_scan(): inputs -> PV,<br/>PV -> outputs"]
    IO --> ST["status snapshot every 100 ms"]
    ST --> W
```

`bacnet_basic_task()` handles at most one received PDU per call; the loop
calls it up to 16 times per iteration while packets keep arriving (the
bacnet-stack-zephyr sample sleeps 100 ms per call, which is too slow for a
burst of requests).

### Executor

```mermaid
sequenceDiagram
    participant A as caller (app thread, SMP work queue, shell)
    participant Q as executor queue (16 slots)
    participant B as BACnet thread
    A->>Q: uc_bn_exec(fn, arg, timeout): put slot index
    A->>B: k_sem_give(wake)
    B->>Q: drain (bounded: at most 16 per loop)
    B->>B: fn(arg) - calls into bacnet-stack
    B-->>A: k_sem_give(slot.done)
    A->>A: return 0 (or -ETIMEDOUT: fn still runs later,<br/>so arg must stay valid)
```

`uc_bn_exec()` runs `fn` inline when called from the BACnet thread. A full
queue returns `-EAGAIN` (mapped to `UC_ERR_BUSY` / rc `BUSY`).

## 4. Data flows

### 4.1 IO scan and objects

```mermaid
flowchart LR
    subgraph hw["channels (uc_io)"]
        di["di: GPIO"]
        ai["ai: ADC (mV)"]
        do["do: GPIO"]
        ao["ao: PWM (% duty)"]
        f["force overrides<br/>(uc_io force)"]
    end
    subgraph obj["BACnet objects (bacnet-stack)"]
        bi["BI / MSI<br/>Present_Value"]
        aiobj["AI<br/>Present_Value"]
        bo["BO (priority array -> PV)<br/>BV (PV)"]
        aoobj["AO (priority array -> PV)<br/>AV (PV)"]
    end
    di -- "debounce, invert" --> bi
    ai -- "raw * scale + offset" --> aiobj
    bo -- "invert" --> do
    aoobj -- "clamp min/max,<br/>(PV - offset) / scale,<br/>0..100 %" --> ao
    f -.-> di & ai & do & ao
```

Every loop, `uc_io_scan()` services the points whose `sample_ms` has elapsed.
Inputs write Present_Value directly (bypassing the write protection of input
objects) unless the object is Out_Of_Service; outputs read the effective
Present_Value (after priority arbitration for AO/BO; AV and BV have no
priority array) and drive the channel when it changed. Details:
[io.md](io.md).

### 4.2 BACnet server requests and write events

A WriteProperty from the BMS is handled entirely in the BACnet thread:
decode, `Device_Write_Property()`, priority array update, then the store
callback, which `uc_bn_local.c` uses as a write hook. When the written object
belongs to an application, the hook queues a `uc_app_on_write` event into
that application's queue (non-blocking; dropped and counted when the queue of
`CONFIG_UC_APP_EVENT_QUEUE_LEN` = 16 events is full). The next IO scan drives
an output from the new Present_Value.

```mermaid
sequenceDiagram
    participant BMS
    participant BT as BACnet thread
    participant APP as app thread (owner)
    BMS->>BT: WriteProperty AO:10 PV=22.5 @8
    BT->>BT: Device_Write_Property (priority array)
    BT->>BT: write hook: owner = app slot 1
    BT-->>BMS: SimpleACK
    BT->>APP: k_msgq_put(event WRITE)
    APP->>APP: uc_app_on_write(AO, 10, PV, 8, 22.5)
```

### 4.3 Client requests from applications

```mermaid
sequenceDiagram
    participant APP as app thread
    participant CL as client slot (8)
    participant BT as BACnet thread
    participant DEV as remote device
    APP->>CL: uc_remote_read(dev 1002, AI:1, PV, 2000 ms)
    Note over APP: watchdog paused while blocked
    CL->>BT: slot NEW, wake
    BT->>BT: bind: static binding / address cache,<br/>else Who-Is (repeated every 1 s)
    BT->>DEV: ReadProperty (TSM, invoke id)
    DEV-->>BT: ReadProperty-ACK
    BT->>CL: decode value, slot DONE, give semaphore
    CL-->>APP: UC_OK, *out = 21.3
```

A caller that times out marks its slot abandoned; the BACnet thread frees it
later and never touches the caller's memory again. Error, Reject and Abort
PDUs return `UC_ERR_BACNET`, a device that cannot be bound
`UC_ERR_NO_ROUTE`, no free slot or TSM entry `UC_ERR_BUSY`.

### 4.4 COV

Applications subscribe through `uc_cov_subscribe()`:

| Target | Mechanism | Traffic |
|--------|-----------|---------|
| local object (`UC_DEVICE_LOCAL` or own instance) | Present_Value compared every BACnet loop; analog objects use their COV_Increment | none |
| remote device supporting COV | SubscribeCOV (unconfirmed notifications), renewed at lifetime/2 | notifications |
| remote device rejecting COV | ReadProperty every `CONFIG_UC_BACNET_COV_POLL_MS` (2 s) | polling |
| remote device not answering SubscribeCOV | polling, SubscribeCOV retried every 60 s | polling |

The current value is delivered once right after subscribing. Before every
SubscribeCOV (initial, renewal, retry) and every poll, a remote subscription
takes the device's address from the address cache again, so a re-bound
device (new I-Am, changed static binding after `reload device`) is reached at
its new address. Server-side COV
(BMS subscribes to the node) is bacnet-stack's `handler_cov_subscribe` with
`CONFIG_BACNET_BASIC_COV_SUBSCRIPTIONS_SIZE` = 16 subscriptions. See
[bacnet.md](bacnet.md#5-change-of-value).

### 4.5 Management

```mermaid
sequenceDiagram
    participant H as harness / mcumgr / smpmgr
    participant U as SMP UDP thread
    participant W as MCUmgr work queue
    participant M as uc_mgmt / fs_mgmt
    participant X as module (uc_config, uc_io, uc_bacnet ...)
    H->>U: SMP write fs upload /lfs/cfg/io.json.new (chunks)
    U->>W: queue frame
    W->>M: fs_mgmt: write chunk to LittleFS
    M-->>H: rsp {off}
    H->>U: SMP write group 66 cmd 1 {"doc":"io"}
    U->>W: queue frame
    W->>M: uc_node reload
    M->>X: uc_config_reload(IO): validate io.json.new,<br/>rename over io.json, then uc_io_apply_config() via executor
    X-->>M: points bound
    M-->>H: {"reboot_required": false}
```

Configuration documents are staged: the client uploads `<doc>.json.new` and
the reload validates it and renames it over the active document (an invalid
one is deleted and the active configuration stays). Requests are limited to
1024 bytes over UDP (`CONFIG_MCUMGR_TRANSPORT_UDP_MTU`), responses to the
1152-byte SMP buffer; the lists `uc_app list`, `uc_io catalog` and
`uc_node objects` are paged with `offset`/`count`, and `uc_node prop_read`
returns an array property whole or rc `LIMIT` when it does not fit. The same
command set is reachable over the console UART (SMP shell transport, frames
multiplexed with the interactive shell, 16 receive buffers so that a full
request needs no pacing). Group, command and key definitions:
[management-protocol.md](management-protocol.md).

### 4.6 Logging

All modules log with `LOG_MODULE_REGISTER(<name>, CONFIG_UC_LOG_LEVEL)`;
applications log through `uc_log()` into the source `uc_app` with the
application name as prefix (max 120 characters per line, 20 lines per second
per application). Deferred logging hands messages to the logging thread,
which writes to the UART backend, the file backend (`/lfs/log/log.NNNN`) and,
with `overlay-syslog.conf`, the network backend. See
[storage-and-logging.md](storage-and-logging.md).

## 5. Boot sequence

```mermaid
sequenceDiagram
    participant K as kernel/drivers (PRE/POST_KERNEL)
    participant M as main thread
    participant B as BACnet thread
    participant A as app threads
    K->>K: clocks, GPIO, flash, Ethernet, net stack,<br/>fstab automount of /lfs (no format),<br/>SMP UDP server
    K->>M: main()
    M->>M: 1 uc_storage_init: mount /lfs (format if<br/>mount fails), create cfg/ apps/ data/ log/
    M->>M: 2 uc_config_init: activate staged *.json.new,<br/>load device/io/apps.json (defaults for missing/invalid)
    M->>M: 3 runtime log level from device.json log.level
    M->>M: 4 uc_net_init: static IPv4 or DHCPv4 (no wait)
    M->>M: 5 uc_io_init: channel hardware from devicetree
    M->>B: 6 uc_bn_start: create BACnet thread
    B->>B: bacnet_basic_init: Device, Network Port,<br/>service handlers, static bindings
    B->>B: bind io.json points (objects exist before the network)
    M->>A: 7 uc_apps_init: WAMR pool, host API,<br/>slots and threads, autostart apps
    A->>A: wait for uc_bn_ready()
    M->>M: 8 uc_mgmt_init: register groups 64/65/66
    M->>M: 9 wait up to CONFIG_UC_NET_WAIT_MS for IPv4,<br/>log "ready", return
    B->>B: IPv4 address present -> bip_init(port),<br/>I-Am, foreign device registration
    B-->>A: uc_bn_ready(): apps load, check, uc_app_init()
```

| Step | Failure | Effect |
|------|---------|--------|
| storage | no flash device, mount and format fail | logged; configuration falls back to Kconfig defaults; logs only on UART; apps cannot be installed |
| configuration | document missing | defaults for that document (`INF` log) |
| configuration | document invalid (syntax, schema, limits) | defaults for that document (`WRN` log with the offending field) |
| configuration | staged `<doc>.json.new` invalid | the staged file is deleted, the active document is used |
| network | no link / no DHCP lease | BACnet thread keeps waiting; after `CONFIG_UC_NET_WAIT_MS` (30 s) it starts BACnet anyway and retries the datalink once per second |
| io | a channel's device not ready | channel marked not ready, reads/writes return `-EIO`; points on it log once |
| bacnet | UDP port cannot be bound | retried once per second |
| apps | WAMR init fails (pool) | logged; app starts fail with "failed" state |
| mgmt | group registration | logged; the standard groups still work |

No step aborts the boot: the node stays reachable over the shell and SMP so
that it can be repaired remotely. `main()` returns after step 9; everything
else runs in the threads above.

## 6. Memory budgets

### 6.1 Static allocation plan

Every large RAM consumer has a fixed size, so that the linker, not a
failure at run time, reports a board that runs out of RAM. The two MCU boards
use the same plan; they differ in where the WAMR pool lives.

| Board | Main RAM (`zephyr,sram`) | WAMR pool (chosen `uc,app-pool`) |
|-------|--------------------------|----------------------------------|
| `nucleo_f767zi` | SRAM1/2, 384 KiB | 112 KiB in the 128 KiB DTCM, next to the Ethernet DMA descriptors and buffers (12 544 B, `CONFIG_ETH_STM32_HAL_USE_DTCM_FOR_DMA_BUFFER`) |
| `frdm_mcxn947/mcxn947/cpu0` | SRAM A-G, 384 KiB (the overlay gives cpu0 the RAM that Zephyr's default split reserves for the unused cpu1; SRAM H stays unused) | 96 KiB, all of SRAMX |
| `native_sim/native/64` | host process | 256 KiB in `.noinit` of the process |

Main RAM consumers (sizes from the symbol table of the `nucleo_f767zi`
build; the MCXN947 build has the same consumers):

| Consumer | Size | Where configured |
|----------|------|------------------|
| WAMR pool: module images, loaded modules (fast-interpreter code), instances, linear memories, app heaps, operand stacks | 112 KiB (F767, DTCM), 96 KiB (MCXN947, SRAMX), 256 KiB (native_sim) | `CONFIG_UC_APP_POOL_SIZE` in `boards/<board>.conf`, region in `boards/<board>.overlay` |
| kernel heap (`k_malloc`): configuration documents and parse buffers, file reads, shell/SMP snapshots | 64 KiB (MCUs; 48 KiB with `uc-ramfs`), 128 KiB (native_sim) | `CONFIG_HEAP_MEM_POOL_SIZE` |
| libc `malloc` arena (picolibc): bacnet-stack object data (`calloc` per object created from `io.json`, by applications or by CreateObject), PSA crypto | 64 KiB (MCUs; 32 KiB with `uc-ramfs`); host `malloc` on `native_sim` | `CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE` in `boards/<board>.conf` (`snippets/uc-ramfs/boards/ram.conf`) |
| app thread stacks | 4 × 8 KiB | `CONFIG_UC_APPS_MAX` × `CONFIG_UC_APP_THREAD_STACK_SIZE` |
| BACnet thread stack | 8 KiB | `CONFIG_UC_BACNET_THREAD_STACK_SIZE` |
| main, system work queue, SMP work queue, shell (UART), shell (dummy backend of the SMP shell group) | 4 KiB each | `prj.conf` |
| bacnet-stack TSM table (16 transactions with a 1476-byte APDU buffer each) | 24 KiB | `CONFIG_BACNET_MAX_TSM_TRANSACTIONS` |
| configuration cache (`device`, `io`, `apps`), the BACnet thread's copies of the device and IO configuration, IO point table | ~30 KiB | `CONFIG_UC_IO_POINTS_MAX`, `CONFIG_UC_APPS_MAX`, `CONFIG_UC_APP_PARAMS_MAX` |
| application slots (event queues, watchdog state, subscriptions) | ~14 KiB | `CONFIG_UC_APPS_MAX`, `CONFIG_UC_APP_EVENT_QUEUE_LEN` |
| network packets and buffers (16/16 packets, 32/32 buffers of 128 bytes, socket contexts) | ~15 KiB | `CONFIG_NET_PKT_*`, `CONFIG_NET_BUF_*` |
| bacnet-stack Network Port object lists | 11 KiB | bacnet-stack |
| owner table (64 objects) | 4.5 KiB | `CONFIG_UC_BACNET_OBJECTS_MAX` |
| log buffer | 4 KiB | `CONFIG_LOG_BUFFER_SIZE` |
| SMP shell transport receive buffers (16 lines) | ~2.4 KiB | `CONFIG_MCUMGR_TRANSPORT_SHELL_RX_BUF_COUNT` |
| LittleFS file caches (8 files × 256 bytes + read/prog/lookahead) | ~3 KiB | `CONFIG_FS_LITTLEFS_*`, fstab node |
| RAM disk of the `uc-ramfs` snippet | 64 KiB | `snippets/uc-ramfs/uc-ramfs.overlay` |

The `malloc` arena has a fixed size (Zephyr's default of -1 would give it
whatever RAM is left after linking). IO points and applications create their
BACnet objects from it, roughly 50..200 bytes per object plus list nodes (an
AO with its 16-entry priority array is about 140 bytes); an exhausted arena
fails object creation at run time with `NO_SPACE_FOR_OBJECT`
(`UC_ERR_NO_MEM`). With a fixed arena, static RAM that grows (a larger pool,
more threads) fails the link instead of silently shrinking the arena. The
board files document 24 KiB as the minimum arena.

### 6.2 Measured usage

`west build -p -b <board> BACNet-uc/firmware` of the current repository
state (Zephyr SDK 1.0.1, GCC 14.3), 0 compiler warnings in every build. None
of the images has run on the boards yet.

| Build | FLASH | RAM (384 KiB) | Second region |
|-------|------:|--------------:|---------------|
| `nucleo_f767zi` | 497 944 B of 2 MiB (23.74 %) | 359 868 B (91.52 %), 33 348 B free | DTCM 127 232 B of 128 KiB (97.07 %): pool 114 688 B + Ethernet DMA 12 544 B, 3 840 B free |
| `frdm_mcxn947/mcxn947/cpu0` | 507 236 B of 2 MiB (24.19 %) | 358 144 B (91.08 %), 35 072 B free | SRAMX 96 KiB of 96 KiB (100 %): pool |
| `nucleo_f767zi` `-S uc-ramfs` | 507 452 B (24.20 %) | 378 044 B (96.14 %), 15 172 B free | DTCM as above |
| `frdm_mcxn947/mcxn947/cpu0` `-S uc-ramfs` | 521 572 B (24.87 %) | 376 456 B (95.74 %), 16 760 B free | SRAMX as above |
| `native_sim/native/64` | `zephyr.exe`: text 715 220 B, data 78 404 B, bss 641 240 B | host process | - |

MCUboot builds (`west build -p --sysbuild -b <board> BACNet-uc/firmware`,
[`sysbuild.conf`](../firmware/sysbuild.conf)):

| Board | MCUboot (`boot_partition`) | Firmware in `slot0_partition` | Firmware RAM | Swap mode |
|-------|---------------------------:|------------------------------:|-------------:|-----------|
| `nucleo_f767zi` | 31 276 B of 64 KiB | 505 712 B of 785 850 B (64.35 %; 768 KiB slot minus the image trailer) | 360 508 B (91.68 %) | swap using scratch |
| `frdm_mcxn947/mcxn947/cpu0` | 48 052 B of 80 KiB | 516 152 B of 999 274 B (51.65 %; 984 KiB slot) | 358 776 B (91.24 %) | swap using offset |

The free main RAM is margin for later growth; the heap and the arena are
already reserved. Tight spots: the `uc-ramfs` variants (~15 KiB free) and the
F767 DTCM (3.8 KiB free next to the pool).

Code size by component (`nucleo_f767zi`, sum of the `.text` input sections
of the linker map; `.rodata` adds 113 512 B, mostly strings and tables):

| Component | `.text` |
|-----------|--------:|
| bacnet-stack + Zephyr glue | 72 996 B |
| WAMR (fast interpreter, loader, libc-builtin, thread manager) | 62 984 B |
| BACnet-uc modules (`firmware/src`) | 55 874 B |
| network stack (IP, UDP, sockets, DHCPv4, Ethernet L2, conn mgr, net shell) | 54 580 B |
| Zephyr libraries (logging, shell, JSON, PSA crypto, ...) | 35 328 B |
| LittleFS + VFS | 24 476 B |
| drivers and HAL (Ethernet, flash, SPI NOR, ADC, PWM, GPIO, UART) | 22 982 B |
| kernel + Cortex-M architecture code | 15 480 B |
| C library (picolibc, libgcc) | 14 234 B |
| MCUmgr (SMP transports and groups) + zcbor | 11 688 B |
| total `.text` | 370 622 B |

### 6.3 Tuning

| Goal | Change | Saves |
|------|--------|-------|
| more room for applications | raise `CONFIG_UC_APP_POOL_SIZE` only together with room in its region (the MCXN947's SRAMX is full; the F767 DTCM has 3.8 KiB left), or move the pool to the main RAM (remove the chosen `uc,app-pool`) and pay for it with the heap or the margin | - |
| fewer applications | `CONFIG_UC_APPS_MAX=2` | 16 KiB stacks + ~7 KiB slots |
| fewer simultaneous client requests of the stack | `CONFIG_BACNET_MAX_TSM_TRANSACTIONS=8` | ~12 KiB |
| smaller app threads (C apps with shallow host calls) | `CONFIG_UC_APP_THREAD_STACK_SIZE=6144` | 2 KiB per slot |
| no shell | `CONFIG_SHELL=n` (also removes the SMP shell transport and group, the console recovery path) | two 4 KiB shell stacks, buffers and the shell code |
| smaller malloc arena | `CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE` down to the 24 KiB minimum, when few objects are created | up to 40 KiB |

## 7. Failure handling

| Failure | Detection | Handling | Visible as |
|---------|-----------|----------|------------|
| corrupt or blank file system | mount fails | format (`CONFIG_UC_STORAGE_FORMAT_ON_FAIL=y`) and continue with defaults | `WRN uc_storage: formatting /lfs` |
| invalid configuration document at boot | parser error | defaults for that document (an invalid staged `<doc>.json.new` is deleted and the active document is used) | `WRN uc_config: ... rejected (-22), using defaults` |
| invalid document on `reload` | parser error | the active configuration is kept; an invalid staged `<doc>.json.new` is deleted | SMP rc `INVALID`, `WRN uc_config: ... rejected (-22), deleting it` |
| power loss during a configuration write | - | the firmware (`apps.json`) writes `<path>.tmp` and renames it; clients upload `<doc>.json.new` and the reload renames it over the document (one LittleFS metadata commit each): the old or the new file exists | - |
| no IPv4 address | `uc_net_wait_ready()` | BACnet waits, starts after 30 s anyway; SMP UDP starts when the address appears | `WRN no IPv4 address after 30000 ms` |
| remote device not reachable | no I-Am / TSM timeout | `UC_ERR_NO_ROUTE` / `UC_ERR_TIMEOUT` to the application; Who-Is repeated | app status `errors` |
| remote device rejects COV | Error/Reject/Abort to SubscribeCOV | polling fallback | transparent to the application |
| executor or client slots exhausted | queue full | `-EAGAIN`/`-EBUSY` → `UC_ERR_BUSY` / rc `BUSY`; caller retries | |
| IO hardware error | driver return code | point skipped, logged once per episode, retried every sample | `ERR uc_io: ai0 (analog-input 1): read failed` |
| application trap (out-of-bounds, unreachable, division by zero) | WAMR exception | instance destroyed, owned objects deleted, subscriptions cancelled, state `failed`, `last_error` set | `uc_app status` |
| application endless loop | watchdog `CONFIG_UC_APP_WATCHDOG_MS` (2 s) of execution time; on `native_sim` the instruction budget `CONFIG_WAMR_INSTRUCTION_LIMIT` (interpreter only) | `wasm_runtime_terminate()` / trap, state `failed` | `last_error: "uc_app_tick: watchdog, callback exceeded 2000 ms"` (`native_sim`: `"uc_app_tick: instruction limit exceeded"`) |
| application floods events or logs | queue full / rate limit | events dropped and counted as errors; log lines above 20/s dropped and counted | `WRN <app>: N log lines dropped` |
| WAMR pool exhausted | load/instantiate fails | start fails, state `failed`, `last_error` names the WAMR message | `uc_app status`, `uc_node info` `wasm.pool_free` |
| thread stack overflow | MPU stack guard (Cortex-M7), stack limit registers (Cortex-M33) | kernel fatal error | fatal error dump on the console |
| kernel fatal error, hang | - | **Planned**: hardware watchdog (IWDG on the F767, WWDT0 on the MCXN947) fed by the BACnet thread, reset on fatal error | |

## 8. Design decisions

| Decision | Alternatives | Reason |
|----------|--------------|--------|
| One thread owns bacnet-stack, others use an executor | a global mutex around the stack | the stack's handlers call back into objects and the store callback; a mutex would be held across callbacks into application code and invite lock inversion with the application threads |
| One thread per application slot | one thread running all apps | a blocking remote read or a long callback of one app does not delay the others; the watchdog can terminate one instance |
| Static WAMR pool | `malloc` from the kernel heap | fragmentation of the system heap by module images is avoided; the pool is sized per board and reported by `uc_node info` |
| JSON documents on LittleFS | Zephyr settings (NVS/ZMS) | human- and agent-readable, schema-validated before upload, diffable by the harness; settings remain available for the BACnet stack |
| SMP (MCUmgr) for management | custom HTTP/REST, BACnet file objects | existing clients (mcumgr, smpmgr, AuTerm), firmware update and file transfer included, same protocol over UDP and UART |
| Fast interpreter by default, AOT optional | AOT only | AOT needs executable RAM (MPU configuration, see [wasm-runtime.md](wasm-runtime.md#21-aot-and-the-mpu)) and per-board compilation; the interpreter runs the same `.wasm` on all boards |
