# Storage and logging

Where the firmware and its data live in flash, how the LittleFS volume at
`/lfs` is configured and organised, how logs are produced, stored, rotated and
retrieved, and what to consider for flash wear and power loss.

Implementation: [`firmware/src/storage/uc_storage.c`](../firmware/src/storage/uc_storage.c)
(API [`uc_storage.h`](../firmware/include/uc/uc_storage.h)), fstab nodes in
[`firmware/boards/*.overlay`](../firmware/boards/) and
[`snippets/uc-ramfs`](../snippets/uc-ramfs/), logging options in
[`firmware/prj.conf`](../firmware/prj.conf) and
[`firmware/overlay-syslog.conf`](../firmware/overlay-syslog.conf).

## 1. Flash layouts

Two build variants exist:

| Variant | Command | Firmware location | Firmware update |
|---------|---------|-------------------|-----------------|
| plain | `west build -b <board> BACNet-uc/firmware` | linked at the start of the internal flash; the board's MCUboot partitions are ignored | by debug probe only (`west flash`) |
| MCUboot | `west build --sysbuild -b <board> BACNet-uc/firmware` ([`sysbuild.conf`](../firmware/sysbuild.conf)) | MCUboot in `boot_partition`, signed firmware in `slot0_partition` | SMP image group (upload to slot 1, test, reset, confirm) |

The file system is on a separate flash device on both boards, so the two
variants have the same `/lfs`.

### 1.1 NUCLEO-F767ZI

Internal flash (2 MiB at 0x0800_0000, single bank), partitions of the board
DTS:

| Offset | Size | Sectors | Partition | plain build | MCUboot build |
|--------|------|---------|-----------|-------------|---------------|
| 0x00_0000 | 64 KiB | 0-1 (32 KiB) | `boot_partition` | firmware (from offset 0, spans all partitions it needs) | MCUboot |
| 0x01_0000 | 64 KiB | 2-3 (32 KiB) | `storage_partition` | overlapped by the firmware, unused | unused |
| 0x02_0000 | 128 KiB | 4 | - | | unused |
| 0x04_0000 | 768 KiB | 5-7 (256 KiB) | `slot0_partition` | | firmware (primary slot) |
| 0x10_0000 | 768 KiB | 8-10 | `slot1_partition` | | update image (secondary slot) |
| 0x1C_0000 | 256 KiB | 11 | `scratch_partition` | | swap scratch |

MCUboot mode on the F767 is **swap using scratch**
([`Kconfig.sysbuild`](../firmware/Kconfig.sysbuild)): with 256 KiB sectors,
swap-using-offset would need one spare sector in the secondary slot and limit
the firmware to 512 KiB; with scratch the full 768 KiB slot is usable.

External SPI NOR (W25Q128JV, 16 MiB, `nucleo_f767zi.overlay`):

| Offset | Size | Partition | Use |
|--------|------|-----------|-----|
| 0x000000 | 16 MiB | `uc_storage_partition` ("uc-storage") | `/lfs` |

### 1.2 FRDM-MCXN947

Internal flash (2 MiB, 8 KiB sectors):

| Offset | Size | Partition | plain build | MCUboot build |
|--------|------|-----------|-------------|---------------|
| 0x00_0000 | 80 KiB | `boot_partition` | firmware (from offset 0) | MCUboot |
| 0x01_4000 | 984 KiB | `slot0_partition` | (overlapped) | firmware |
| 0x10_A000 | 984 KiB | `slot1_partition` | unused | update image |

MCUboot mode: **swap using offset** (the Zephyr 4.4 default; the twister
scenario sets `SB_CONFIG_MCUBOOT_MODE_SWAP_USING_OFFSET=y` explicitly). The
`slot1_partition` is also the code partition of cpu1 in Zephyr's dual-core
samples; BACnet-uc does not use cpu1.

External QSPI NOR (W25Q64JV on FlexSPI, board DTS):

| Offset | Size | Partition | Use |
|--------|------|-----------|-----|
| 0x000000 | 8 MiB | `storage_partition` | `/lfs` |

### 1.3 native_sim

Simulated flash (2 MiB, 4 KiB erase blocks, backed by `flash.bin` in the
working directory or `--flash=<file>`):

| Offset | Size | Partition | Use |
|--------|------|-----------|-----|
| 0x00_0000 | 48 KiB | `boot_partition` | unused |
| 0x00_C000 | 420 KiB | `slot0_partition` | unused (the executable does not run from flash) |
| 0x07_5000 | 1580 KiB | `storage_partition` (overlay) | `/lfs` |

### 1.4 RAM file system (snippet `uc-ramfs`)

64 KiB of RAM with 1 KiB erase blocks, mounted at `/lfs` instead of the board
entry; erased at every boot. See [hardware.md](hardware.md#42-ram-file-system-snippet-uc-ramfs).

## 2. LittleFS parameters

The fstab node `uc_lfs` (compatible `zephyr,fstab,littlefs`) of each overlay:

| Parameter | F767 (SPI NOR) | MCXN947 (FlexSPI NOR) | native_sim | `uc-ramfs` |
|-----------|---------------:|---------------------:|-----------:|-----------:|
| mount point | `/lfs` | `/lfs` | `/lfs` | `/lfs` |
| `read-size` / `prog-size` | 16 / 16 | 16 / 16 | 16 / 16 | 16 / 16 |
| `cache-size` | 256 | 256 | 256 | 256 |
| `lookahead-size` (bytes; ×8 blocks per scan) | 256 | 256 | 64 | 32 |
| `block-cycles` (wear-leveling eviction) | 512 | 512 | 512 | 512 |
| block size (from the flash page layout) | `CONFIG_SPI_NOR_FLASH_LAYOUT_PAGE_SIZE`: 64 KiB by default, 4 KiB recommended | 4 KiB | 4 KiB | 1 KiB |
| block count | 256 (64 KiB) / 4096 (4 KiB) | 2048 | 395 | 64 |
| automount / format | `automount`, `no-format` | same | same | same |

Global options in `prj.conf`: `CONFIG_FS_LITTLEFS_NUM_FILES=8` (open files:
log backend, SMP file transfer, configuration, application module, kv store),
`CONFIG_FS_LITTLEFS_NUM_DIRS=4`, `CONFIG_FS_LITTLEFS_CACHE_SIZE=256` (must
equal the largest fstab `cache-size`).

Mounting: the fstab entry is mounted during boot without formatting. If that
fails (blank or corrupted flash), `uc_storage_init()` formats the partition
with the same parameters and mounts again (`CONFIG_UC_STORAGE_FORMAT_ON_FAIL=y`).
A first boot on blank flash therefore logs mount errors from the automount
(`Corrupted dir pair at {0x0, 0x1}`, `fs mount error (-14)`) followed by
`formatting /lfs` and `/lfs ready`.

Small files are stored inline in their directory's metadata block. The
LittleFS default limit is the smaller of the cache size (256 bytes) and 1/8
of the block size: 256 bytes with 4 KiB blocks, 128 bytes with the 1 KiB
blocks of `uc-ramfs`. Key/value entries (at most 256 bytes) therefore do not
occupy a block of their own on the flash-backed volumes.

## 3. Directory layout

| Path | Written by | Content |
|------|------------|---------|
| `/lfs/cfg/device.json` | SMP upload, harness | device identity, network, BACnet options |
| `/lfs/cfg/io.json` | SMP upload, harness | IO point mapping |
| `/lfs/cfg/apps.json` | firmware on `uc_app install/remove`; SMP upload | installed applications |
| `/lfs/cfg/*.tmp` | firmware | transient: atomic replace of `apps.json` |
| `/lfs/apps/<name>.wasm`, `.aot` | SMP upload | application modules (file name stem 1..40 characters of `[A-Za-z0-9_.-]`) |
| `/lfs/data/<app>/<key>` | applications (`uc_kv_set`) | key/value store, one file per key; `<key>~` transient during a write |
| `/lfs/log/log.NNNN` | log backend | rotating log files, `NNNN` = 0000..9999 |

The four directories are created at boot if missing. Planned additions:
`/lfs/cert` (BACnet/SC and DTLS certificates), `/lfs/cfg/keys` (application
signing keys).

Space budget (MCXN947, 8 MiB): configuration < 3 × 8 KiB
(`CONFIG_UC_CONFIG_DOC_MAX`), applications ≤ 4 × 256 KiB
(`CONFIG_UC_APP_MAX_FILE_SIZE`), logs 4 × 16 KiB, key/value data a few KiB.
LittleFS needs free blocks for copy-on-write; keep at least 10 % free
(`uc_node info` → `fs.free`).

## 4. Configuration documents and atomicity

| Writer | Method | Power loss during the write |
|--------|--------|-----------------------------|
| firmware (`apps.json`) | `uc_storage_write_file()`: write `<path>.tmp`, `fs_sync()`, rename over `<path>` (one metadata commit) | old or new file |
| applications (kv) | write `<key>~`, rename | old or new value |
| SMP file upload (group 8) | writes the destination file chunk by chunk | **partial file**: at the next boot the document is rejected and its defaults are used (for `device.json`: default instance, DHCP) |

Power-safe upload of a configuration document over SMP: upload to
`/lfs/cfg/<doc>.json.new`, then move it with the shell group
(`fs mv /lfs/cfg/<doc>.json.new /lfs/cfg/<doc>.json`), then `uc_node reload`.
LittleFS renames atomically. Documents are read at boot and on `reload`
only; see [configuration.md](configuration.md#4-apply-and-reload-semantics).

## 5. Logging

### 5.1 Sources and levels

Every firmware module registers a log source (`uc_main`, `uc_storage`,
`uc_config`, `uc_net`, `uc_bn_node`, `uc_bn_local`, `uc_bn_client`,
`uc_bn_cov`, `uc_io`, `uc_app_mgr`, `uc_app`, `uc_mgmt_*`, `uc_shell`) with
`CONFIG_UC_LOG_LEVEL` (compiled at debug level). Zephyr subsystems and
bacnet-stack have their own sources. Application log lines come from the
source `uc_app` as `"<app>: <message>"`.

| Setting | Value | Where |
|---------|-------|-------|
| mode | deferred (messages are formatted by the logging thread) | `CONFIG_LOG_MODE_DEFERRED` |
| buffer | 4 KiB; the oldest messages are overwritten when full | `CONFIG_LOG_BUFFER_SIZE`, `CONFIG_LOG_MODE_OVERFLOW` |
| processing | logging thread (priority 14) wakes every 1000 ms or after 10 pending messages | `CONFIG_LOG_PROCESS_THREAD_SLEEP_MS`, `CONFIG_LOG_PROCESS_TRIGGER_THRESHOLD` |
| runtime level | `device.json` `log.level` (`err`, `wrn`, `inf`, `dbg`; default `inf`) applied to all sources at boot | `main.c`, `CONFIG_LOG_RUNTIME_FILTERING` |
| timestamps | uptime `[hh:mm:ss.mmm,uuu]` | no wall clock yet |

The runtime level is applied once at boot; `uc_node reload device` does not
change it (reboot after changing `log.level`).

### 5.2 Backends

| Backend | Enabled | Output | Notes |
|---------|---------|--------|-------|
| shell (console UART) | always (`CONFIG_SHELL_LOG_BACKEND`) | colored text on the console, 115200 8N1 | shares the UART with the shell and the SMP shell transport |
| file system | always (`CONFIG_LOG_BACKEND_FS`) | text without colors in `/lfs/log/log.NNNN` | starts writing once `/lfs` is mounted; earlier messages reach the UART only |
| network (syslog) | with `overlay-syslog.conf` | RFC 5424 over UDP (default port 514) or TCP | see 5.4 |

### 5.3 File rotation

| Option | Value |
|--------|-------|
| directory | `/lfs/log` (`CONFIG_LOG_BACKEND_FS_DIR`) |
| file name | `log.` + 4-digit number (`CONFIG_LOG_BACKEND_FS_FILE_PREFIX`) |
| file size | 16 KiB (`CONFIG_LOG_BACKEND_FS_FILE_SIZE`; 4 KiB with `uc-ramfs`) |
| files kept | 4 (`CONFIG_LOG_BACKEND_FS_FILES_LIMIT`; 2 with `uc-ramfs`) |
| full | the oldest file is deleted (`CONFIG_LOG_BACKEND_FS_OVERWRITE`) |
| after reboot | appends to the newest file while it has room (`CONFIG_LOG_BACKEND_FS_APPEND_TO_NEWEST_FILE`) |
| durability | `fs_sync()` after every processing batch of the logging thread |

The highest number is the newest file; numbering wraps after 9999. About
48..64 KiB of recent log text are kept.

### 5.4 Remote logging (syslog)

```sh
west build -b frdm_mcxn947/mcxn947/cpu0 BACNet-uc/firmware -- \
    -DEXTRA_CONF_FILE=overlay-syslog.conf \
    '-DCONFIG_LOG_BACKEND_NET_SERVER="192.168.10.10:514"'
```

With DHCPv4, the log server option (option 7) of the DHCP server overrides
the configured address. The syslog HOSTNAME is `bacnet-uc-` followed by the
last bytes of the MAC address. The backend starts when the network is up. A
minimal receiver on the development host: `nc -klu 514`, or rsyslog with a UDP
input.

### 5.5 Retrieving logs

| Method | Command |
|--------|---------|
| console | serial terminal on the board's VCP (`/dev/ttyACM0`, 115200 8N1) |
| SMP file group | `smpmgr --ip <node> file download /lfs/log/log.0000 log.0000.txt` |
| list files | SMP shell group: `smpmgr --ip <node> shell "fs ls /lfs/log"` |
| harness | `bacnet-uc node logs <node> [-n N] [--grep RE]`, MCP tool `read_logs`: lists `/lfs/log`, downloads the newest files, returns the last N lines, optional regular-expression filter ([harness-mcp.md](harness-mcp.md)) |
| syslog | the syslog server |

A file that the backend is still writing can be downloaded; it contains the
data up to the last sync (at most about one second old).

## 6. Wear and power loss

### 6.1 Flash endurance

| Flash | Endurance (datasheet) | Used for |
|-------|-----------------------|----------|
| STM32F767 internal | 10 000 cycles per sector | firmware only |
| MCXN947 internal | see NXP datasheet | firmware only |
| W25Q64JV / W25Q128JV | 100 000 cycles per 4 KiB sector | `/lfs` |

LittleFS levels wear dynamically: a metadata block is moved after
`block-cycles` (512) erases, and file data is written copy-on-write to newly
allocated blocks spread over the whole volume.

### 6.2 Log writes dominate wear

Appending to a LittleFS file after a sync copies the incomplete last block
of the file into a freshly erased block (`lfs_ctz_extend()`). Every sync of
the log backend therefore costs one block erase, plus metadata commits.

| Log activity | Erases per day | Blocks | Lifetime estimate (perfect leveling, 100 000 cycles) |
|--------------|---------------:|-------:|------------------------------------|
| one batch per second, continuously (debug level, busy node) | 86 400 | MCXN947: 2048 × 4 KiB | ~6.5 years |
| same | 86 400 | F767, 4 KiB blocks: 4096 | ~13 years |
| same | 86 400 | F767, 64 KiB blocks (current default): 256 | **~10 months** |
| one batch per minute (info level, steady state) | 1 440 | any of the above | > 40 years |

Recommendations:

- Run production nodes at `log.level` `inf` or `wrn`; use `dbg` for
  diagnosis only.
- On the F767 set `CONFIG_SPI_NOR_FLASH_LAYOUT_PAGE_SIZE=4096` so that
  LittleFS uses 4 KiB blocks.
- For verbose logging use syslog and disable the file backend
  (`CONFIG_LOG_BACKEND_FS=n`), or raise `CONFIG_LOG_PROCESS_THREAD_SLEEP_MS`
  (fewer, larger syncs; more messages lost on power failure).
- Applications should persist state on change, not periodically: every
  `uc_kv_set` is a metadata commit (and a rename), and the directory's
  metadata block is erased whenever it is compacted.

### 6.3 Power loss

| Data | Behaviour on power loss |
|------|-------------------------|
| file system structure | LittleFS is power-loss resilient: metadata commits are atomic, a mount after power loss finds the last committed state |
| `apps.json`, kv values | old or new version (atomic rename) |
| documents uploaded over SMP | possibly partial, rejected at boot (section 4) |
| log files | messages not yet synced (up to ~1 s) are lost |
| application modules being uploaded | partial file; `install` rejects it (size, magic, optional `sha256`); an installed app whose file was being replaced fails its next start |
| firmware update (MCUboot) | swap is resumable; a test image that is not confirmed is reverted at the next reset |
| BACnet state (Present_Value, priority arrays) | lost, see [bacnet.md](bacnet.md#35-persistence) |
