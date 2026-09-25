# Hardware-in-the-loop session prompt

This file is the brief for the next working session: a Claude Code session on
a machine with the boards attached. To start it, open the repository on the
branch `claude/zephyr-bacnet-stm32-162k1g` and run:

```sh
claude "Read docs/HIL_SESSION_PROMPT.md and follow it."
```

Everything below is addressed to that session.

---

## 1. Your task

Bring the BACnet-uc firmware up on real hardware (NUCLEO-F767ZI and
FRDM-MCXN947), verify every hardware assumption the earlier sessions could
only check by building or in simulation, fix what fails, and finish with a
distributed application running across real boards. Record every result in
`docs/HIL_REPORT.md`, a new file you create and fill as you go.

Before you change anything, read:
- `README.md`
- `docs/architecture.md`
- `docs/hardware.md`, especially the IO channel tables and storage wiring
- `docs/getting-started.md`, sections 2, 3, 5 and 6
- `docs/management-protocol.md`
- `docs/SESSION_NOTES.md`, for environment pitfalls and the MQTT flash reservation

## 2. State at hand-off

The branch is at or after commit `4ee098e`. What exists:

| Part | Where |
|------|-------|
| Zephyr application: BACnet node, IO, LittleFS, logging, SMP management groups 64/65/66, WAMR app runtime | `firmware/` |
| WAMR Zephyr glue: module_ext_root, linear-memory fix, AOT/MPU policy | `modules/wasm-micro-runtime/`, `zephyr/module.yml` |
| WASM SDK (`uc-cc`, `uc-aot`, `uc-wasm-info`) and examples (blinky, thermostat, alarm, uc-link) | `wasm/` |
| Python harness: SMP and BACnet/IP clients, manifests, planner, simulator, MCP server (36 tools), CLI `bacnet-uc` | `harness/` |
| Schemas for `device.json`, `io.json`, `apps.json` and system manifests | `schemas/` |

**Verified** (all in a cloud container, without hardware):
- All three targets (`native_sim/native/64`, `nucleo_f767zi`, `frdm_mcxn947/mcxn947/cpu0`) build with `-DCONFIG_COMPILER_WARNINGS_AS_ERRORS=y`.
- nucleo_f767zi: FLASH 500 528 B, RAM 360 380 B / 384 KiB (91.7 %), DTCM 127 232 B / 128 KiB (97.1 %).
- frdm_mcxn947: FLASH 509 808 B, RAM 358 680 B / 384 KiB (91.2 %), SRAMX 96 / 96 KiB.
- MCUboot sysbuild images fit slot0: F767 64 %, MCXN947 52 %.
- Firmware unit tests (36) pass on native_sim.
- Harness: 644 unit tests and 22 end-to-end tests pass against the native_sim firmware, including a two-node network-namespace system.
- `make -C wasm check` passes: host-stub tests, a WAMR runner loading every example, and x86-64 AOT.

**Never run on real hardware:** everything. The watchdog and the AOT MPU handling were verified only in QEMU (Cortex-M4/M55). The CI workflow `.github/workflows/ci.yml` has never run on GitHub.

## 3. Before touching hardware: ask the user

Ask the user these questions and record the answers in `docs/HIL_REPORT.md`:

1. **Boards:** which boards are attached (how many of each), and how to tell them apart. Use probe serial numbers: `STM32_Programmer_CLI -l`, `LinkServer probes`, `pyocd list`, or `ls -l /dev/serial/by-id/`.
2. **F767 storage:** is a 3.3 V SPI NOR module (W25Q128JV or similar) wired to the NUCLEO-F767ZI?
   - Wiring: CLK PA5 CN7-10 (D13), DO PA6 CN7-12 (D12), DI **PB5** CN7-13 (D22, not D11: PA7 is the Ethernet RMII CRS_DV), /CS PD14 CN7-16 (D10), 3V3 and GND.
   - If not, build with `-S uc-ramfs` (volatile 64 KiB RAM disk) and note it.
3. **Network:** are all boards and the host on one Ethernet segment with DHCP, or should static addresses be set in `device.json`? Is anything on the host blocking UDP 47808 (BACnet/IP) or UDP 1337 (SMP)?
4. **Flash tools:** which are installed? F767: STM32CubeProgrammer, OpenOCD (the Zephyr SDK host tools include one) or J-Link. MCXN947: LinkServer, pyOCD with the MCXN947 pack, or J-Link.
5. **Test equipment and IO safety:** is there a way to set analog inputs (potentiometer or supply), observe PWM outputs (scope, multimeter, or an LED with a resistor), and press the buttons? Is anything attached to the IO pins that must not be driven? Confirm before writing outputs.
6. **Other firmware:** does the FRDM-MCXN947 also carry the MQTT firmware from branch `claude/inter-session-communication-h989ye`? That firmware keeps its settings in the top 64 KiB of the external W25Q64 (0x7F0000..0x7FFFFF). BACnet-uc's LittleFS partition stops below it. Never erase the whole W25Q64.

Ask again before any action with a physical effect: flashing a specific board, mass erase, driving outputs, power cycling.

## 4. Environment

Set the workspace up exactly as in `docs/SESSION_NOTES.md`:
- Python >= 3.12 venv
- `west init -l BACNet-uc && west update --narrow -o=--depth=1` from the workspace top
- Zephyr SDK 1.0.1 minimal bundle plus the arm-zephyr-eabi toolchain
- `pip install -r zephyr/scripts/requirements-base.txt`

Then install the harness with `pip install -e 'BACNet-uc/harness[dev,serial,sim]'`. You also need clang/lld with the wasm32 target for `uc-cc`. For AOT, get `wamrc` 2.4.5 from the WAMR-2.4.5 GitHub release.

Preflight, before any board is flashed:

```sh
west build -p -b native_sim/native/64 BACNet-uc/firmware -d build-native
west build -b native_sim/native/64 BACNet-uc/firmware/tests/unit -d build-unit -t run
cd BACNet-uc/harness && python -m pytest -q && cd -
make -C BACNet-uc/wasm check
```

Any failure here is an environment problem. Fix it before touching hardware and record it in `docs/SESSION_NOTES.md`.

## 5. Phases

Work through the phases in order. Each phase has pass criteria. Record the results, measured numbers and relevant log excerpts in `docs/HIL_REPORT.md`. Commit at the end of each phase (fixes, tests and docs in the same commit).

### Phase 1: NUCLEO-F767ZI bring-up

```sh
west build -p -b nucleo_f767zi BACNet-uc/firmware -d build-f767 -- -DCONFIG_COMPILER_WARNINGS_AS_ERRORS=y
# or, without the SPI NOR module: add -S uc-ramfs
west flash -d build-f767            # STM32CubeProgrammer; or -r openocd / -r jlink
picocom -b 115200 /dev/ttyACM0      # ST-LINK VCP (USART3)
```

Pass criteria:
- **Boot log:** clean, ending in `ready: device …, IPv4 …`, with no fault and no assert.
- **Storage:** `/lfs` mounts. On the first boot it is formatted, and the log says so. The SPI NOR JEDEC id is read. `uc info` shows the size of `/lfs`.
- **Network:** a DHCP address is obtained, and the MAC address stays the same across resets.
- **SMP over UDP:** `bacnet-uc node add f767 --udp <ip> --board nucleo_f767zi`, then `bacnet-uc node info f767` answers.
- **SMP over the serial console:** `bacnet-uc node add f767s --serial /dev/ttyACM0` answers. It uses the interrupt-driven shell backend.
- **BACnet:**
  - `bacnet-uc node discover` finds the device through a real broadcast Who-Is/I-Am. This is the first test of broadcasts; native_sim could not do them.
  - `bacnet-uc prop read f767 device:<instance> object-name` answers.
- **Persistence:**
  - Upload `device.json` staged and reload it (`bacnet-uc config set …`).
  - Reset the board, and check that the configuration and the `/lfs/log/log.*` files survive.
  - The newest log line must be in the file right after it is logged (fix `uc_log_sync.c`).
- **Stacks and memory:** with the shell commands `kernel thread list` and `kernel thread stacks`, record the peak stack use of every thread. Flag any thread above 80 %.

### Phase 2: F767 IO

Use `bacnet-uc io catalog f767`, `io read`, `io write` and `io force`. Then push an `io.json` that binds every channel to an object, and check the objects over BACnet.

Verify each board assumption below against the board and fix the dtsi if it is wrong (`firmware/boards/io/nucleo_f767zi.dtsi`, see `docs/hardware.md` and `docs/io.md`):

| Channel | Assumption to verify |
|---------|----------------------|
| di0 (B1, PC13) | Configured `GPIO_ACTIVE_HIGH`, overriding the board DTS's ACTIVE_LOW. Pressing must read 1. |
| di1, di2 (D2, D4) | Pull-ups: a contact to GND reads 1. |
| do0..do2 | Drive LD1 green, LD2 blue and LD3 red. |
| do3, do4 | Drive D7 and D8. |
| ai0..ai5 (A0..A5) | PA3, PC0, PC3 (ADC1) and PF3, PF5, PF10 (ADC3), 0..3300 mV. Apply known voltages and check the mV accuracy. The ADC prescaler was changed to /4. |
| ao0..ao2 (D6, D5, D3) | TIM1 CH1..3 PWM at 1 kHz. Check the frequency and the duty cycle at 0, 25, 50 and 100 %. The TIM1 prescaler was changed to 9. |

Also check the processing logic:
- debounce, invert, scale and offset
- Out_Of_Service
- the force/release semantics of `docs/io.md` §5
- an AO commanded at priority 8, then relinquished

### Phase 3: WebAssembly apps on the F767

1. **Examples:** build them with `make -C BACNet-uc/wasm` and deploy them with `bacnet-uc app deploy` (see getting-started §8): blinky on do0, alarm on ai0, thermostat, and uc-link with a local link.
   - Pass: all reach `running` with `errors: 0`, and their outputs and objects behave as on native_sim.
   - Record the WAMR pool usage per app on 32-bit (`bacnet-uc node info`, `wasm.pool_free`) and compare it with the numbers in `docs/wasm-runtime.md`.
2. **Watchdog:** deploy a module whose `uc_app_tick` loops forever. The app must reach `failed` about `CONFIG_UC_APP_WATCHDOG_MS` (2 s) after the loop starts, and the node must keep answering. This is the first test of `wasm_runtime_terminate` on hardware.
3. **Stop cancellation:** a tick that loops on `uc_remote_read` to an unbound device must stop within about 1 s. The e2e test `harness/tests/e2e/test_app_limits.py` has the modules.
4. **Refusals and bounds:**
   - A module with a start function, or with `__wasm_call_ctors` exported, is refused with rc `VERIFY`.
   - The bounds probe from `wasm/sdk/wamr-runner` traps at the WAMR bound, not earlier or later.
5. **AOT (opt-in):**
   - Rebuild with `-DCONFIG_WAMR_AOT=y -DCONFIG_WAMR_AOT_MPU_EXEC=y`.
   - Compile the examples with `wasm/sdk/uc-aot --board nucleo_f767zi` (thumbv7em, cortex-m7, eabihf, indirect mode).
   - Deploy the `.aot` files. They must run and match the interpreter's results.
   - Note that `MPU_EXEC` on the F767 makes all of SRAM executable; see `docs/security.md`. Also run an AOT busy loop: it can only be terminated if it was compiled with `--enable-multi-thread`. Document what happens.

### Phase 4: FRDM-MCXN947

Repeat phases 1 to 3 on `frdm_mcxn947/mcxn947/cpu0`:

```sh
west build -p -b frdm_mcxn947/mcxn947/cpu0 BACNet-uc/firmware -d build-mcxn
west flash -d build-mcxn            # LinkServer; or -r pyocd / -r jlink
```

Board-specific checks:
- **External flash:** `/lfs` is on the W25Q64 at 0x0..0x7EFFFF (2032 blocks of 4 KiB). If the MQTT firmware is present, confirm its settings at 0x7F0000 survive a BACnet-uc format.
- **RAM:** cpu0 now uses 384 KiB (SRAM A–G; cpu1 is unused). Confirm there are no hard faults under load, and that the WAMR pool works in SRAMX.
- **Ethernet:** ENET QoS works, and the MAC comes from the unique ID.
- **IO** (`firmware/boards/io/frdm_mcxn947_mcxn947_cpu0.dtsi`; the J3 pin numbers came from NXP SDK readmes and need checking):

| Channel | Assumption to verify |
|---------|----------------------|
| di0, di1 | SW2 (P0_23), SW3 (P0_6) |
| di2, di3 (D2, D4) | Contact to GND reads 1 |
| do0..do2 | RGB LED, active low. `1` must mean on. |
| do3, do4 | D7, D8 |
| ai0..ai2 (A2..A4) | LPADC0 with the 1.8 V VREFO: 0..1800 mV. Keep inputs at or below 3.3 V. |
| ao0, ao1 | FlexPWM1 SM0 A/B on P2_6/P2_7 (J3-15, J3-13) |
| ao2 | SCTimer0 OUT5 on D3; the driver gives 1 % duty steps |

- **AOT:** the target is thumbv8m.main / cortex-m33 (`uc-aot --board frdm_mcxn947/mcxn947/cpu0`). ARMv8-M MPU handling was only verified in QEMU.

### Phase 5: Distributed application on real boards

1. **Manifest:** adapt `harness/examples/systems/hvac-demo.yaml` to the lab and save the result as `harness/examples/systems/hil-lab.yaml`. Set the real IPs, the channels actually wired, and one node per real board.
   - If there are only two boards, place the supervisor on a `native_sim/native/64` node running on the host. It uses host sockets, so it is reachable on the LAN at the host's IP.
   - Every native_sim node needs a static binding, because NSOS cannot send broadcasts.
2. **Run:** `bacnet-uc system validate`, `plan`, `apply --no-dry-run`, then `test`. All manifest tests must pass.
3. **Measure and record:**
   - COV latency: from an input change on node A to the output on node B.
   - Poll-link latency.
   - Behaviour when a node is unplugged and plugged back in: fallback to polling, recovery, relinquish defaults.
   - Who-Is binding against static bindings.
4. **Interoperability:** test against a third-party BACnet client. Options are YABE on a PC, or bacnet-stack's host tools (`bacwi`, `bacrp`, `bacwp`, `bacscov`) built from `modules/lib/bacnet/stack` for Linux. Check:
   - Who-Is, ReadProperty, ReadPropertyMultiple, WriteProperty and SubscribeCOV
   - the password-protected DeviceCommunicationControl and ReinitializeDevice, with `bacnet.password` set
   - that CreateObject and DeleteObject are rejected

### Phase 6: MCUboot and OTA

1. Build both boards with `west build --sysbuild` and the MCUboot configuration (`firmware/sysbuild.conf`, `overlay-mcuboot.conf`; see `docs/storage-and-logging.md`), and flash MCUboot plus the app.
2. Run `bacnet-uc firmware update <node> <build_dir> --yes` with a second build that has a different `CONFIG_UC_FW_VERSION`.
3. Pass criteria:
   - The image is tested, the node reboots into it, confirms it, and reports the new version.
   - An unconfirmed image reverts after a reset.
   - On the F767, check the swap-scratch timing and that the `/lfs` data (external flash) is untouched.

### Phase 7: Robustness and soak

- Pull power while `apps.json` or a staged `*.json.new` is being written. After power returns, the node boots with a valid configuration: either the old one or the new one.
- Soak for at least 2 hours with the phase 5 system running. Record:
  - free heap and malloc arena
  - peak thread stacks
  - WAMR pool
  - BACnet packet counters
  - log rotation (`/lfs/log`, 4 files × 16 KiB)
  - the absence of resets
- Also run: an Ethernet cable pull and replug, a DHCP lease renewal, and a flood of SMP requests. SMP must stay responsive and BACnet must be unaffected.

## 6. Rules

- **Fixes:**
  - Fix the root causes you find, with the smallest correct change, plus a regression test where a framework exists: firmware unit tests, harness pytest, or `wasm` tests.
  - For behaviour that only hardware shows, add opt-in tests under `harness/tests/hil/` with a `hil` pytest marker. They are skipped unless an environment variable (for example `BACNET_UC_HIL_NODES`) names the nodes.
- **Keep the repository consistent:** update `docs/hardware.md` (pin tables, wiring), `docs/io.md`, `docs/architecture.md` (memory numbers, timing) and `docs/getting-started.md` (flashing) whenever the hardware proves them wrong. Record environment and hardware pitfalls in `docs/SESSION_NOTES.md`; the sibling MQTT session reads it.
- **Security defaults:** do not weaken them to make a test pass, for example the password requirement, the CreateObject/DeleteObject policy, or AOT being off by default. Document the need instead.
- **Flash layout:** do not change the external-flash layout of the MCXN947 without keeping 0x7F0000..0x7FFFFF free.
- **Git:** commit per phase with clear messages and push to the branch the user names (default: `claude/zephyr-bacnet-stm32-162k1g`). Do not open a pull request unless asked.
- **When blocked:** if a phase is blocked (missing equipment, a hardware fault), record it in the report with what would be needed and continue with the next phase.

## 7. `docs/HIL_REPORT.md` layout

```markdown
# HIL report

## Setup
boards (revision, probe serial numbers), wiring, network, host OS, tool versions
(west, SDK, flash tools, clang, wamrc), firmware commit

## Results
| Phase | Check | Result (pass/fail/blocked) | Measured | Notes / fix commit |

## Measurements
memory (static and runtime peaks), WAMR pool per app (32-bit), latencies (COV, poll,
remote read), OTA timings, soak counters

## Defects found
id, symptom, root cause, fix commit, test

## Open items
```

## 8. Reference

| Topic | File |
|-------|------|
| Board pin tables, storage wiring, BOM | `docs/hardware.md` |
| IO model, force semantics | `docs/io.md` |
| SMP commands and keys | `docs/management-protocol.md` |
| Configuration documents | `docs/configuration.md`, `schemas/` |
| App ABI | `wasm/sdk/include/bacnet_uc.h`, `docs/wasm-runtime.md` |
| Flash layouts, logging, MCUboot | `docs/storage-and-logging.md` |
| Threat model and security defaults | `docs/security.md` |
| Harness CLI, MCP tools | `harness/README.md`, `docs/harness-mcp.md` |
| System manifests | `docs/distributed-apps.md`, `harness/examples/systems/` |
| Board-specific Kconfig and devicetree | `firmware/boards/`, `firmware/boards/io/` |
