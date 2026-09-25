# Getting started

From an empty Linux machine to a running node with configuration, IO points
and a WebAssembly application, first on `native_sim` (no hardware), then on
the NUCLEO-F767ZI and the FRDM-MCXN947, and finally with the MCP server for
an AI agent.

Commands marked with ✔ were run as written for the current repository state
(Zephyr 4.4.2, SDK 1.0.1, Python 3.12, clang 18, smpmgr 0.19.1, on x86-64
Linux); see the note at the end for what could not be run in that
environment.

## 1. Prerequisites

| Item | Version | Needed for |
|------|---------|------------|
| Linux x86-64 host (Ubuntu 22.04/24.04 tested by Zephyr) | | everything |
| Python | **3.12** or newer (Zephyr 4.4 requires ≥ 3.12) | west, Zephyr build, harness |
| CMake, Ninja, dtc, git | Zephyr requirements | firmware build |
| Zephyr SDK | 1.0.1, `arm-zephyr-eabi` toolchain | MCU builds (`native_sim` uses the host gcc) |
| clang + wasm-ld | ≥ 16 with the `wasm32` target (Ubuntu `clang`, `lld`) | WebAssembly applications |
| STM32CubeProgrammer or OpenOCD | | flashing the NUCLEO-F767ZI |
| NXP LinkServer (or J-Link, pyOCD) | | flashing the FRDM-MCXN947 |
| serial terminal (`picocom`, `minicom`) | | console |

## 2. Workspace

The repository is the west manifest repository (T2 topology); the workspace
top directory contains it and the imported projects.

```sh
mkdir bacnet-uc-ws && cd bacnet-uc-ws
git clone https://github.com/IlijaVorontsov/BACNet-uc
python3.12 -m venv .venv && . .venv/bin/activate
pip install west
west init -l BACNet-uc                      # uses BACNet-uc/west.yml
west update --narrow -o=--depth=1           # Zephyr v4.4.2, HALs, bacnet-stack, WAMR
pip install -r zephyr/scripts/requirements-base.txt
pip install -e 'BACNet-uc/harness[serial,sim]'   # bacnet-uc CLI and bacnet-uc-mcp
```

Resulting layout:

```
bacnet-uc-ws/
├── BACNet-uc/          this repository (firmware/, wasm/, harness/, schemas/, docs/)
├── zephyr/             Zephyr v4.4.2
├── modules/lib/bacnet  bacnet-stack-zephyr (+ stack/ = bacnet-stack)
├── modules/lib/wamr    WAMR 2.4.5
├── modules/hal/...     cmsis, cmsis_6, hal_stm32, hal_nxp
└── bootloader/mcuboot
```

✔ `west list` shows the projects of section "Versions" in [README.md](README.md).

### Zephyr SDK (ARM toolchain only)

```sh
mkdir -p ~/zsdk && cd ~/zsdk
base=https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v1.0.1
curl -sSLO $base/zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz
curl -sSLO $base/toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz
tar xf zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz
mkdir -p zephyr-sdk-1.0.1/gnu
tar xf toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz -C zephyr-sdk-1.0.1/gnu
zephyr-sdk-1.0.1/setup.sh -h                # installs host tools; "-h" does not print help
export ZEPHYR_SDK_INSTALL_DIR=~/zsdk/zephyr-sdk-1.0.1
```

The SDK 1.0 layout puts the toolchains under `<sdk>/gnu/<triple>`.

## 3. Build the firmware

Run `west build` from the workspace top directory. The build directory is
free to choose; `-p always` forces a pristine build.

```sh
cd bacnet-uc-ws
west build -b native_sim/native/64       BACNet-uc/firmware -d build-native   # ✔
west build -b nucleo_f767zi              BACNet-uc/firmware -d build-f767     # ✔ RAM 91.5 %, DTCM 97.1 %
west build -b frdm_mcxn947/mcxn947/cpu0  BACNet-uc/firmware -d build-mcxn     # ✔ RAM 91.1 %, SRAMX 100 %
```

Variants:

| Variant | Command |
|---------|---------|
| F767 without the SPI NOR module (volatile `/lfs`) | `west build -b nucleo_f767zi BACNet-uc/firmware -d build-f767-ram -S uc-ramfs` ✔ (the 64 KiB RAM disk is taken from the kernel heap and the `malloc` arena; RAM 96.1 %) |
| remote logging | `west build -b <board> BACNet-uc/firmware -d build-x -- -DEXTRA_CONF_FILE=overlay-syslog.conf '-DCONFIG_LOG_BACKEND_NET_SERVER="192.168.10.10:514"'` |
| MCUboot + firmware update over SMP | `west build --sysbuild -b <board> BACNet-uc/firmware -d build-x-mcuboot` ✔ (both boards) |
| all build scenarios (twister) | `west twister -T BACNet-uc/firmware -p native_sim/native/64 -p nucleo_f767zi -p frdm_mcxn947/mcxn947/cpu0` |

The harness wraps the same commands: `bacnet-uc firmware build <board>
[--sysbuild] [-S snippet]`.

## 4. First node on `native_sim`

`native_sim` builds a Linux executable. Its sockets are host sockets: SMP
listens on UDP 1337 and BACnet/IP on UDP 47808 of the host, and `/lfs` is a
file.

```sh
mkdir -p ~/uc-sim && cd ~/uc-sim
~/bacnet-uc-ws/build-native/zephyr/zephyr.exe --flash=node1.flash.bin     # ✔
```

Expected output (abridged, ✔):

```
uart connected to pseudotty: /dev/pts/0
<err> littlefs: ...: Corrupted dir pair at {0x0, 0x1}   (first start only: blank flash,
<err> fs: fs mount error (-14)                             the automount fails and
<err> littlefs: Error mounting filesystem: at /lfs: -14    uc_storage formats /lfs)
*** Booting Zephyr OS build ... ***
<inf> uc_main: BACnet-uc 0.1.0 on native_sim/native/64, WASM API 1.0
<wrn> uc_storage: /lfs not mounted at boot (no valid file system)
<wrn> uc_storage: formatting /lfs
<inf> uc_storage: /lfs ready: 1580 KiB total, 1540 KiB free
<inf> uc_config: /lfs/cfg/device.json not found, using defaults
<inf> uc_io: IO catalog native_sim: 8 channel(s), 8 ready
<inf> uc_app_mgr: WAMR fast interpreter, pool 262144 bytes (RAM), 0 apps installed
<inf> uc_mgmt: SMP groups registered: 64 uc_app, 65 uc_io, 66 uc_node
<inf> uc_main: ready: device 260001, IPv4 127.0.0.1, BACnet/IP UDP 47808, SMP UDP 1337
<inf> uc_bn_node: ReinitializeDevice/DCC refused: no bacnet.password
<inf> uc_bn_node: BACnet/IP 127.0.0.1:47808, device 260001
```

The interactive shell is on the pseudo-terminal (`picocom /dev/pts/0`). Stop
the node with Ctrl-C. A reboot (`os reset`, `kernel reboot cold`, BACnet
ReinitializeDevice) restarts the process in place with the same arguments
(`native_sim_reboot: Restarting process.`). Useful options: `--flash_erase`
(start empty; also applied again at every in-place restart), `--flash_rm`
(delete the file on exit). Several simulated nodes on one host
need network namespaces or containers: see [simulation.md](simulation.md) and
`bacnet-uc sim up`.

## 5. Flash a board

### NUCLEO-F767ZI

1. Wire a W25Q128JV (or similar 3.3 V SPI NOR) module to D13/D12/PB5/D10
   ([hardware.md](hardware.md#41-spi-nor-on-the-nucleo-f767zi)), or build with
   `-S uc-ramfs`.
2. Connect the RJ45 Ethernet port to a network with a DHCP server, and the
   ST-LINK USB port (CN1).
3. Flash and open the console:

   ```sh
   west flash -d build-f767                 # STM32CubeProgrammer; or: west flash -d build-f767 -r openocd
   picocom -b 115200 /dev/ttyACM0
   ```

### FRDM-MCXN947

1. Connect Ethernet and the MCU-Link USB-C port. `/lfs` is on the on-board
   8 MiB QSPI flash.
2. Flash and open the console:

   ```sh
   west flash -d build-mcxn                 # LinkServer; or: -r jlink / -r pyocd
   picocom -b 115200 /dev/ttyACM0
   ```

On both boards the console shows the same boot log as `native_sim`; the
`ready:` line contains the IPv4 address obtained by DHCP. `uc info` in the
shell prints the node state.

## 6. First contact over SMP

Any SMP client works for the standard groups. Three options:

**Shell on the console** (all boards):

```
uart:~$ uc info
```

**smpmgr** (Intercreate, `pip install smpmgr`) ✔:

```sh
smpmgr --ip 127.0.0.1 os echo hello
smpmgr --ip 127.0.0.1 shell "uc info"
```

```
fw:      0.1.0
board:   native_sim/native/64 (io catalog: native_sim)
uptime:  4 s
device:  260001 "bacnet-uc" (bacnet ready)
net:     ipv4 127.0.0.1, bacnet/ip udp 47808
bacnet:  0 packets, 2 objects
fs:      /lfs 1580 KiB total, 1536 KiB free
apps:    0 installed, 0 running
wasm:    interp yes, aot no, pool 261368/261760 bytes free
```

File uploads with smpmgr over UDP need `--mtu 1024` (the node's
`CONFIG_MCUMGR_TRANSPORT_UDP_MTU`); without it multi-frame uploads fail with
`rc 9` (`MGMT_ERR_ECORRUPT`) ✔. smpmgr has no support for the custom groups
64..66; use the shell group (`smpmgr shell "uc ..."`) or the harness for them.

**Harness CLI** ✔:

```sh
export BACNET_UC_HOME=~/uc-sim               # inventory in ~/uc-sim/.bacnet-uc/
bacnet-uc node add sim1 --udp 127.0.0.1 --bacnet 127.0.0.1:47808 --board native_sim/native/64
bacnet-uc node info sim1
bacnet-uc node discover --target 127.0.0.1:47808   # BACnet Who-Is / I-Am
```

For a board use its address: `bacnet-uc node add f767 --udp 192.168.10.51
--board nucleo_f767zi`, or the console UART: `--serial /dev/ttyACM0`.

## 7. Configure the node

### Device identity

Give every node a unique device instance before a second node joins the
network. `device-sim.json`:

```json
{"schema": 1,
 "device": {"instance": 1001, "name": "uc-sim-1", "description": "native_sim node", "location": "desk"},
 "bacnet": {"udp_port": 47808, "password": "change-me"},
 "log": {"level": "inf"}}
```

`bacnet.password` (1..20 characters) is what BACnet clients must send with
DeviceCommunicationControl and ReinitializeDevice; without it the node
refuses both ([bacnet.md](bacnet.md#7-network-security)).

With the harness (validates against the schema, uploads, reloads) ✔:

```sh
bacnet-uc config set sim1 device device-sim.json    # ... reboot_required: true
```

With plain SMP, power-safe (upload as a staged `.new` document, reload: the
node validates it and renames it over `device.json`, or deletes it if it is
invalid) ✔:

```sh
smpmgr --ip 127.0.0.1 --mtu 1024 file upload device-sim.json /lfs/cfg/device.json.new
smpmgr --ip 127.0.0.1 shell "uc cfg reload device"     # device.json reloaded, reboot required ...
smpmgr --ip 127.0.0.1 shell "uc cfg show device"       # ... password: configured
```

The name, the password and the log level are applied at once; the instance
change reports `reboot required`. Restart the node ✔:

```sh
smpmgr --ip 127.0.0.1 os reset                         # native_sim: the process restarts in place
bacnet-uc node info sim1                               # device 1001 after a few seconds
```

### IO points

The example [`schemas/examples/io.json`](../schemas/examples/io.json) binds
`di0`, `do0`, `ai0`, `ao0` of any of the three catalogs. From the workspace
top (the paths below are relative to it; `BACNET_UC_HOME` still points at
`~/uc-sim`) ✔:

```sh
cd ~/bacnet-uc-ws
bacnet-uc config set sim1 io BACNet-uc/schemas/examples/io.json   # staged upload + reload
bacnet-uc io force sim1 ai0 2500                 # simulated 2500 mV
bacnet-uc prop read sim1 analog-input:1          # 2500 * 0.1 - 50 = 200.0 (°C)
bacnet-uc prop write sim1 binary-output:1 1 --priority 8
bacnet-uc io read sim1 do0                       # 1.0
bacnet-uc node objects sim1
```

Shell equivalents: `uc io force ai0 2500`, `uc io read`, `uc obj list`.

## 8. Build and deploy applications

```sh
make -C BACNet-uc/wasm                           # ✔ build/blinky.wasm thermostat.wasm alarm.wasm uc-link.wasm
make -C BACNet-uc/wasm test                      # ✔ host unit tests of the examples and the SDK
```

Deploy with the harness (upload, install, start) ✔:

```sh
bacnet-uc app deploy sim1 blinky BACNet-uc/wasm/build/blinky.wasm \
    --perm bacnet.local --param type=5 --param instance=100 --heap 0
bacnet-uc app list sim1                          # state: running, ticks
bacnet-uc prop read sim1 binary-value:100        # toggles every second
```

To build from C source with the harness: `bacnet-uc app build blinky
BACNet-uc/wasm/examples/blinky/blinky.c -o out/` (runs `uc-cc`, prints
imports, exports and the permissions the module needs) ✔.

Deploy with plain SMP (upload the module and a staged `apps.json`, reload) ✔:

```sh
smpmgr --ip 127.0.0.1 --mtu 1024 file upload BACNet-uc/wasm/build/thermostat.wasm /lfs/apps/thermostat.wasm
smpmgr --ip 127.0.0.1 --mtu 1024 file upload apps.json /lfs/cfg/apps.json.new
smpmgr --ip 127.0.0.1 shell "uc cfg reload apps"
smpmgr --ip 127.0.0.1 shell "uc app list"              # thermostat running
```

The uploaded `apps.json` replaces the whole list, so the `blinky` installed
above by the harness is stopped and removed by this reload.

`apps.json` for the thermostat (sensor `analog-input:1` and output
`analog-output:1` from the IO example; setpoint object created by the app):

```json
{"schema": 1, "apps": [
  {"name": "thermostat", "file": "/lfs/apps/thermostat.wasm", "heap_kb": 0,
   "perms": ["bacnet.local", "kv"], "params": [{"key": "setpoint", "value": "21.5"}]}
]}
```

Application parameters of all examples: [`wasm/README.md`](../wasm/README.md#examples).
Writing your own: [wasm-runtime.md](wasm-runtime.md#8-toolchains).

## 9. Logs

```sh
bacnet-uc node logs sim1 -n 20                    # ✔ last 20 lines of /lfs/log
bacnet-uc node logs sim1 --grep uc_app
smpmgr --ip 127.0.0.1 shell "fs ls /lfs/log"      # ✔
smpmgr --ip 127.0.0.1 file download /lfs/log/log.0000 log.0000.txt   # ✔
```

Details: [storage-and-logging.md](storage-and-logging.md#5-logging).

## 10. The MCP server

The harness exposes 36 tools (nodes, configuration, IO, BACnet properties,
applications, logs, firmware, system manifests, simulation) to an MCP client:

```sh
bacnet-uc-mcp --home ~/uc-sim                      # stdio (default) ✔
bacnet-uc-mcp --home ~/uc-sim --http --port 8000   # streamable HTTP on 127.0.0.1:8000/mcp
```

Claude Code, project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "bacnet-uc": {
      "command": "/home/me/bacnet-uc-ws/.venv/bin/bacnet-uc-mcp",
      "args": ["--home", "/home/me/uc-sim"]
    }
  }
}
```

or `claude mcp add bacnet-uc -- /home/me/bacnet-uc-ws/.venv/bin/bacnet-uc-mcp
--home /home/me/uc-sim`. The shell tool (`node_shell`) is disabled unless
the server runs with `--allow-shell` or `BACNET_UC_ALLOW_SHELL=1`. Tool
reference and agent workflows: [harness-mcp.md](harness-mcp.md); building a
distributed application from a manifest: [distributed-apps.md](distributed-apps.md).

## 11. Tests

| Test | Command |
|------|---------|
| firmware unit tests (ztest, `native_sim`) | `west build -b native_sim/native/64 BACNet-uc/firmware/tests/unit -d build-unit -t run` ✔ (32 tests: `uc_common` 10, `uc_config` 22) |
| firmware build scenarios | `west twister -T BACNet-uc/firmware -p native_sim/native/64 -p nucleo_f767zi -p frdm_mcxn947/mcxn947/cpu0` |
| WebAssembly SDK and examples | `make -C BACNet-uc/wasm check` (needs wamrc for the AOT part; `make -C BACNet-uc/wasm all test validate` without it) |
| harness unit and integration tests | `cd BACNet-uc/harness && pip install -e '.[dev,serial,sim]' && python -m pytest -q` ✔ (596 passed; the 10 end-to-end tests are skipped without a firmware) |
| harness end-to-end tests against `native_sim` (root: private network and mount namespaces) | `cd BACNet-uc/harness && sudo PYTHON=$(command -v python) BACNET_UC_FIRMWARE=$PWD/../../build-native/zephyr/zephyr.exe tests/e2e/run-isolated.sh` ✔ (10 passed) |

The same jobs run in CI (`.github/workflows/ci.yml`, see
[harness-mcp.md](harness-mcp.md#8-ci-usage)).

## Verification notes

Run for the current repository state (build directories under `/tmp`
instead of `build-*`, otherwise as written):

- `west build -p` for all three boards, 0 compiler warnings:
  `native_sim/native/64` builds and runs; `nucleo_f767zi` (FLASH 497 944 B,
  RAM 359 868 B of 384 KiB, DTCM 97.07 %) and `frdm_mcxn947/mcxn947/cpu0`
  (FLASH 507 236 B, RAM 358 144 B, SRAMX 100 %) link. Also `-S uc-ramfs` on
  both boards and `--sysbuild` (MCUboot) on both boards; numbers in
  [architecture.md](architecture.md#62-measured-usage).
- The `native_sim` walkthrough of sections 4 and 6..10 in a private network
  namespace: smpmgr 0.19.1 (echo, shell, staged uploads with `--mtu 1024`, a
  multi-frame upload without `--mtu` failing with rc 9, `os reset`, file
  download), the harness CLI (`node add/info/discover/objects/logs`,
  `config set`, `io force/read`, `prop read/write`, `app build/deploy/list`),
  `make -C BACNet-uc/wasm` and `make -C BACNet-uc/wasm test`, and the MCP
  server over stdio (36 tools listed, `node_info` called, `node_shell`
  refused).
- The quick start of the top-level [README](../README.md), including the
  two-node `sim-demo` in network namespaces (3 of 3 tests passed).
- Firmware unit tests: 32 of 32 passed (`uc_common` 10, `uc_config` 22).

Not run: flashing and the console on real boards (no hardware attached to
the build machine), `mcumgr` (Go), the Claude Code registration commands,
the HTTP transport of the MCP server, `west update` from scratch (the
workspace existed).
