# WebAssembly application runtime

Control logic on a BACnet-uc node runs as WebAssembly modules executed by the
WebAssembly Micro Runtime (WAMR) 2.4.5. This document describes the runtime
as built into the firmware, the application model, the host ABI, memory,
sandboxing, toolchains and deployment.

| Normative source | File |
|------------------|------|
| Guest ABI (imports, exports, constants, error codes) | [`wasm/sdk/include/bacnet_uc.h`](../wasm/sdk/include/bacnet_uc.h) |
| Management commands (`uc_app` group) | [management-protocol.md](management-protocol.md#group-64-uc_app---webassembly-applications) |
| `apps.json` | [`schemas/apps.schema.json`](../schemas/apps.schema.json) |
| SDK, compiler wrapper, examples, test tools | [`wasm/README.md`](../wasm/README.md) |

Implementation: [`firmware/src/apps/uc_app_mgr.c`](../firmware/src/apps/uc_app_mgr.c)
(runtime, slots, threads, watchdog) and
[`uc_app_host_api.c`](../firmware/src/apps/uc_app_host_api.c) (host functions).

## 1. Why WebAssembly on a microcontroller

| Property | What it gives a BACnet controller |
|----------|-----------------------------------|
| Portable binary | the same `.wasm` runs on the Cortex-M7, the Cortex-M33 and `native_sim`; applications are tested on the host and deployed unchanged |
| Memory safety by construction | a module addresses only its own linear memory (bounds-checked); it cannot corrupt the firmware, the BACnet stack or another application |
| Capability-based host API | the module sees only the functions of import module `bacnet_uc`, gated by per-application permissions |
| Update without reflashing | a module is a file in `/lfs/apps`; installing, replacing or removing it takes seconds over SMP and needs no reboot |
| Language choice | C (SDK), Rust, AssemblyScript, TinyGo, anything that targets `wasm32` |
| Small artefacts | the example applications are 3.4..7.4 KiB (`.wasm`) |

Costs and limits: interpretation is slower than native code (acceptable for
control loops with periods of 10 ms and more); the fast interpreter keeps a
translated copy of the code in RAM; there is no preemption inside a module
other than the watchdog; floating point on the MCXN947's single-precision
FPU is emulated for `double`.

Alternatives considered:

| Alternative | Not chosen because |
|-------------|--------------------|
| Zephyr LLEXT (native loadable extensions) | native code has full access to memory and peripherals; no isolation between applications and firmware |
| MicroPython / Lua | one language, larger RAM footprint per application, garbage-collector pauses |
| IEC 61131-3 runtime | domain-appropriate, but a much larger engineering tool chain; may be layered on top later (a 61131 compiler emitting WebAssembly) |

## 2. WAMR configuration

The WAMR Zephyr glue lives in this repository
([`modules/wasm-micro-runtime/`](../modules/wasm-micro-runtime/)) because
WAMR's `zephyr/module.yml` refers to external glue.

| Feature | Setting | Kconfig / CMake |
|---------|---------|-----------------|
| Interpreter | fast interpreter (pre-translated internal bytecode) | `CONFIG_WAMR_FAST_INTERP=y` |
| AOT loader | off by default (section 2.1) | `CONFIG_WAMR_AOT`, `CONFIG_WAMR_AOT_MPU_EXEC` |
| JIT, Fast JIT | off | |
| C library for modules | libc-builtin (subset, `wasm/sdk/include/uc_libc.h`); no WASI | `WAMR_BUILD_LIBC_BUILTIN=1`, `WAMR_BUILD_LIBC_WASI=0` |
| Bulk memory, reference types | on | `CONFIG_WAMR_BULK_MEMORY=y`, `CONFIG_WAMR_REF_TYPES=y` |
| Thread manager | on, only for interruptible execution (the watchdog); WebAssembly threads are not available | `CONFIG_WAMR_THREAD_MGR=y`, `WASM_ENABLE_HEAP_AUX_STACK_ALLOCATION=1` |
| Instruction metering | `native_sim` only: 100 000 000 interpreted instructions per call into a module (section 6) | `CONFIG_WAMR_INSTRUCTION_LIMIT` (0 = off, the default on the boards) |
| SIMD, shared memory, multi-module, GC, exception handling, memory64, tail calls | off | |
| Memory allocation | one static pool, `Alloc_With_Pool` | `CONFIG_UC_APP_POOL_SIZE` |
| Linear memory allocation | rounded up to the 4 KiB page that WAMR bounds-checks (fix of a WAMR 2.4.5 gap, section 5) | `wamr_linear_memory.c` |
| Build target | `THUMBV7EM_VFP` (F767), `THUMBV8M.MAIN_VFP` (MCXN947), `X86_64` (`native_sim`) | `CONFIG_WAMR_BUILD_TARGET` (derived from CPU and `CONFIG_FP_HARDABI`) |
| Runtime log | on | `CONFIG_WAMR_LOG` |

The pool is a static array. The devicetree chosen node `uc,app-pool`
(pointing at a `zephyr,memory-region`) selects its RAM region; without it the
pool is in `.noinit` of the main RAM. The board overlays place it outside the
main RAM:

| Board | `uc,app-pool` | Pool |
|-------|---------------|------|
| `nucleo_f767zi` | `&dtcm` | 112 KiB of the 128 KiB DTCM (the Ethernet DMA buffers use 12.3 KiB of the rest) |
| `frdm_mcxn947/mcxn947/cpu0` | `&sramx` | 96 KiB, all of SRAMX |
| `native_sim/native/64` | - | 256 KiB in `.noinit` |

### 2.1 AOT and the MPU

An AOT module (`.aot`, produced by `wamrc`) contains native machine code that
WAMR copies into its pool and executes there. On a Cortex-M this requires
that the MPU allows instruction fetches from the pool. Zephyr's default MPU
configuration marks SRAM execute-never whenever the image executes in place
from flash (`CONFIG_XIP=y`, the case on both boards: `REGION_RAM_ATTR` in
`arm_mpu_v7m.h` and `arm_mpu_v8.h`): the F767's DTCM is covered by the SRAM
region with XN, the MCXN947's SRAMX by its own RAM region with XN.

WAMR 2.4.5's Zephyr platform layer
(`core/shared/platform/zephyr/zephyr_platform.c`) tries to handle this and
gets it wrong; the firmware does not build that part:

| WAMR 2.4.5 | Behaviour | BACnet-uc glue |
|------------|-----------|----------------|
| `bh_platform_init()` with AOT and `CONFIG_ARM_MPU` calls `disable_mpu_rasr_xn()` | ARMv7-M: for MPU regions 0..7 with `RASR.XN` set it executes `MPU->RASR \|= ~MPU_RASR_XN_Msk`, which sets every other RASR bit and leaves XN set, i.e. switches those regions off instead of making them executable (upstream bug; intended: `&= ~`). ARMv8-M: `MPU_RASR_XN_Msk` does not exist (XN is in `RBAR`), the loop compiles to nothing | `zephyr_platform.c` is compiled with `WASM_ENABLE_AOT=0`, so the function is not built (a CMake check stops the build if a WAMR update changes this) |
| `os_mmap()` for code sections | `BH_MALLOC()` from the pool, 8-byte aligned | in AOT builds the glue makes every `os_mmap()` block 16-byte aligned (x86-64 AOT code reads 16-byte constants with `movaps`) |
| `os_mprotect()` | returns 0 without doing anything | - |
| cache maintenance | `os_dcache_flush()` cleans the D-cache on the Cortex-M7; `os_icache_flush()` calls `sys_cache_instr_flush_range()` | used as is |

Instead, the application manager calls `wamr_zephyr_exec_enable()`
(`modules/wasm-micro-runtime/wamr_zephyr_exec.c`) at start:

| Target | What happens | Result |
|--------|--------------|--------|
| `nucleo_f767zi`, `frdm_mcxn947` (ARMv7-M and ARMv8-M MPU) | the MPU registers are read to check that every 32-byte granule of the pool is executable. Only with `CONFIG_WAMR_AOT_MPU_EXEC=y` is the XN attribute of the enabled regions that decide the access to the pool cleared first | without the option: AOT files are refused; with it: the whole region becomes executable, on the F767 all of SRAM including DTCM (like a `CONFIG_XIP=n` image), on the MCXN947 only SRAMX |
| `native_sim` | the pool (page-aligned in AOT builds) is made executable with `mprotect()` | AOT files load |

`uc_node info` reports `"aot": true` (with `aot_target`) only when AOT files
can actually run; otherwise `uc_app install` and a start of an AOT file fail
with rc `UNSUPPORTED` ("AOT: WAMR pool not executable" or "AOT modules not
supported"). Without `CONFIG_WAMR_AOT` the firmware reports `"aot": false`.

Status: `CONFIG_WAMR_AOT=y` builds without warnings for all three boards,
on the boards with and without `CONFIG_WAMR_AOT_MPU_EXEC`. The four examples run as x86-64 AOT
files on the `native_sim` firmware. On the boards the Thumb AOT files are
compiled and symbol-checked, and the MPU check and XN clearing were
exercised in QEMU (mps2/an385 ARMv7-M, mps2/an521 ARMv8-M); nothing has run
on the real boards yet.

Rules for AOT modules (`wasm/sdk/uc-aot`, [`wasm/README.md`](../wasm/README.md#uc-aot)):

1. The AOT target must match `CONFIG_WAMR_BUILD_TARGET` without the `_VFP`
   suffix (reported as `wasm.aot_target` by `uc_node info`).
2. `--bounds-checks=1` (no hardware bounds checks on Zephyr).
3. Cortex-M: indirect mode (uc-aot's default). In direct mode LLVM emits
   calls to `__aeabi_memclr`, which WAMR 2.4.5's `aot_reloc_thumb.c` symbol
   map lacks, and WAMR offers no way to extend that map from outside.
4. `--enable-multi-thread` (uc-aot's default): only then does AOT code check
   the terminate flag, so that the watchdog can stop it on the boards. The
   instruction budget of `native_sim` does not apply to AOT code: an AOT busy
   loop still hangs a `native_sim` node.

Executable, writable RAM weakens the isolation argument of section 7: a bug
in WAMR's AOT loader or a crafted AOT file can run native code with full
privileges. Enable AOT (and on the boards `CONFIG_WAMR_AOT_MPU_EXEC`) only
with trusted modules; a dedicated W^X execution region is **Planned**
([roadmap.md](roadmap.md#33-aot-by-default-on-the-nucleo-f767zi)). The
interpreter needs no executable RAM.

## 3. Application model

An application is an installed `apps.json` entry that binds a name to a
module file, permissions, parameters and resource limits. Up to
`CONFIG_UC_APPS_MAX` (4) applications are installed; each occupies a slot
with its own thread (priority 10, 8 KiB native stack), event queue (16
events), watchdog and module instance.

### 3.1 Lifecycle

```mermaid
stateDiagram-v2
    [*] --> stopped: install (autostart false)
    stopped --> starting: start / install (autostart) / boot (autostart)
    starting --> running: uc_app_init() returned 0
    starting --> failed: load, link, version, instantiation code or init error
    running --> stopped: stop (uc_app_deinit() called)
    running --> failed: trap or watchdog, also while stopping (no uc_app_deinit())
    failed --> starting: start
    stopped --> [*]: remove
    failed --> [*]: remove
```

Start sequence in the application's thread:

1. Wait until the BACnet node is ready (`uc_bn_ready()`): applications never
   run before the stack and the datalink are up.
2. If the entry has `sha256`, hash the file and compare (`VERIFY` on
   mismatch).
3. Read the module into a pool buffer (WAMR references it until unload); check
   the magic (`\0asm`, or `\0aot` when AOT files can run, section 2.1).
4. `wasm_runtime_load()`; every function import must be resolved (`bacnet_uc`
   host functions or libc-builtin in `env`), otherwise "unresolved import
   module.name".
5. Refuse a module whose instantiation would run module code: a start
   function (start section) or an exported `__wasm_call_ctors`,
   `__post_instantiate` or `_initialize` (any signature). WAMR would run that
   code inside `wasm_runtime_instantiate()` on an execution environment of
   its own, outside the watchdog, the instruction budget and the app context
   of the host functions. The start fails with rc `VERIFY` and `last_error`
   "`<what> not supported (runs at instantiation)`"
   (`modules/wasm-micro-runtime/wamr_zephyr_module.c`); see section 8 for
   the toolchain consequences.
6. `wasm_runtime_instantiate()` with `stack_kb` and `heap_kb`; create the
   execution environment.
7. Look up the exports and check their signatures; `uc_app_api_version` is
   mandatory.
8. Call `uc_app_api_version()`; the major version must equal the host's
   `UC_API_VERSION_MAJOR`.
9. Call `uc_app_init()`; non-zero aborts the start (`failed`, "uc_app_init
   returned N"). Global setup of a module belongs here.

Running: the first `uc_app_tick(now_ms)` comes one `period_ms` after the
start, then at a fixed rate (missed ticks are skipped after an overrun).
Between ticks the thread waits for events and delivers them in arrival order
(`uc_app_on_cov`, `uc_app_on_write`); a due tick is delivered before queued
events. `uc_set_tick_period(0)` or `period_ms: 0` gives an event-only
application.

Stop (`uc_app stop`, `remove`, `install` with `restart`, `reload apps`):

1. The manager sets the stop flag and cancels the application's blocking host
   calls. A remote request in flight is abandoned within about 50 ms
   (`UC_BN_CANCEL_POLL_MS`) and returns `UC_ERR_TIMEOUT`; a late
   confirmation is dropped.
2. From then on, blocking calls of the running callback fail at once
   (`uc_remote_*` to another device: `UC_ERR_TIMEOUT`, `uc_kv_*`:
   `UC_ERR_IO`) and no longer pause the watchdog, so the callback has to
   return. If it does not, the watchdog terminates it; a callback that makes
   100 more blocking calls after the cancellation is terminated at once
   (on `native_sim` host calls do not consume the instruction budget). A
   terminated callback ends in `failed`, without `uc_app_deinit()`.
3. After the callback returned, `uc_app_deinit()` is called under the
   watchdog. Its blocking calls work again (e.g. to relinquish a command on
   another device), but their time counts against the watchdog, and an
   expiry cancels them again: a relinquish to a device that does not answer
   before the watchdog (2 s for the whole of `uc_app_deinit()`) expires is
   cut off. A trap or watchdog expiry here is recorded in `last_error`; the
   application still ends `stopped`.
4. The slot is cleaned up (below) and the request returns.

A stop normally completes in about 1 s and takes at most 2 ×
`CONFIG_UC_APP_WATCHDOG_MS` (the rest of the running callback plus
`uc_app_deinit()`) plus about 1 s for the cancellation and the cleanup,
whatever the module does. The manager gives up after 2 × watchdog + 8 s
(rc `BUSY`, the application keeps its `apps.json` entry,
[management-protocol.md](management-protocol.md#group-64-uc_app---webassembly-applications));
an application blocked in, or looping on, remote requests no longer runs
into that limit. Failure (trap, watchdog, failed start) skips
`uc_app_deinit()`.

Cleanup in both cases: cancel COV subscriptions, delete the objects the
application owns, drop queued events, destroy exec env, instance, module and
the module image. A failed application stays failed until it is started
again; there is no automatic restart.

### 3.2 Exports

| Export | Signature (C) | WebAssembly type | Required | Called |
|--------|---------------|------------------|----------|--------|
| `uc_app_api_version` | `uint32_t (void)` | `() -> i32` | yes (`UC_APP_DECLARE()`) | once, before `uc_app_init` |
| `uc_app_init` | `int32_t (void)` | `() -> i32` | no | once; 0 = run |
| `uc_app_tick` | `void (uint64_t now_ms)` | `(i64) -> ()` | no | every tick period; `now_ms` = uptime |
| `uc_app_on_cov` | `void (int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance, uint32_t prop, double value)` | `(i32 i32 i32 i32 i32 f64) -> ()` | no | per notification of a live subscription |
| `uc_app_on_write` | `void (uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority, double value)` | `(i32 i32 i32 i32 f64) -> ()` | no | after another writer wrote a numeric value to a property of an object the application owns (not for NULL/relinquish or string writes) |
| `uc_app_deinit` | `void (void)` | `() -> ()` | no | on a regular stop |

An export with a different signature fails the start ("export X: wrong
signature").

## 4. Host ABI reference

Import module `"bacnet_uc"`. Pointers are offsets into the application's
linear memory; the WAMR signature strings use `i` for i32 (also pointers),
`I` for i64 and `F` for f64. All functions returning `int32_t` return ≥ 0 on
success or a negative `UC_ERR_*` code. Every error return is counted in the
application's `errors` statistic, except `UC_ERR_NOT_FOUND` of
`uc_param_get`, `uc_param_get_number` and `uc_kv_get` (a missing parameter or
key selects a default and is not an error).

Pointer arguments: the host checks the whole range `[ptr, ptr + len)` (all 8
bytes of a `double *`) against the linear memory. A NULL pointer (offset 0), a
range outside the memory or a zero length where data is needed returns
`UC_ERR_INVALID`; the host never traps on a bad pointer. (The libc-builtin
functions of section 4.3 trap on invalid pointers instead.)

### 4.1 Error codes

| Code | Value | Typical cause |
|------|------:|---------------|
| `UC_OK` | 0 | |
| `UC_ERR_INVALID` | -1 | bad pointer or length; value the property's datatype cannot hold (NaN/infinity, a fraction for an integer datatype, out of range, not 0/1 for a binary value); array index on a non-array property; malformed key; unsupported object type; buffer too small |
| `UC_ERR_NOT_FOUND` | -2 | unknown object/property, parameter, channel, key or subscription |
| `UC_ERR_PERM` | -3 | permission missing in `perms`; write access denied by the object; write to an input channel |
| `UC_ERR_TIMEOUT` | -4 | no confirmation within `timeout_ms` |
| `UC_ERR_BUSY` | -5 | client slots or executor queue exhausted, retry |
| `UC_ERR_NO_MEM` | -6 | table full (objects, subscriptions), allocation failed |
| `UC_ERR_BACNET` | -7 | the remote device answered with Error, Reject or Abort |
| `UC_ERR_UNSUPPORTED` | -8 | feature not built into this firmware |
| `UC_ERR_IO` | -9 | file system or hardware error |
| `UC_ERR_TYPE` | -10 | property datatype not numeric, parameter not a number, relinquish of an input's Present_Value or of a property without priorities |
| `UC_ERR_EXISTS` | -11 | object exists and is owned by someone else |
| `UC_ERR_NO_ROUTE` | -12 | remote device could not be bound |

### 4.2 Functions

| Function | WAMR sig | Permission | Returns | Errors and notes |
|----------|----------|------------|---------|------------------|
| `void uc_log(int32_t level, const char *msg, uint32_t len)` | `(iii)` | - | - | Logs `"<app>: <msg>"` at level 1 ERR, 2 WRN, 3 INF, ≥4 DBG. Truncated to 120 characters, trailing CR/LF removed, control characters replaced by blanks. Rate limit 20 lines per second per application, shared with `printf` output (section 4.3); excess lines are dropped and counted ("N log lines dropped"). Invalid pointer: counted as error, nothing logged |
| `uint64_t uc_uptime_ms(void)` | `()I` | - | ms since boot | |
| `int32_t uc_set_tick_period(uint32_t period_ms)` | `(i)i` | - | `UC_OK` | 0 disables ticks; otherwise 10..3 600 000 ms, else `INVALID`. Takes effect for the next tick (the period restarts) |
| `int32_t uc_param_get(const char *key, uint32_t key_len, char *buf, uint32_t buf_len)` | `(iiii)i` | - | value length without NUL | `NOT_FOUND`; `INVALID` for a bad key (1..23 chars of `[A-Za-z0-9_.-]`), a bad buffer or `buf_len` < length. NUL-terminated only if it fits |
| `int32_t uc_param_get_number(const char *key, uint32_t key_len, double *out)` | `(iii)i` | - | `UC_OK` | `strtod` of the value: `TYPE` if not a number; `NOT_FOUND`, `INVALID` |
| `int32_t uc_obj_create(uint32_t type, uint32_t instance, const char *name, uint32_t name_len)` | `(iiii)i` | `bacnet.local` | `UC_OK` (also if already owned by this app) | types AI, AO, AV, BI, BO, BV, MSI, MSO, MSV else `INVALID`; name ≤ 63 bytes (empty: stack default name); `EXISTS` if owned by IO, another app or created over the network; `NO_MEM` owner table full; `BUSY` |
| `int32_t uc_obj_delete(uint32_t type, uint32_t instance)` | `(ii)i` | `bacnet.local` | `UC_OK` | only objects owned by this app (`NOT_FOUND` / `PERM` otherwise) |
| `int32_t uc_prop_read(uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index, double *out)` | `(iiiii)i` | `bacnet.local` | `UC_OK` | any local object incl. Device; `array_index` -1 (`UC_ARRAY_ALL`) = the property (of an array: its first element), 0 = array size, 1..n = element; an index on a property that is not an array is `INVALID`; `NOT_FOUND`, `TYPE` for non-numeric datatypes |
| `int32_t uc_prop_write(uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index, double value, uint32_t priority)` | `(iiiiFi)i` | `bacnet.local` | `UC_OK` | WriteProperty semantics: priority 0 = none (16 for the Present_Value of AO, BO, MSO), 1..16; AV, BV, MSV and other properties ignore the priority (but priority 6 is `PERM` on AV); value converted to the property's datatype (`TYPE` if not numeric, `INVALID` if the datatype cannot hold it); `PERM` write access denied (e.g. input not Out_Of_Service, priority 6 on AO/BO/MSO/AV) |
| `int32_t uc_prop_write_null(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority)` | `(iiii)i` | `bacnet.local` | `UC_OK` | AO, BO, MSO Present_Value: clears that priority slot (0 = 16). AV, BV, MSV Present_Value (no priority array): `UC_OK`, nothing changes. Present_Value of inputs and other properties: `TYPE` |
| `int32_t uc_prop_write_string(uint32_t type, uint32_t instance, uint32_t prop, const char *str, uint32_t len)` | `(iiiii)i` | `bacnet.local` | `UC_OK` | CharacterString properties (Object_Name, Description); length ≤ 63 |
| `int32_t uc_remote_read(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index, double *out, uint32_t timeout_ms)` | `(iiiiiii)i` | `bacnet.remote` (`bacnet.local` for the own device) | `UC_OK` | blocks up to `timeout_ms` (0 = 5000, max 600 000); `NO_ROUTE`, `TIMEOUT`, `BACNET`, `BUSY`, `TYPE`. `TIMEOUT` also when the application is being stopped: a request in flight is abandoned within about 50 ms, later calls of the stopping callback fail at once (section 3.1). The own device instance or `UC_DEVICE_LOCAL` is served locally |
| `int32_t uc_remote_write(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index, double value, uint32_t priority, uint32_t timeout_ms)` | `(iiiiiFii)i` | `bacnet.remote` (`bacnet.local` for the own device) | `UC_OK` | as above (blocking, `TIMEOUT` when stopped); the value is converted like `uc_prop_write` before sending and rejected with `INVALID` if it does not fit. The datatype comes from the host's table of standard numeric properties (`uc_value_from_double()` in `uc_common.c`), which covers AI, AO, AV, BI, BO, BV, MSI, MSO, MSV, Integer Value, Positive Integer Value, Large Analog Value, Accumulator, Loop, Pulse Converter, Lighting Output and Binary Lighting Output (see below); other properties return `TYPE` without a request. A BACnet-uc peer's AV, BV, MSV ignore the priority |
| `int32_t uc_remote_write_null(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority, uint32_t timeout_ms)` | `(iiiiii)i` | `bacnet.remote` (`bacnet.local` for the own device) | `UC_OK` | WriteProperty NULL at `priority`; blocking and cancellation as for `uc_remote_read`; effect on a BACnet-uc node as for `uc_prop_write_null` |
| `int32_t uc_cov_subscribe(uint32_t device, uint32_t type, uint32_t instance, uint32_t lifetime_s)` | `(iiii)i` | `bacnet.local` for local, `bacnet.remote` for remote | subscription id ≥ 0 | Present_Value only; current value delivered once, then changes ([bacnet.md](bacnet.md#52-client-for-applications)); `NO_MEM` when the application's or the node's table (16) is full |
| `int32_t uc_cov_unsubscribe(int32_t sub_id)` | `(i)i` | - | `UC_OK` | only this instance's subscriptions (`NOT_FOUND` otherwise); no event of that subscription is delivered afterwards |
| `int32_t uc_io_find(const char *name, uint32_t len)` | `(ii)i` | `io` | channel id ≥ 0 | `NOT_FOUND` |
| `int32_t uc_io_read(int32_t channel, double *out)` | `(ii)i` | `io` | `UC_OK` | di/do 0/1, ai mV, ao %; forced value while forced; `NOT_FOUND`, `IO` |
| `int32_t uc_io_write(int32_t channel, double value)` | `(iF)i` | `io` | `UC_OK` | outputs only (`PERM` for inputs); `INVALID` for NaN/infinities; do: non-zero = 1; ao clamped to 0..100 |
| `int32_t uc_kv_get(const char *key, uint32_t key_len, void *buf, uint32_t buf_len)` | `(iiii)i` | `kv` | stored length (may exceed `buf_len`; `min(len, buf_len)` bytes copied) | key 1..31 chars of `[A-Za-z0-9_.-]`; `NOT_FOUND`, `IO` (storage not ready, read error, or at once while the application is being stopped, except in `uc_app_deinit()`). `buf_len` 0 queries the length |
| `int32_t uc_kv_set(const char *key, uint32_t key_len, const void *val, uint32_t val_len)` | `(iiii)i` | `kv` | `UC_OK` | ≤ `CONFIG_UC_APP_KV_VALUE_MAX` (256) bytes else `INVALID`; written atomically (`<key>~` then rename) to `/lfs/data/<app>/<key>`; `IO` (also at once while the application is being stopped, except in `uc_app_deinit()`) |

Value conversions: REAL/DOUBLE ↔ value (REAL: finite, within the float
range); UNSIGNED, SIGNED, ENUMERATED ↔ value (integral and within the
datatype's range on write); BOOLEAN ↔ 0.0/1.0; binary Present_Value,
Relinquish_Default and priority-array entries ↔ 0.0 inactive / 1.0 active.
A write of NaN, an infinity, a fraction for an integer datatype or a value
out of range returns `UC_ERR_INVALID` (the same rules as SMP `prop_write`,
[management-protocol.md](management-protocol.md#values)). Other datatypes
give `UC_ERR_TYPE`. The `Values` comment of `bacnet_uc.h` states these
rules, and the host stub (`wasm/sdk/host-stub`) applies them, so an app's
stub tests see the same `UC_ERR_INVALID` as the node.

Datatypes for `uc_remote_write`: the host does not ask the remote device for
the datatype; it encodes the value with the datatype from bacnet-stack's
table of constructed properties (`bacapp_known_property_tag()`) or, for
primitive properties, from its own table (`uc_value_from_double()` in
[`uc_common.c`](../firmware/src/common/uc_common.c)):

| Property | Datatype |
|----------|----------|
| Present_Value, Relinquish_Default, Priority_Array of AI, AO, AV, Loop, Pulse Converter, Lighting Output | REAL |
| … of BI, BO, BV, Binary Lighting Output | ENUMERATED (BI/BO/BV: 0.0 or 1.0 only) |
| … of MSI, MSO, MSV, Positive Integer Value, Accumulator | UNSIGNED |
| … of Integer Value | SIGNED |
| … of Large Analog Value | DOUBLE |
| COV_Increment, High_Limit, Low_Limit, Deadband, Min_Pres_Value, Max_Pres_Value, Resolution | the Present_Value datatype for Large Analog Value, Integer Value (COV_Increment and Deadband: UNSIGNED) and Positive Integer Value; REAL otherwise |
| Loop: Setpoint, Controlled_Variable_Value, Proportional/Integral/Derivative_Constant, Bias, Maximum_Output, Minimum_Output; Lighting Output: Tracking_Value, Default_Ramp_Rate, Default_Step_Increment, Min_Actual_Value, Max_Actual_Value | REAL |
| Loop: Action; Units, Polarity, Event_State, Reliability, Notify_Type; Device: System_Status, Segmentation_Supported | ENUMERATED |
| Out_Of_Service, Event_Detection_Enable; Device: Daylight_Savings_Status | BOOLEAN |
| Number_Of_States, Time_Delay, Notification_Class, Minimum_On_Time, Minimum_Off_Time, Change_Of_State_Count, Elapsed_Active_Time, Update_Interval, Default_Fade_Time; Device: APDU_Timeout, APDU_Segment_Timeout, Number_Of_APDU_Retries, Max_APDU_Length_Accepted, Max_Segments_Accepted, Vendor_Identifier, Database_Revision, Protocol_Version, Protocol_Revision, Max_Info_Frames, Max_Master | UNSIGNED |
| Device: UTC_Offset | SIGNED |
| any other property, or Present_Value of any other object type | not written: `UC_ERR_TYPE` |

Priorities: only AO, BO and MSO have a priority array on BACnet-uc nodes;
the value objects AV, BV and MSV take every write whatever its priority, and a
relinquish of them succeeds without effect ([bacnet.md](bacnet.md#31-object-types)).
Priority 6 (reserved for Minimum_On/Off) is the exception: a write or
relinquish at priority 6 on AO, BO, MSO and a write at priority 6 on AV
return `UC_ERR_PERM`; BV and MSV ignore it. The host stub does the same.
`uc_app_on_write` reports the writer's priority (16 for a write without
priority) also for value objects.

Blocking: remote requests and kv file access block the calling application
thread only. The watchdog is paused while they block, except once a stop is
pending: a stop cancels them (a remote request in flight returns
`UC_ERR_TIMEOUT` within about 50 ms, further calls fail at once until
`uc_app_deinit()`, whose blocking calls count against the watchdog; section
3.1). A watchdog expiry cancels them the same way. Local object access goes
through the BACnet executor and takes at most one BACnet loop.

### 4.3 C library (libc-builtin)

Modules may import the libc-builtin functions listed in
[`uc_libc.h`](../wasm/sdk/include/uc_libc.h) from module `env` (`printf`
family, string functions, `malloc` family on the app heap, `strtol`,
character classes). Any other import makes the load fail.

Output of `printf`, `vprintf`, `puts` and `putchar` is not written to the
console: the firmware collects it per line (up to `\n`) and logs each line
like `uc_log` at info level, from log source `uc_app` as
`"<app>: <line>"` (`uc_app: <app>: ...` in the log), with the same rules:
control characters replaced by blanks, cut to 120 characters, and the
20 lines per second of the application's `uc_log` budget. An unfinished line
is logged when the callback returns. Runtime diagnostics that WAMR prints in
the application's thread are handled the same way, so a module cannot write
untagged or unlimited text into the log
([`uc_app_host_api.c`](../firmware/src/apps/uc_app_host_api.c),
`modules/wasm-micro-runtime/wamr_zephyr_glue.c`). Prefer `uc_log` (or
`uc_logf()` of `uc_util.h`), which also selects the level.

## 5. Memory model

```
linear memory (per instance, from the WAMR pool)
0              stack_size            __data_end  __heap_base            + heap_kb * 1024
|<- C (aux) stack, grows down -|- data/bss -|             |<- app heap (malloc) ->|
```

| Memory | Size | Where | Configured by |
|--------|------|-------|---------------|
| C (auxiliary) stack | ≥ 4 KiB, page-aligned by uc-cc | linear memory, below the data (`--stack-first`): an overflow traps below address 0 instead of overwriting data | link time (`uc-cc --stack-size`) |
| data, bss | module dependent | linear memory | compiler |
| app heap | `heap_kb` KiB (default 8, 0 if unused) | appended to the linear memory by WAMR | `apps.json` `heap_kb` |
| WAMR execution stack (interpreter frames, operand stack) | `stack_kb` KiB (default 4) | pool | `apps.json` `stack_kb` |
| native thread stack | 8 KiB | RAM, per slot | `CONFIG_UC_APP_THREAD_STACK_SIZE` |
| module image, translated module, instance | module dependent | pool | |

Linear memory shrinking: a module declares one 64 KiB page, exports
`__heap_base` and `__data_end` and does not use `memory.grow`; WAMR then
truncates the memory at `__heap_base` and appends the app heap, so a small
application needs 8..16 KiB of linear memory instead of 64 KiB.

Linear memory allocation: WAMR 2.4.5 bounds-checks a linear memory up to its
size rounded up to the page size (4 KiB without an MMU), but its Zephyr
platform allocates only the unrounded size from the pool, so a module whose
memory size is not a multiple of 4 KiB could read and write up to 4095 bytes
behind its pool block. The firmware closes this gap:
`modules/wasm-micro-runtime/wamr_linear_memory.c` redirects every allocation
of WAMR's `wasm_memory.c` to one that rounds up to the page size, so the block
always covers the checked range (the build stops if a WAMR update changes the
code this relies on). An access inside the rounding tail reaches the module's
own zeroed memory; the first byte past the rounded bound traps. The rounding
costs pool memory the module cannot use, so uc-cc page-aligns `__heap_base`
and `heap_kb` should be a multiple of 4
([`wasm/README.md`](../wasm/README.md#linear-memory)).

Pool consumption per application, measured on the `native_sim` firmware
(x86-64; `wasm.pool_free` of `uc_node info` before and after starting the
module built by `make -C wasm`, `stack_kb` 4). WAMR's structures are somewhat
smaller on the 32-bit targets, so these figures are conservative for the
boards:

| Module | `.wasm` | `heap_kb: 8` | `heap_kb: 0` |
|--------|--------:|-------------:|-------------:|
| blinky | 3 497 B | 38 768 B | 28 464 B |
| thermostat | 7 536 B | 54 856 B | 44 552 B |
| alarm | 6 268 B | 49 576 B | 39 272 B |
| uc-link (one link) | 5 419 B | 46 832 B | 36 528 B |

None of the examples allocates from the app heap, so `heap_kb: 0` is
sufficient for them (the harness deploys `uc-link` with 0). All four together
with `heap_kb: 0` use 154 392 B of the 256 KiB `native_sim` pool. By these
figures the F767's 112 KiB pool holds three of the examples with
`heap_kb: 0`, the MCXN947's 96 KiB pool two. `uc_node info` reports
`wasm.pool_total` and `wasm.pool_free` of the running node, and the harness's
`validate_system` estimates the pool use of every node of a manifest and warns
above 90 % ([distributed-apps.md](distributed-apps.md#4-placement-rules)).

## 6. Scheduling and timing

| Aspect | Behaviour |
|--------|-----------|
| Threads | one per slot, priority 10, preemptible; applications of equal priority are time-sliced (20 ms) |
| Relation to BACnet | the BACnet thread (5), network threads and SMP (3) preempt applications; an application cannot delay BACnet responses or the IO scan |
| Tick jitter | a tick runs when its thread is scheduled after the timer expired; with idle higher-priority threads this is within a scheduler tick |
| Event latency | a COV notification or write is delivered after the running callback returns |
| Watchdog | each callback may execute for `CONFIG_UC_APP_WATCHDOG_MS` (2000 ms); time blocked in remote requests or kv access does not count (the watchdog pauses). Expiry: `wasm_runtime_terminate()`, the instance stops at its next branch or call, the application enters `failed` with `last_error` "`<export>: watchdog, callback exceeded 2000 ms`" |
| Watchdog on `native_sim` | simulated time only advances while the simulated CPU idles, so the watchdog timer cannot expire during a callback that never blocks. Instead every call into a module gets a budget of interpreted instructions (`CONFIG_WAMR_INSTRUCTION_LIMIT`, 100 000 000 by default on `native_sim`, roughly 0.1 to 0.5 s on a PC); a callback that exceeds it traps (`last_error` "`uc_app_tick: instruction limit exceeded`") and the node keeps running. The budget does not cover AOT code: an AOT busy loop still hangs a `native_sim` node. On the boards the metering is not built (0) and the watchdog works in real time |

## 7. Sandboxing

| Mechanism | Protects against |
|-----------|------------------|
| Linear memory with software bounds checks; every linear memory allocated with the full size WAMR checks against (section 5) | reading or writing firmware memory, other applications, peripherals |
| Pointer validation in every host function: the whole range `[ptr, ptr+len)` (all 8 bytes of a `double *`) must lie in the linear memory; NULL (offset 0) and zero lengths where data is needed are rejected; data is copied with `memcpy` (no alignment assumptions) | host memory corruption through crafted pointers; bad pointers return `UC_ERR_INVALID` instead of trapping |
| Import whitelist | only `bacnet_uc` and libc-builtin functions link; unknown imports fail the load |
| Permissions (`perms`) | `bacnet.local`: local objects (create, delete own, read, write, local COV); `bacnet.remote`: requests to other devices; `io`: raw channel access; `kv`: persistent storage. Without any permission an application can only log, read the uptime, change its tick period and read its parameters |
| Object ownership | an application deletes only its own objects; its objects disappear when it stops |
| Watchdog | endless loops: `wasm_runtime_terminate()` stops the instance at the next branch or call (interpreter; AOT only with `--enable-multi-thread`); on `native_sim` the instruction budget (interpreter only) |
| No native code by default | the interpreter executes modules; AOT (native code in executable RAM) is a build option with its own risk (section 2.1) |
| Resource limits | pool sized per board, `heap_kb`, `stack_kb`, 16 queued events, 20 log lines/s, 16 COV subscriptions, 8 client slots (shared), kv values ≤ 256 bytes, module ≤ `CONFIG_UC_APP_MAX_FILE_SIZE` (256 KiB) |
| Thread per application | a trap or a blocking call affects one application only |
| Integrity | optional `sha256` in the manifest, checked at install and at every start |

Known limits of the isolation:

- `bacnet.local` allows writing **any** writable local property, including
  IO outputs and other applications' objects. A per-object write ACL is
  **Planned**.
- The pool is shared: an application with a large memory can prevent another
  from starting (load time only; the instance size is fixed after start).
- CPU: an application may use up to 2 s of CPU per callback repeatedly;
  it starves only threads of lower priority (shell, logging).
- Integrity is not authenticity: whoever can use SMP can install code.
  Signed applications are **Planned** (section 10); SMP access control is
  described in [security.md](security.md).

## 8. Toolchains

### 8.1 C (supported)

`wasm/sdk/uc-cc` wraps clang (≥ 16, `wasm32` target and `wasm-ld`) with the
flags and the post-link ABI check described in
[`wasm/README.md`](../wasm/README.md#uc-cc):

```sh
wasm/sdk/uc-cc -o hello.wasm hello.c                    # 289 B, "linear memory 8192 B + heap 8192 B"
wasm/sdk/uc-wasm-info hello.wasm                        # imports, permissions (bacnet.local), memory
wasm/sdk/uc-aot --board nucleo_f767zi -o hello.aot hello.wasm   # optional, needs wamrc
```

| Tool | Usage | Notes |
|------|-------|-------|
| `uc-cc` | `uc-cc [-O z\|s\|0..3] [-I dir] [-D name[=val]] [-g] [--stack-size B] [--heap-kb N] [--no-page-align] [--allow-grow] -o app.wasm app.c [more.c ...]` | clang `--target=wasm32` with the ABI's flags (`-mcpu=mvp -msign-ext -mnontrapping-fptoint -mbulk-memory`, `-Oz` by default), links twice so that `__heap_base` is page-aligned, then checks the module against the host ABI (imports, signatures, features, no `memory.grow`); `--print-flags` prints the flags for other build systems (`wasm/sdk/Makefile.inc`, `wasm/sdk/cmake`) |
| `uc-wasm-info` | `uc-wasm-info app.wasm` | imports, exports, required permissions, memory layout and the linear memory the firmware will allocate |
| `uc-aot` | `uc-aot --board <board> [-o app.aot] [-O 0..3] app.wasm`; `uc-aot --list` | wamrc (`--wamrc`, `$WAMRC`, `/opt/wamrc/wamrc` or `PATH`, WAMR 2.4.5) with the board's target, `--bounds-checks=1`, `--enable-multi-thread`, indirect mode on Cortex-M; then checks the runtime symbols the file needs against WAMR's symbol map. Only useful with AOT firmware (section 2.1) |

The `hello.c` of the commands above:

```c
#include <bacnet_uc.h>

UC_APP_DECLARE()

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	return uc_obj_create_str(UC_OBJ_ANALOG_VALUE, 100, "uptime-s");
}

UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
	(void)uc_pv_write(UC_OBJ_ANALOG_VALUE, 100, (double)(now_ms / 1000u), UC_PRIORITY_NONE);
}
```

Host-side unit tests compile the same source natively against the host stub
(`wasm/sdk/host-stub`); `make -C wasm test`.

### 8.2 Rust (sketch, checked)

Target `wasm32v1-none` (Rust ≥ 1.84, `no_std`) generates WebAssembly 1.0
code without the reference-types and multivalue defaults of
`wasm32-unknown-unknown`. The following crate was built with Rust 1.94,
passes `uc-wasm-info` and runs in the host WAMR runner
(`uc-wamr-runner --perms bacnet.local --heap 0`):

`.cargo/config.toml`:

```toml
[build]
target = "wasm32v1-none"

[target.wasm32v1-none]
rustflags = [
  "-C", "link-arg=--export=__heap_base",
  "-C", "link-arg=--export=__data_end",
  "-C", "link-arg=--stack-first",
  # 8192 - 32: puts __heap_base on a 4 KiB boundary for this crate's 26 bytes
  # of data; check with uc-wasm-info and adjust
  "-C", "link-arg=-zstack-size=8160",
  "-C", "link-arg=--initial-memory=65536",
  "-C", "link-arg=--max-memory=65536",
  "-C", "target-feature=+sign-ext,+nontrapping-fptoint,+bulk-memory",
]
```

`Cargo.toml`: `crate-type = ["cdylib"]`, release profile with
`opt-level = "z"`, `lto = true`, `panic = "abort"`.

`src/lib.rs`:

```rust
#![no_std]

#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    core::arch::wasm32::unreachable()
}

#[link(wasm_import_module = "bacnet_uc")]
extern "C" {
    fn uc_log(level: i32, msg: *const u8, len: u32);
    fn uc_obj_create(obj_type: u32, instance: u32, name: *const u8, name_len: u32) -> i32;
    fn uc_prop_write(obj_type: u32, instance: u32, prop: u32, index: i32, value: f64,
                     priority: u32) -> i32;
}

#[no_mangle]
pub extern "C" fn uc_app_api_version() -> u32 {
    1 << 16 // UC_API_VERSION 1.0
}

#[no_mangle]
pub extern "C" fn uc_app_init() -> i32 {
    let msg = b"hello from Rust";
    let name = b"rust-uptime";
    unsafe {
        uc_log(3, msg.as_ptr(), msg.len() as u32);
        uc_obj_create(2, 100, name.as_ptr(), name.len() as u32)
    }
}

#[no_mangle]
pub extern "C" fn uc_app_tick(now_ms: u64) {
    unsafe {
        uc_prop_write(2, 100, 85, -1, (now_ms / 1000) as f64, 0);
    }
}
```

Result: 772-byte module, linear memory 8 KiB with `heap_kb: 0`. A crate
that needs an allocator provides a `#[global_allocator]` over a static
buffer (the WAMR app heap is only reachable through libc-builtin `malloc`).
Bindings for the whole ABI can be generated from `bacnet_uc.h` with
`bindgen`; that is **Planned** as an SDK crate.

### 8.3 AssemblyScript and TinyGo (notes, not verified)

| Toolchain | Imports / exports | Points to watch |
|-----------|-------------------|-----------------|
| AssemblyScript | `@external("bacnet_uc", "uc_log") declare function uc_log(level: i32, msg: usize, len: u32): void;` exported functions with `export function uc_app_init(): i32` | the default runtime imports `env.abort` with a signature that differs from libc-builtin: build with `--use abort=` or a custom abort; strings are UTF-16, convert with `String.UTF8.encode()`; use `--runtime stub` or `minimal` and check that no `memory.grow` is emitted (otherwise the memory is not shrunk: `--initialMemory 1 --maximumMemory 1`) |
| TinyGo | `//go:wasmimport bacnet_uc uc_log` and `//export uc_app_init` | target `wasm-unknown` (no WASI), `-scheduler=none`, `-gc=leaking` or `conservative` with a fixed heap; module sizes are larger (tens of KiB); check the result with `uc-wasm-info` |

## 9. Deployment

```mermaid
sequenceDiagram
    participant D as developer / harness
    participant N as node (SMP)
    participant M as app manager
    D->>D: uc-cc -o pid.wasm pid.c (optionally uc-aot per board)
    D->>N: fs upload /lfs/apps/pid.wasm (SMP group 8, chunks)
    D->>N: fs hash /lfs/apps/pid.wasm (sha256, optional check)
    D->>N: uc_app install {name, file, perms, params, sha256, restart}
    N->>M: validate file (size, sha256, magic), write apps.json
    M->>M: start (autostart): load, link, version, uc_app_init
    N-->>D: rc 0
    D->>N: uc_app status {name}
    N-->>D: state running, ticks, errors, last_error
```

| Operation | Commands |
|-----------|----------|
| first install | upload module, `uc_app install` |
| update code | upload the new module (same or new file name), `uc_app install` with the same name and `"restart": true` (rc `STATE` without it while running) |
| change parameters | `uc_app install` with the new `params` and `"restart": true` (no upload) |
| stop / start | `uc_app stop`, `uc_app start` |
| remove | `uc_app remove {"name": ..., "delete_file": true}` (also deletes `/lfs/data/<name>`) |
| bulk / declarative | upload the modules and `/lfs/cfg/apps.json.new` (staged), then `uc_node reload apps` (activates the document, stops removed or changed apps, starts autostart apps) |
| large manifest | an `install` request must fit one SMP request (1024 bytes over UDP); an entry with many or long `params` goes through `apps.json` as above (the harness does this automatically) |

The harness performs these steps with change detection by SHA-256, see
[harness-mcp.md](harness-mcp.md). Over the shell: `uc app list|start|stop|remove`.

## 10. Versioning and signing

`UC_API_VERSION` = major << 16 | minor (currently 1.0). `uc_node info`
reports the host's version as `api` (65536 for 1.0).

| Change | Version |
|--------|---------|
| new host function, new constant, new optional export | minor + 1. An application that uses a new function fails to start on an older host with "unresolved import bacnet_uc.<name>" |
| changed signature or semantics of an existing function, removed function | major + 1. The host refuses applications with a different major ("API x.y not supported") |

**Planned: signed applications.** Today the manifest `sha256` protects
against corrupted transfers only. The plan:

1. A detached signature file `/lfs/apps/<file>.sig` (ECDSA P-256 over the
   SHA-256 of the module, the application name and the requested
   permissions), produced by the harness or a CI signing step.
2. Trusted public keys provisioned in the firmware image or in `/lfs/cfg/keys/`
   (the latter itself protected by SMP access control).
3. Verification with PSA Crypto at install and at every start; a policy
   option (`CONFIG_UC_APP_REQUIRE_SIGNATURE`) rejects unsigned modules with
   rc `VERIFY`.
4. Permissions granted only if the signing key is allowed to grant them.
