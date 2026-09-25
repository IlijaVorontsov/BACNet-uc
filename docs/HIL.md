# BACNet-uc hardware-in-the-loop (HIL) test rig: design v2

Design v2.2 (reviewed), 2026-09-25. Owner: HIL session, branch `claude/hardware-in-loop-testing-x74tww`.

Companion documents in `docs/hil/`: [pin tables](hil/pin-tables.md), [stimulus protocol](hil/stimulus-protocol.md), [test catalogue](hil/test-catalogue.md), [firmware contract](hil/firmware-contract.md), [bill of materials](hil/bom.md).

History:
- v1 (2026-09-24) targeted NUCLEO-H563ZI on Zephyr 3.7.2. The same evening both firmware branches moved to Zephyr 4.4.2 and the user's boards (STM32F767ZI, MCXN947), so v2 re-baselines the whole rig. Nothing below depends on v1's platform.
- v2.1 (2026-09-25 ~03:00 UTC) applies the three adversarial reviews of v2.0 (pins and electrical, tools and standards, completeness). Appendix D lists every finding and where it was fixed.
- **v2.2 (2026-09-25)** rebases the rig on the boards the user owns: FRDM-MCXN947, FRDM-MCXN236, FRDM-MCXA156, FRDM-MCXA153, an nRF54LM20 DK and several nRF54L15 DKs. No NUCLEO-F767ZI is owned. Changes:
  - The FRDM-MCXN947 becomes the canonical DUT, **P1**. The NUCLEO-F767ZI becomes the optional profile **P1-F767**. P2 is retired.
  - An FRDM-MCXN236 replaces the second F767 as the stimulus.
  - MS/TP uses the product's **planned** Arduino UART with a GPIO driver enable (user decision), so the rig measures software DE timing.
  - New decisions are D35-D51. Appendix E resolves the contradictions between the three v2.2 research reports (DUT, stimulus, stimulus port). Appendix F lists the findings of the two v2.2 reviews (pins and electrical; tools and completeness) and where each one was fixed.

**Provenance.** Every fact here was read this session in a primary source, unless it is tagged:
- **[likely]**: inferred from sources, not read directly;
- **[unverified]**: still to be measured. The W-step that measures it is named.

Primary sources used:
- Zephyr v4.4.2 (dccb0959) and its bundled revisions: hal_stm32 fc11896d, hal_nxp 7554bc0f, hal_nordic 44fd3d4, SDK 1.0.1.
- Board manuals:
  - UM1974 Rev 8 and the MB1137 B-01 schematic, DS11532 Rev 8;
  - UM12018 Rev 2.0 (FRDM-MCXN947), UM12041 Rev 3.0 (FRDM-MCXN236), UM12121 Rev 1 (FRDM-MCXA156), UM12012 Rev 2.0 (FRDM-MCXA153);
  - MCXNP184M150F70 Rev 8.2;
  - nRF54L15 DK UG v1.0.0, nRF54LM20 DK UG v0.7.0, nRF54L15 datasheet v0.7.
- TI (TPS22918 SLVSD76C, THVD1450), Nexperia, Microchip and Pololu datasheets.
- Mbed TLS 4.1.0, mosquitto 2.1.2, libsigrok 0.5.2, DSView `dsl.h`, and the GitHub Actions docs.
- The branches themselves, at their live heads (`git ls-remote`, 2026-09-25 07:19 UTC).

Builds made for v2.2 with SDK 1.0.1 against v4.4.2. All have 0 warnings, and those marked (W) were built with `CONFIG_COMPILER_WARNINGS_AS_ERRORS=y`:

| Image | Ref | Board | FLASH | RAM |
|---|---|---|---|---|
| BACnet release (plain) (W), **release baseline** (D24) | 4ee098e (BACnet tip) | frdm_mcxn947/mcxn947/cpu0 | 509,808 B | 358,680 B of 384 KiB (91.2 %); SRAMX 96/96 KiB (WAMR pool) |
| BACnet release (plain) (W), reference only | e62a095 = `main` | frdm_mcxn947/mcxn947/cpu0 | 507,236 B | 358,144 B (91 %). Its `storage_partition` is 0x800000 (whole W25Q64), so it fails D44. |
| BACnet + `-S hil` (W) | 4ee098e (identical at 6d1a151) | frdm_mcxn947/mcxn947/cpu0 | 511,676 B | 358,752 B (91.2 %) |
| mqtt_tls plain | 75e0620 | frdm_mcxn947/mcxn947/cpu0 | 282,936 B | 169,776 B of 320 KiB |
| mqtt_tls MCUboot (sysbuild, `FILE_SUFFIX=mcuboot`) | 75e0620 | frdm_mcxn947/mcxn947/cpu0 | app 291,924 B; MCUboot 48,052 B | 170,448 B |
| mqtt_tls + `-S hil` (W) | e859980 (0.4.0 tip) | frdm_mcxn947/mcxn947/cpu0 | 315,144 B | 175,144 B |
| mqtt_tls + `-S hil`, planned-UART F767 overlay (W) | e859980 | nucleo_f767zi | 304,964 B | 170,004 B |
| `apps/hil_stimulus`, v2.2 port + review patch (W) | HIL 6cdac99 + port + patch | frdm_mcxn236 | 100,244 B | 42,976 B |
| `apps/hil_stimulus`, same sources (W) | HIL 6cdac99 + port + patch | nucleo_f767zi | 92,324 B | 42,688 B |

Between 6d1a151 and 4ee098e the firmware changed only in one comment line (`firmware/include/uc/uc_apps.h`), so both give the same `-S hil` image size.

Nothing has run on hardware yet.

IDs (D-, FW-, R-, SYS-, …) are the same in this document, the pin tables, the BOM, the test catalogue, the firmware contract and the stimulus protocol.

---

## 0. Context

### 0.1 What each session is doing (origin, 2026-09-25 ~01:50 UTC)

Refreshed for v2.2 from `git ls-remote origin` at 2026-09-25 07:19 UTC. This table replaces the v2.1 rows. `survey.sh` reruns it just before the v2.2 commit.

| Branch @head | Session | State | Consequence for the rig |
|---|---|---|---|
| `main` @e62a095 (04:08 UTC) | integration (created by the user) | Same commit that the BACnet branch had at 04:08.<br>- `.github/workflows/ci.yml` runs on push to main, `pull_request` and `workflow_dispatch`, on hosted runners.<br>- **Not the default branch.**<br>- **No shared history** with the HIL or MQTT branches: `git merge-base` is empty; root commits are 847e1d1 for main and edf7749 for HIL/MQTT.<br>- Maps the whole W25Q64 as `storage_partition`. | Integration target (D45). It becomes the BACnet release baseline once the storage shrink (6d1a151) is merged there. Until then the baseline is the BACnet tip (D24, D44). |
| `claude/zephyr-bacnet-stm32-162k1g` @4ee098e (06:13 UTC; e62a095 + 5 commits) | BACnet | - e62a095: the default builds link on both boards. F767: WAMR pool in DTCM. MCXN947: cpu0 has 384 KiB, WAMR pool in SRAMX. **FW-04 met.**<br>- MCXN947 `/lfs` is on the on-board W25Q64 (FlexSPI). Since 6d1a151, `storage_partition` is 0x000000-0x7EFFFF, leaving the top 64 KiB to mqtt_tls (FW-23). `docs/hardware.md` §4 says so since 4ee098e.<br>- DCC and Reinit are refused without a configured password; Create/DeleteObject are off by default. **FW-19 met.**<br>- No watchdog and no fatal-handler override (FW-05 open).<br>- MS/TP is planned (hardware.md §5, roadmap 3.1): Arduino UART plus GPIO DE. No RS-485 port code yet.<br>- 6d1a151 → 4ee098e changes the firmware only in one comment; the v2.2 builds were redone at 4ee098e. | P1 payload and, until main has the shrink, the release baseline. FW-07 is now the planned UART. |
| `claude/inter-session-communication-h989ye` @e859980 | MQTT | mqtt_tls 0.4.0:<br>- M4-M6 done: MCUboot sysbuild variant, SMP over UDP 1337, runtime settings with last-known-good fallback and factory reset, logs over MQTT.<br>- MCXN947 `settings_partition` at 0x7F0000 (64 KiB on the W25Q64).<br>- HIL answers: FW-01/02/03/05/06/08 accepted; FW-10 = plain SMP on the management VLAN; FW-11 = MCUboot dev key; FW-12 deferred with M7.<br>- This is the repo **default branch**. | Second payload. `rig_config` factory-resets the MQTT settings (D44). SEC-02 now covers MQTT SMP too. |
| `claude/bacnet-mqtt-broker-devices-57orta` @b437af3 | device management | Requested MQTT M4-M7 and reconciled them with the HIL contract (FW-06, FW-08, FW-11, FW-12 take precedence). No firmware. | none beyond the MQTT row |
| `claude/ai-harness-building-control-05nrgm` @5fd2872 (06:11 UTC) | Hub | Requests B1-B8 (not re-read for v2.2) | FW-21 |
| `claude/tests-docs-relevance-future-lpfqzn` @ace8910 | Experiment | spec-vs-code study | none |
| `claude/hardware-in-loop-testing-x74tww` @6cdac99 | HIL | It1 snapshot:<br>- `apps/hil_stimulus` (F767 only);<br>- `lib/hil`;<br>- `snippets/hil` and `hil-io` (F767 only, USART2 hardware DE);<br>- `hilrig`.<br>The working tree carries uncommitted host changes from another workflow. | W0 commits:<br>- the v2.2 stimulus port with the review patch (Appendix F);<br>- the MCXN947 snippet overlay;<br>- the F767 planned-UART overlay (Appendix A);<br>- `hil/tools/mcxn_flash_guard.py`. |

`survey.sh` (W0) also compares each branch against `main` and prints `git merge-base` for each pair.

### 0.2 Decision log (v1 D-numbers kept where they survive)

**Rows changed in v2.2** (replace the row with the same number):

| # | Decision | v2 status | Why |
|---|---|---|---|
| D3 | Flashing per profile:<br>- P1: D42 (`linkserver --probe <full serial>` through the rig wrapper, pyocd fallback);<br>- stimulus and MS/TP peers: `linkserver --probe <full serial>` through the same wrapper;<br>- P1-F767: `openocd` from the SDK hosttools with `--cmd-pre-init "adapter serial <SN>"` (v2.1 text);<br>- P3: unchanged. | **changed (v2.2)** | D35, D36, D42 |
| D4 | Stimulus: FRDM-MCXN236 (D36). No second NUCLEO-F767ZI. | **superseded by D36** | user board list |
| D5 | DUT I/O is the catalog. HIL extras come from two snippets:<br>- `hil`: on P1, markers D12/D13/SDA/SCL plus the MS/TP UART alias and DE GPIO; on P1-F767, markers PG0-PG3 plus USART6/PD15;<br>- `hil-io`: P1-F767 only. | **changed (v2.2)** | P1 has no free outer-row GPIO left (D38). |
| D6 | RS-485:<br>- DE and /RE are tied. A 10 k pull-down sits on every DE net. A 10 k pull-up to that transceiver's VCC sits on every RO that feeds an MCU.<br>- **U1 VCC: on P1, P3V3_MCU at J24 pin B, which is the TPS22918 VOUT when the switch is fitted and the shunt output otherwise; on P1-F767, DUT 3V3 (CN8-7).**<br>- On P1, RO reaches D0 through the 2.2 kΩ shield resistor.<br>- U2 and U3 run from the stimulus 3V3; the peers from their own 3V3. | extended | U1 goes down with the MCU, so no RO back-feeds an unpowered D0. An unpowered THVD1450 is Hi-Z on the bus. |
| D7 | **The DUT DE is software** (GPIO, FW-07): the rig measures lead and lag instead of setting them. The injector uses LPUART RTS hardware DE, whose fixed lead and lag are measured once (R-08). | **changed (v2.2)** | D37 |
| D8 | Bias 510 Ω / 510 Ω from the always-on +5VA, at one point. +5VA is a 5 V PSU bought with the It2 MS/TP parts. | kept | |
| D15 | Board-level E5V cut: P1-F767 only. For P1 see D39. | **changed (v2.2)** | |
| D16 | AI node: P1 = 2.2 kΩ + 100 nF at the DUT pin (D40); P1-F767 = 1 kΩ + 100 nF. | **changed (v2.2)** | |
| D17 | MS/TP MACs: DUT 5, host node 7, injector spoofs any MAC, slave tests 130, Max_Master 10. Peers: FRDM-MCXA153 = 3, FRDM-MCXA156 = 4, set by `CONFIG_MSTP_NODE_MAC` in their board confs (§8.3). The peers' Device instances are 260200 + MAC (260203, 260204), and R-10 checks both. The Pico is dropped. | **changed (v2.2)** | A master above Max_Master is never polled for the token. The stimulus report's MACs 10/11 would have left MAC 11 out, and the research peer builds all used the Kconfig default 10. |
| D18 | F767 MS/TP is USART6 PG14/PG9 with GPIO DE on PD15 (D37). | **superseded by D37** | user decision |
| D19 | IO description = the catalog only. `/zephyr,user` holds `hil-marker-gpios`, and `mstp-de-gpios` until the BACnet port defines its own node (FW-07). | **changed (v2.2)** | |
| D22 | Stimulus channels keep DUT catalog names. The MCXN236 stimulus carries `profile = "P1"` for the MCXN947 map. A P1-F767 map for the MCXN236 is written only if an F767 is bought. | kept | |
| D23 | Replaced by the planned UART (D37). | **superseded by D37** | user decision |
| D24 | Release artifacts:<br>- **BACnet plain = the unmodified default build of the BACnet branch tip** (4ee098e today, at least 6d1a151) until main carries the storage shrink (D44); then main. FW-04 is met, `hil/site/f767-app-pool.overlay` is retired, and SEC-01 no longer whitelists it. main e62a095 builds but fails D44.<br>- BACnet MCUboot (It2) = scenario `bacnet_uc.firmware.mcuboot.mcxn947` (swap-using-offset).<br>- MQTT plain and mTLS: unchanged.<br>- MQTT MCUboot = scenario `app.mqtt_tls.mcuboot` (`FILE_SUFFIX=mcuboot`). | **changed (v2.2)** | |
| D25 | Known state:<br>- BACnet `rig_config`: unchanged.<br>- **MQTT sessions start with the SMP write `mqtt/factory_reset`** (0.4.0), so the Kconfig site values apply.<br>- P1 OTA sessions clear slot 1 with SMP `image erase`. **`west flash --erase` is never used on P1** (D42).<br>- P1-F767: unchanged. | **changed (v2.2)** | MQTT stored settings override Kconfig once they are written. The LinkServer erase path skips the board's flash overrides. |
| D27 | `main` exists (D45). Until the user makes it the default branch, the v2.1 trigger model stays: nightly by push to `hil/nightly`. | refined | |
| D29 | P1-F767 recipe unchanged. P1: D43. | **changed (v2.2)** | |
| D34 | Revised by D47. | **revised** | |

**New decisions** (append after D34):

| # | Decision | v2 status | Why |
|---|---|---|---|
| D35 | **Profiles.**<br>- **P1 = `frdm_mcxn947/mcxn947/cpu0`**: owned, canonical, every area.<br>- **P1-F767 = `nucleo_f767zi`**: an optional purchase. The v2.1 F767 material is kept in pin-tables Appendix A.<br>- **P2 is retired**; it was the MCXN947.<br>- P3 = `nucleo_h563zi`: optional, MQTT only, not owned. | **new** | User board list (2026-09-25). The MCXN947 is the only owned board with Ethernet, and both apps build for it. |
| D36 | **Stimulus = FRDM-MCXN236** (`frdm_mcxn236`), running the ported `apps/hil_stimulus` with protocol v0 unchanged. **Fallback: FRDM-MCXA156**, which needs:<br>- the upstream `pwm_mcux.c` A/B/X capture guards (X-channel capture only);<br>- `CONFIG_MCUX_OS_TIMER=n` (96 MHz 64-bit SysTick);<br>- an LPUART2 node plus a clock hook. | **new** | MCXN236:<br>- 3.3 V, with an Arduino header;<br>- same MCX-N family as the DUT: LPADC, LP_FLEXCOMM, runner;<br>- 64-bit SysTick at 150 MHz by default;<br>- FlexPWM capture compiles on A/B/X;<br>- 8 LP_FLEXCOMMs, so an RTS-DE injector is available;<br>- no DAC, so the MCP4728 moves to It1 (D41). |
| D37 | **MS/TP UART = the planned one** (user decision):<br>- P1: LPUART2 TX P4_2 D1, RX P4_3 D0, FC2 in **combined LPI2C+LPUART mode**, DE and /RE on **GPIO P0_24 D11**;<br>- P1-F767: USART6 TX PG14 D1, RX PG9 D0, DE on GPIO PD15 D9.<br>The HIL snippet publishes the alias `mstp-uart0` and `/zephyr,user mstp-de-gpios` until the BACnet port has its own node (FW-07). The rig measures software DE timing (MSTP-04/05) at 9600-115200 baud, which is roadmap 3.1's open question. | **new**; supersedes D18, D23 and the v2.1 §4.2 option "LPUART-only FC2 with hardware DE" | `mfd_nxp_lp_flexcomm.c` selects the FC2 mode from the devicetree status of its children (`DT_INST_FOREACH_CHILD_STATUS_OKAY`), not from Kconfig. The combined mode has no RTS pin. Disabling lpi2c2 would switch FC2 to LPUART-only mode and move the UART pins. |
| D38 | **P1 HIL extras.**<br>- Markers m0-m3 = D12, D13, SDA, SCL (P0_26, P0_25, P4_0, P4_1).<br>- The snippet keeps `flexcomm2_lpi2c2` okay and disables `dac0` (P4_2) and `flexcomm1_lpspi1` (P0_24-P0_27).<br>- `check_catalog` fails an instrumented P1 image if any of these holds:<br>  - CONFIG_I2C, CONFIG_SPI, CONFIG_DAC, CONFIG_I2S, CONFIG_LED or CONFIG_INPUT is set, or an SDHC/SDMMC disk is enabled;<br>  - lpi2c2 or lpuart2 is not okay;<br>  - dac0 or lpspi1 is okay.<br>- No `hil-io` on P1. | **new** | With MS/TP on D0/D1/D11, these are the only free outer-row GPIOs. The markers need unmuxed FC2 pads, so no I2C driver may be built. The board's gpio-leds and gpio-keys nodes share pins with do0, do1 and di0, and no LED or INPUT driver may claim them. The markers span two ports, so there is no single-write marker group. |
| D39 | **P1 power.**<br>- `mcu_rail`: a TPS22918 replaces the J24 shunt. VIN = P3V3, VOUT = P3V3_MCU, QOD tied to VOUT, CT 1 nF, ON pulled up to P3V3 through 100 k and pulled low by BSS138P Q1 (or a 2N7002) from stim `pwr`, with a 100 k gate pull-down. Fail-safe = DUT on.<br>- The J24 pins are identified **by continuity** with the board unpowered (pin A 0 Ω to J3-8, pin B 0 Ω to AREF J2-16), and the leads use a keyed or labelled 2-pin housing. A reversed switch would put its QOD pull-down across the always-on P3V3.<br>- `usb_port`: uhubctl on the DUT J17 hub port, for full-board and flash power-loss cuts. This also drops the probe.<br>- PWR-01, PERS-01 and OTA-04 use `mcu_rail` only after the W1 back-feed measurement passes: **aggregate current < 3 mA into a 22 Ω shunt (< 66 mV) and pin B < 0.3 V.** Until then, or if it fails, they use `usb_port`.<br>- R154 is never populated; it would stop Y3 and with it the Ethernet.<br>- J11, J3-10 and J3-16 are never connected, and the shields' 5V and VIN stacking pins are clipped. | **new**; replaces D15 for P1 | J24 is the only feed of P3V3_MCU. The MCU-Link, PHY, Y3, NOR and LEDs stay on P3V3, so the VCOM and the Ethernet link stay up. There is no 5 V anywhere in the switch path. The largest single-pin back-feed current cannot exceed the aggregate, so the 3 mA aggregate limit protects the ±3 mA per-pin limit. |
| D40 | **Series resistors (P1).** One 2.2 kΩ resistor per DUT line: every stimulus line, and every transceiver line into the DUT. It is mounted **on the DUT shield**. The DUT side of that resistor is the probe point for:<br>- the LA;<br>- the nRF probe (through 4.7 kΩ);<br>- each analog node (100 nF at the DUT pin; the stimulus sense input adds 10 kΩ).<br>Stimulus DI lines are 0/z only; the stimulus firmware enforces it (D49). | **new**; replaces "1 kΩ at the stim end" for P1 | Injection into an unpowered MCXN pin is at most (3.3 − 0.3)/2.2 k = 1.36 mA, below the 3 mA limit. Neither MCX board is 5 V tolerant. Appendix E2 explains the placement. |
| D41 | **Analog front end.**<br>- One MCP4728 on the stimulus LPI2C2 (P4_0/P4_1 at mikroBUS J5-6/J5-5, GND J5-8, 400 kHz). **VDD = 3.3 V from J6-7 (VDD_BOARD); never J5-7, which is P5V0 (5 V).** The module's I2C pull-ups go to that 3.3 V or are removed. LDAC to GND. Internal 2.048 V reference at **gain 2**: 0-3.2 V usable (VOUT ≤ VDD), 1 mV per LSB. Channels A-C drive ai0-ai2 through the node resistor; D is spare.<br>- **Truth = the stimulus 16-bit LPADC0**: reference VDD_ANA, nominal 3300 mV, with a per-board DMM correction in bench.yml.<br>- ADS1115: optional It2 reference for IO-07. | **new** | One static devicetree configuration covers both the in-range points (0.05-1.75 V) and the over-range points (2.0/3.0 V, which must read full scale). The Zephyr driver fixes the gain per devicetree. UM12041 Table 25: J5 has no 3.3 V pin. |
| D42 | **P1 flashing.**<br>- `hil/tools/mcxn_flash_guard.py <b>` passes, then **`hil/tools/hil-flash -d <b> --probe <full MCU-Link serial>`**, which runs `west flash -r linkserver --probe <serial>`.<br>- The wrapper refuses a missing `--probe`, `#N` indices, `-i/--dev-id` and `--erase`. It checks that the serial appears exactly once in `LinkServer probes` output [likely: LinkServer CLI not run here; its `--probe` also matches serial substrings]. It is used for the DUT, the stimulus and the peers.<br>- Fallback: `-r pyocd -i <uid>`, with NXP.MCXN947_DFP 26.06.00 pinned.<br>- **Never `--erase`**, blhost/ISP, J21, `//cpu0/qspi` or `/ns`; never drive SW3/P0_6.<br>- udev keys every MCU-Link on `ID_SERIAL_SHORT` and sets `ID_MM_DEVICE_IGNORE=1`.<br>- LinkServer's version is pinned. CI never runs `LinkServer probe … update`. The MCU-Link firmware version is recorded in bench.yml.<br>- Under Twister, the map.yml `pre_script` gets its build directory from `HIL_BUILD_DIR`, which the conftest exports, and fails closed without it (§8.4). | **new**; replaces D3 for P1 | The 4.4.2 `linkserver.py` defaults `probe='#1'` (parser default too) and `do_create` passes only `args.probe`, so `-i` is dropped and a missing `--probe` silently flashes probe #1. `do_erase` builds its command without the board's `--override` flash settings (linkserver.py:175-180). Twister adds `--probe=<id>` for linkserver (handlers.py:580-584; harness hardware_adapter.py:111-112). The DUT, stimulus and peers are all MCU-Links, so they may share VID:PID [unverified]. The static guard rejects anything outside 0x10000000-0x10200000, which covers CMPA/IFR and the W25Q64. |
| D43 | **P1 identity.** MAC = AE:9A:22 followed by the CRC-24/OpenPGP (init 0xB704CE, poly 0x864CFB) of the 16-byte hwinfo UID; both apps set `nxp,unique-mac`. Commissioning:<br>1. Flash an instrumented image once (D42 wrapper) and read `HIL-BOOT uid=`.<br>2. Compute the MAC with `hilrig.bench.mcxn947_mac()`.<br>3. Confirm it from the first DHCPDISCOVER.<br>R-07 re-checks the DHCPDISCOVER MAC after every flash. | **new**; refines D29 | Reading the UID over SWD is unverified tooling (W1). |
| D44 | **Shared W25Q64 (P1).**<br>- BACnet `/lfs`: 0x000000-0x7EFFFF (BACnet 6d1a151; the 4ee098e build's generated devicetree has `reg = <0x0 0x7f0000>`).<br>- mqtt_tls settings: 0x7F0000-0x7FFFFF (MQTT 0.4.0).<br>- This is contract FW-23. `check_catalog` fails a BACnet P1 build whose `storage_partition` reaches 0x7F0000. main e62a095 still does (its build maps 0x800000), so **on main the rule is a warning until main contains the shrink** (D45); on every other ref it is an error.<br>- The rig's flashes never touch the NOR. | **new** | Otherwise, alternating apps on one DUT corrupts both stores. |
| D45 | **Integration with `main`.** main exists at e62a095, and its CI runs on push to main and on PRs. It shares no history with the HIL branch, so there is no branch PR. Instead, W3 does the following:<br>1. Cut `claude/hil-into-main` from main.<br>2. Copy only HIL-owned paths: `docs/HIL.md`, `docs/hil/`, `hil/`, `apps/hil_stimulus/`, `lib/hil/`, `snippets/hil/`, `snippets/hil-io/`, `.github/workflows/hil.yml`.<br>3. Put the HIL session notes in `docs/hil/SESSION_NOTES.md` on that branch.<br>4. Drop the HIL's copies of `west.yml`, `zephyr/` and `modules/`; main's own files are used.<br>5. hil.yml gains `push: branches: [main, "claude/hil-**"]` (build job only; paths include `firmware/boards/**`; D44 as a warning on main until the merge).<br>6. **After the merge, main is the single source.** HIL work continues on branches cut from main (`claude/hil-*`). The old HIL branch is frozen, with a pointer to main in its README. The nightly cuts `hil/nightly` from `origin/main`.<br>After the user makes main the default branch:<br>- the nightly moves to `on: schedule` on main;<br>- `workflow_dispatch` becomes usable;<br>- `hil/nightly` stays as the fallback. | **new**; refines D27 | `git merge-base origin/main origin/claude/hardware-in-loop-testing-x74tww` returns nothing. The MQTT branch has the same problem (its session's call). Two diverging copies of hil.yml would otherwise run on different triggers. |
| D46 | **Owned-board roles.**<br>- FRDM-MCXN947: DUT.<br>- FRDM-MCXN236: stimulus.<br>- FRDM-MCXA156: fallback stimulus, otherwise MS/TP peer (MAC 4).<br>- FRDM-MCXA153: MS/TP peer (MAC 3).<br>- nRF54L15 DK #1: It2 precision edge probe (optional).<br>- nRF54LM20 DK: alternative or bus-side probe (It3).<br>- The other nRF54L15 DKs: spares, one kept preset to 3.3 V.<br>Nothing is bought for these roles. | **new** | Stimulus report. The peers use hardware RTS DE (`nxp,rs485-mode`), so they are never timing references. |
| D47 | **MS/TP gate revised (D34).** Rig-side MS/TP work is no longer gated: bus, injector, peers, and the tests R-08, R-10, R-11, MSTP-01 and MSTP-22a. It runs in It2 on owned boards plus **about €65-90** of transceivers, bus parts, the bias PSU and the FTDI (€40-60 without the FTDI). DUT MS/TP *product* tests (MSTP-03..08, 11, 22b, 23, 24) stay gated on BACnet Phase 3 code.<br>Optional It2 spike (lead confirms with the BACnet session first): a HIL-owned MS/TP test image on the DUT (bacnet-stack dlmstp with a GPIO-DE port on P4_2/P4_3/P0_24) measures software-DE Tturnaround and Tpostdrive at 76800 and 115200 baud. The result goes to the BACnet notes as input to roadmap 3.1/3.2. | **new**; revises D34 | The UART is decided (D37) and the peers are owned. The software-DE result decides whether the BACnet roadmap needs the cpu1 coprocessor (3.2). |
| D48 | **P1 LA map and timing sources.**<br>- It1 FX2: nrst, m0, m1, di2, do3, do0, vcp_tx (J9-30, no bridge), sync.<br>- It2 DSLogic adds de (D11), tx (D1), rx (D0), bus_ro, stim_de, m2, m3 and ao0; the `pers` capture profile puts `p3v3_brd` on CH15 (D51).<br>- D9 is extended: the nRF54L15 probe (TIMER00 128 MHz via GPIOTE/DPPI) becomes a third MS/TP timing source once the new rig test **R-11** shows it agrees with the LA within ±(62.5 ns + 2 LA samples). Its UART1 RTS/CTS group is disconnected from the debugger first (§6). | **new**; refines D9, D10 | §6 |
| D49 | **Stimulus per-board figures (protocol v0 unchanged).** On the MCXN236:<br>- quantum: 150 MHz, 6.67 ns;<br>- `dac`: 0..3200 mV (MCP4728); it refuses non-zero values while `pwr` is off **or the `v3v3` sense reads < 3000 mV**;<br>- `adc`: 16-bit, VDD_ANA reference;<br>- `pwmcap`: FlexPWM ticks of 106.7 ns (±2 ticks), period and pulse from consecutive cycles, at most 6.99 ms per wrap;<br>- **di channels are 0/z only**: no P1 channel has the new `drive-high` property, so level 1 returns ERR -1;<br>- injector DE lead and lag fixed by hardware;<br>- transport: MCU-Link VCOM on FC4.<br>bench.yml `stim.chans` for P1 has no di1, ai3-ai5 or v5, and adds `mstp_rx`, `mstp_tx`, `mstp_de` and `v3v3_brd`. | **new** | Port report and the v2.2 pins review. The per-board note goes into stimulus-protocol.md §1, §2, §4 and §6 (the PA15 exception applies to the F767 stimulus only). |
| D50 | **RESET_B sense isolation (P1).** Q3, a BSS138P on the DUT shield: gate on RESET_B (J3-6) through 1 kΩ, source GND, drain over ribbon line 33 to stim P3_6 with a 10 kΩ pull-up to the stimulus VDD_BOARD. `nrst_sense` is GPIO_ACTIVE_HIGH (pin high = DUT in reset). RESET_B then carries no DC load from the rig, whatever the stimulus's power state. Q3 switches at VGS(th) 0.9-1.5 V, before the MCU's own threshold: W1/W2 measure the offset to the LA edge and to the boot marker, and RST-01 uses that window. With the ribbon unplugged, P3_6 reads "in reset", which R-02a catches. | **new**; refines §7.2 | v2.2 pins review: through a plain 10 kΩ, an unpowered stimulus (input clamped near 0.5 V) divides RESET_B (RPU 33-75 k, MCXNP184M150F70 Table 9) to about 0.8-1.2 V, near VIL max 0.99 V. |
| D51 | **Flash power-loss tests (P1).**<br>- **PERS-02 (NOR data)** needs `usb_port`, because the W25Q64 stays powered on `mcu_rail`. The cut is host-timed: hilrig issues `uhubctl off` at a random delay after the FW-16 marker edge. The real power loss is time-stamped on the LA (`p3v3_brd`, §6), and results are binned by the measured offset. This is coarser than the v2.1 swept delay.<br>- **OTA-04** keeps stimulus-timed random offsets on `mcu_rail` once W1 passes. The MCUboot slots are in the internal flash (`frdm_mcxn947.dtsi`: slot0 0x14000, slot1 0x10a000), which loses power with the MCU. Before W1 passes it uses the PERS-02 method.<br>- A stimulus-timed VBUS switch for PERS-02 is an It3 option [proposed]. | **new** | v2.2 tools review (the stimulus cannot time a uhubctl cut). |

Baseline-changes table, v2.2 rows:
- "MCXN markers": decided (D38).
- "CHANGE B11 MS/TP gate": revised (D47).
- "CHANGE B13 nightly by push": still forced until main is the default branch (D45).

---

## 1. Goals and scope

Replace the Profiles table:

| Profile | Board | Role | Scope | Owned |
|---|---|---|---|---|
| **P1** | `frdm_mcxn947/mcxn947/cpu0` | canonical DUT | every area | yes |
| **P1-F767** | `nucleo_f767zi` | optional DUT (buy only for F767-specific work) | every area; pin tables Appendix A | no |
| P2 | – | **retired in v2.2** (was the FRDM-MCXN947 second DUT, now P1) | – | – |
| **P3** | `nucleo_h563zi` | optional | MQTT only | no |

Rig boards (D46), all owned:
- stimulus FRDM-MCXN236 (`frdm_mcxn236`);
- fallback stimulus FRDM-MCXA156;
- MS/TP peers FRDM-MCXA153 and FRDM-MCXA156 (It2);
- edge probe nRF54L15 DK (It2, optional);
- nRF54LM20 DK (It3).

In the Areas table, "OpenOCD" in the Instruments column becomes "LinkServer/pyocd (P1), OpenOCD (P1-F767)", and "INA226" means the MCU-rail current at the TPS22918 output.

---

## 2. Rig block diagram (P1)

```
+=========================== HIL HOST  x86 N100 (or an existing Linux PC), Ubuntu 24.04, self-hosted runner (user hil) ======+
| pytest + hilrig | Twister 4.4.2 | west + SDK 1.0.1 | LinkServer (pinned) + pyocd 0.45.1 | tshark/dumpcap | sigrok-cli       |
| netns lan-a[br-a] svc(.1,.2) sim1 sim2 rtr lan-b[br-b] fd bbmdb | dnsmasq | mosquitto 2.1.2 (docker, --tls-keylog)         |
|   eth0 = uplink        eth1 = DUT NIC (moved into lan-a, offloads off)        USB -> uhubctl hub | FX2 LA on a root port    |
+======|======================|===================================================================|=======================+
       |                      | Cat6 direct                                                        | hub ports (uhubctl):
   LAN/Internet               |                                                                    |  P1 DUT MCU-Link J17 (usb_port cuts)
                              |                                                                    |  P2 STIM MCU-Link  P3 FTDI (It2)
+-----------------------------+----------------------------------------+                           |  P4 nRF probe  P5/P6 peers (It2)
| DUT  FRDM-MCXN947 (P1)            RJ45 / LAN8741A (on P3V3: stays up) |
|  MCU-Link J17: SWD + VCOM (LPUART4 P1_8/P1_9, 115200) + board power  |      +5VA 5 V PSU (It2) --> RS-485 bias (D8)
|  J24 shunt -> TPS22918 (mcu_rail, QOD) ON <- Q1 BSS138P <- STIM P3_18|
|  catalog di0 di2 di3 do0-4 ai0-2 (0-1.8 V) ao0-2 (Arduino + J3-13/15)|
|  snippet hil: m0-m3 = D12 D13 SDA SCL; MS/TP LPUART2 D1 TX / D0 RX,  |==== D1/D0/D11 -> U1 THVD1450 (VCC = P3V3_MCU, J24 pin B)
|               DE = GPIO P0_24 D11 (software DE), FC2 combined mode    |     == RS-485 bench bus (It2, §4)
|  W25Q64 QSPI NOR on P3V3: /lfs 0-0x7EFFFF, MQTT settings 0x7F0000+   |
|  RESET_B J3-6    VDD_ANA (switched) = AREF J2-16    P3V3 = J3-8       |
+-------------|------------------------------------|-------------------+
              | DUT shield: 2.2 kOhm per line (DUT side = probe point), AI node 100 nF at the DUT pin,
              | NRST Q2 + 100 R, RESET_B sense Q3 (gate only, D50), PWR Q1 -> TPS22918 ON, senses 100k/100k + 100 nF;
              | 5V/VIN stacking pins clipped; 40-way IDC ribbon
+-------------+------------------------------------|-------------------+     +---------------------------------------+
| STIM  FRDM-MCXN236  apps/hil_stimulus (4.4.2, shell 'stim', P1)      |     | Logic analyzer, DUT side of the R      |
|  di0 di2 di3 out (0/z only)  do0-4, m0-3, mstp_rx/tx/de in (per-pin  |---->|  It1 FX2 8 ch 24 MS/s (sigrok)         |
|     ICR, 64-bit SysTick 150 MHz = 6.67 ns)                            | SYNC|  It2 DSLogic Plus 16 ch (PID 0x0020)   |
|  MCP4728 (LPI2C2 J5, VDD 3V3 from J6-7) -> ai0-2; LPADC0 senses (10k) | P1_7|  It3 Saleae Logic Pro 16               |
|  FlexPWM1 capture <- ao0 (J3-7), ao1 (J3-13), ao2 (D3)               |     +---------------------------------------+
|  P2_8 -> Q2 NRST   P3_6 <- Q3 drain (10k pull-up)   P3_18 -> Q1 PWR  |     nRF54L15 DK edge probe (It2, VDD 3.3 V,
|  R153-R156 removed (USB-SPI bridge off D10-D13)                       |       4.7 k inputs, UART1 RTS/CTS group
|  It2: FC3 LPUART RTS-DE injector on J9 -> U2; U3 listen-only sniffer |       disconnected): de, tx, m0-m3, sync, bus_ro
+----------------------------------------------------------------------+
 It2 MS/TP peers on the bench bus: FRDM-MCXA153 (MAC 3), FRDM-MCXA156 (MAC 4), each with its own THVD1450 (RTS hardware DE)
```

Ownership rules:
- Only the stimulus drives DUT pins.
- Each board's MCU-Link is the only USB path to that board.
- The LA and the probe only listen, always on the DUT side of the series resistors.
- The host never bridges `lan-a` to the uplink.

---

## 3. Network, capture and TLS

### 3.1 Topology (implemented in `hil/net/up.sh`, validated with bacserv stand-ins)

Replace the "DUT identity (D29)" bullet with:
- **DUT identity (D43; P1).** At commissioning (W1):
  1. Flash an instrumented image once (`hil/tools/hil-flash --probe $DUT_SN`) and read `HIL-BOOT … uid=<32 hex digits>` from the console.
  2. Compute the MAC with `hilrig.bench.mcxn947_mac(uid)`: AE:9A:22 followed by the CRC-24/OpenPGP (init 0xB704CE, poly 0x864CFB) of the 16 UID bytes. Compute the MQTT client id `z` + base32(UID).
  3. Confirm the MAC from the first DHCPDISCOVER with the unchanged tshark command (capture filter `udp src port 68 and udp dst port 67`, `-Y 'dhcp.option.dhcp == 1'`, `-c 1`).
  4. Write the UID, MAC, client id, probe serial, MCU-Link firmware version and CMPA SHA-256 into bench.yml.
  The v2.1 OpenOCD recipe (`mdw 0x1ff0f420 3`, `hilrig.bench.stm32_mac()`, prefix 02:80:E1) stays for P1-F767 only.

The rest of §3 is unchanged.

---

### 3.2 Host services (implemented in `hilrig.services` unless marked)

| Service | Netns | Notes |
|---|---|---|
| `Dnsmasq` | svc | `--dhcp-authoritative`, DUT reservation, `broker.hil.lan`, NTP option. `leases()` parses the lease file. **New (W2):** `point_broker(ip)` rewrites an `--addn-hosts` file and sends SIGHUP (D33). |
| `Mosquitto` 2.1.2 | svc .1 | - `eclipse-mosquitto:2.1.2-alpine` in docker; Ubuntu ships 2.0.18, which has no `--tls-keylog`. `--tls-keylog` is in the 2.1.2 man page (read).<br>- Listener 8883 (TLS, `require_certificate` for mTLS, `crlfile`) and an observer on 127.0.0.1:1883.<br>- `tls_version` is a **minimum**, so the DUT negotiates 1.3. |
| `TlsServer` | svc .2 | `openssl s_server -tls1_2 \| -tls1_3 -keylogfile`, optionally `-Verify 1`. Used for handshake negatives only (it does not speak MQTT). |
| TLS-1.2 front (**new, It1 W2**) | svc .2 | Python `ssl` terminator: `maximum_version = TLSv1_2`, `keylog_filename`. It forwards to mosquitto 127.0.0.1:1883 and gives a full MQTT session pinned to TLS 1.2. |
| `Chrony` | svc | wall clock for certificate-time tests (FW-12) |
| bacnet-stack tools @54544d02 (`make bip`, BBMD=full) | svc, fd, sim* | bacwi, bacrp, bacwp, bacrpm, bacwpm, bacscov, bacwh (Who-Has), bacrfdt, bacrbdt, bacepics, bacdcc, bacrd, bacts, bacserv; mstpcap in It2 |
| bacpypes3 | svc | independent second stack |
| BACnet SMP client (`bacnet_uc_harness.smp`, pip -e from the firmware checkout under test) | svc | `rig_config`, R-02b, SMP tests. It reuses the product team's client instead of a third one; FW-06 covers its API. |
| DTLS SMP client (**conditional**, only if FW-10 chooses DTLS) | svc | python `ssl` cannot do DTLS: use python-mbedtls or an `openssl s_client -dtls1_2` UDP proxy [likely], with a test PSK or certificate from the site config. Until it exists, `rig_config` falls back to SMP over the console shell transport (`MCUMGR_TRANSPORT_SHELL=y`), for instrumented images only. |

### 3.3 TLS 1.2 and 1.3

| Method | When | Mechanism |
|---|---|---|
| 1. Broker keylog (default) | every MQTT test | `mosquitto --tls-keylog` writes NSS lines. tshark decrypts with `-o tls.keylog_file:`. Covers TLS 1.3 handshake and application traffic. |
| 2a. `s_server -keylogfile` on .2 | TLS-01 negatives per version | `-tls1_3` / `-tls1_2` pin the version. The key log decrypts TLS 1.3 alerts. |
| 2b. TLS-1.2 front on .2 | MQTT-01/02 over TLS 1.2, TLS-03 per version | full MQTT session pinned to 1.2 |
| 3. Firmware shim `hil-keylog` (INSECURE, optional, It2) | third-party brokers only | `-Wl,--wrap=mbedtls_ssl_setup` plus `mbedtls_ssl_set_export_keys_cb()`. It prints NSS lines keyed by client_random:<br>- `CLIENT_RANDOM` (TLS 1.2 master secret);<br>- `CLIENT_/SERVER_HANDSHAKE_TRAFFIC_SECRET`;<br>- `CLIENT_/SERVER_TRAFFIC_SECRET_0`.<br>These map to the Mbed TLS 4.1.0 export types `MBEDTLS_SSL_KEY_EXPORT_TLS12_MASTER_SECRET` and `…TLS1_3_{CLIENT,SERVER}_{HANDSHAKE,APPLICATION}_TRAFFIC_SECRET` (ssl.h:1263-1270). SEC-01 greps for `KEYLOG `. |
| 4. Python malicious broker on .2 | TLS-04 (It3) | asyncio + ssl with keylog |

Expected DUT behaviour. The mapping is `mbedtls_ssl_verify_certificate()` in Mbed TLS 4.1.0 ssl_tls.c:8840-8866 (read). TLS 1.3 calls the same function (ssl_tls13_generic.c:670, read by the tools review).

| Case | TLS 1.2 on the wire | TLS 1.3 on the wire | Status |
|---|---|---|---|
| rogue CA, right name | DUT fatal alert 48 unknown_ca, plaintext | alert 48 encrypted under the client handshake secret; visible after decryption with the server key log | TLS-01 |
| right CA, wrong name | alert 42 bad_certificate (CN_MISMATCH wins over NOT_TRUSTED) | same, encrypted | TLS-01 |
| expired / not yet valid | no alert: dates are unchecked (`HAVE_TIME_DATE` off) | same | `xfail(strict)` until FW-12 |
| wrong keyUsage/EKU | alert 43 unsupported_certificate | same | TLS-06 (It2) |
| revoked | accepted (sockets_tls passes no CRL) | same | TLS-02 `xfail(strict)`, documented gap |
| client cert rejected by broker | handshake fails in `connect()` | client finishes, alert on first read (-7780); the DUT may already have sent an encrypted CONNECT | TLS-03 accepts both forms |
| RSA-only TLS 1.2 server | alert 47 with `X509_RSASSA_PSS_SUPPORT=y` (per MQTT session notes) | n/a | TLS-06 documents it. The test PKI is EC P-256, so It1 is unaffected. |

The PKI (implemented in `hil/pki`):
- a committed TEST-ONLY CA (EC P-256) and a DUT client cert, embedded at build time through the site config (D24);
- per-run leaves from `mkpki.sh`: srv-good, srv-wrongname, srv-expired, srv-revoked with a CRL, a rogue CA, rogue and revoked clients.

---

## 4. MS/TP bench bus (It2, gated on FW-07: D34)

The gate is revised by D47. Rig-side work runs in It2 on owned boards. DUT product tests wait for BACnet Phase 3.

### 4.1 Topology

```
 END L (K1 term, NC)                                                                    END R (K2 term, NC / FTDI)
 +-120R-o-o-+                                                                                  +-o-o-120R-+
D+ ===+=====+=======+==============+===============+==============+===============+===========+===========+
D- ===+=============+==============+===============+==============+===============+===========+===========+
      |             |              |               |              |               |           |
  bias K3 (NC): [fault box It3]   U1 DUT          U2 STIM         U3 sniffer      FTDI USB-RS485   peers (It2):
  D+ 510R->+5VA K4/K5 open (NO)   THVD1450        injector        listen-only     mstpcap + host   MCXA153 MAC 3
  D- 510R->GND  K6 short, K7 swap VCC = P3V3_MCU  VCC = stim 3V3  VCC = stim 3V3  node MAC 7       MCXA156 MAC 4
  (one point, always-on 5 V)      (J24 pin B),    FC3 RTS-DE (J9) DE=/RE=GND      latency_timer=1  own THVD1450,
                                  MAC 5           fixed lead/lag  RO -> LA/probe  (VCC wire        RTS hardware DE
                                  GPIO DE D11                                      insulated)
GND ======================== second CAT5e pair, all nodes ===========================================================
 taps (MSTP-01): D+ / D- -> 100k 0.1 % -> midpoint (100k 0.1 % + 100 nF to GND) -> 10 k -> stim rs485_a / rs485_b; pins chosen in It2
```

- Cable, stubs, terminators and bias are unchanged (D8). The +5VA PSU is an It2 purchase.
- The tap capacitors sit at the divider midpoints, never directly on a bus wire, so they do not load the drivers.
- Transceiver supplies:
  - **U1 runs from P3V3_MCU at J24 pin B**: the TPS22918 VOUT when the switch is fitted, the shunt output otherwise (P1-F767: DUT 3V3 CN8-7). So U1 goes down with the MCU.
  - U2 and U3 run from the stimulus 3V3.
  - Each peer runs from its own board 3V3.
  - Every RO pull-up goes to its own transceiver's VCC.
- The module rules (D6) apply to every transceiver, including the peers'.
- The FTDI USB-RS485-WE's red VCC (5 V) and its terminator wires are insulated and never reach a board.
- The Pico is dropped. The owned MCXA153 and MCXA156 are functional peers only (R-10 characterises them); they are never a timing reference.
- The rs485_a/b tap pins on the MCXN236 are chosen in It2 from the free ADC0 inputs. J2-5 is taken by `v3v3_brd` (Appendix E11).

### 4.2 DUT node (P1, snippet `hil`, FW-07)

| DUT pin | Signal | U1 | Extras |
|---|---|---|---|
| P4_2 J1-4 (D1) | LPUART2 TXD = FC2_P2 (combined LPI2C+LPUART mode). Both apps instantiate the UART at boot, so the pin is driven idle-high from boot. | DI | LA `tx`; stim `mstp_tx` (read only) |
| P4_3 J1-2 (D0) | LPUART2 RXD = FC2_P3, board pinctrl `bias-pull-up` | RO → 2.2 kΩ → D0; 10 k pull-up to U1 VCC at RO | LA `rx` (DUT side); stim `mstp_rx` (no pull) |
| P0_24 J2-8 (D11) | GPIO output `/zephyr,user mstp-de-gpios`, 1 = drive. The pad is DIS after reset. | DE + /RE (tied) | 10 k pull-down on the net; LA `de`; stim `mstp_de` |

Zephyr 4.4.2 facts the node relies on:
- `mfd_nxp_lp_flexcomm.c` picks `LP_FLEXCOMM_PERIPH_LPI2CAndLPUART` only when both FC2 children are `okay`. The board DTS enables both, so **`flexcomm2_lpi2c2` must stay okay**. With CONFIG_I2C=n its driver is not built, and P4_0/P4_1 stay free for m2/m3.
- In combined mode TXD/RXD are FC2_P2/FC2_P3 = P4_2/P4_3 (UM12018 net names). Where they would go in LPUART-only mode is [likely] only, because the RM was not obtained. W1 loopback confirms the pins.
- The board DTS enables `dac0` on P4_2 and `flexcomm1_lpspi1` on P0_24-P0_27. Neither driver is built in either app, and the snippet disables both nodes anyway.
- LPUART hardware DE exists only through RTS (`nxp,rs485-mode`), which the combined mode does not provide. **So D0/D1 get software DE only.**
- `gpio_mcux` sets PORT PCR MUX to GPIO on every configure, so `mstp-de-gpios` and the markers take their pads back from any earlier mux.
- UM12018 Table 17 notes an "MCU-Link UART" potential conflict on J1-2/J1-4. W1 check: with the DUT held in reset, a 10 k resistor to GND on each of D0 and D1 must read < 0.1 V.

What the rig measures (MSTP-04/05, instrumented):
- DE rise relative to the start bit (it must come first);
- Tpostdrive = DE fall minus the end of the last stop bit, 0 ≤ … ≤ 15 bit. A DE fall inside the stop bit is a truncated frame, which fails;
- Tturnaround and the inter-octet gaps at 9600, 38400, 76800 and 115200 baud.

The distributions are reported to the BACnet session (roadmap 3.1). Before FW-07 lands, the DUT's electrical bus safety (DE low and no garbage while the DUT is in reset, booting or unpowered) is already testable as MSTP-22a: the DE pad stays DIS with its 10 k pull-down until code drives it. P1-F767 counterpart: USART6 PG14 TX (CN10-14, D1), PG9 RX (CN10-16, D0, pull-up), DE on GPIO PD15 (CN7-18, D9); Appendix A.

The v2.1 texts on "USART2 hardware DE (F767)" and "P2 hardware DE needs FC2 in LPUART-only mode" are **withdrawn** (D37).

### 4.3 Capture and decode

The capture paths table is unchanged. Replace the paragraph "The rig is validated before any DUT MS/TP code exists (once the gate opens)" and its bullets with:

Rig validation (not gated since v2.2, D47):
- **R-08**: LA frames match mstpcap 1:1, and LA gaps are within ±(1 µs + 2 samples) of the injector's **commanded** gaps.
- **R-10** characterises the FRDM-MCXA153 and FRDM-MCXA156 peers: Tturnaround, Tusage and reply delays, plus their MAC (3/4) and Device instance (260203/260204). They are functional peers only, never a timing reference.
- **R-11** qualifies the nRF54L15 probe against the LA (D48).
- MSTP-01 and MSTP-22a run for real. The implemented MSTP-04/06/08/11/23 test code is dry-run against the FTDI host node, the peers and the injector. The DUT verdicts of MSTP-03..08, 11, 22b, 23 and 24 wait for FW-07 (BACnet Phase 3).

### 4.4 Clause 9 limits (bacnet-stack 54544d02 `mstpdef.h`/`mstp.h`; ASHRAE 135 not read)

Replace the Tpostdrive row's "DUT setting" cell with: "software DE (GPIO, FW-07/FW-17); measured, not configured". The rest of the table is unchanged.

### 4.5 Fault injection (stimulus protocol v0 caps `rs485`)

Replace the "event-timed collision" row with: "event-timed collision | TBD. The Pico PIO agent is dropped with the Pico. Candidates: an injector `tx` triggered by a DUT marker edge, or a hook in the peer image. | It3". The "DUT reset or power loss mid-TX" row uses `stim reset` and `stim power off` (`mcu_rail`, after W1). The other rows are unchanged; `rs485 break/fe/jam/de` take over the FC3 pins (P1_12/P1_13/P1_14) instead of PD5/PD4.

---

## 5. Stimulus board and protocol

### 5.1 Board (B3 confirmed)

v2.2: **FRDM-MCXN236** (`frdm_mcxn236`), owned (D36). The second NUCLEO-F767ZI is no longer planned.

Why:
- 3.3 V, with an Arduino-R3 header plus FRDM inner rows. J3-13 lines up with the DUT's J3-13 (ao1 goes straight across).
- MCX-N like the DUT: same LPADC, VREF, LP_FLEXCOMM and hal_nxp, and the same `linkserver` runner.
- 64-bit SysTick cycle counter at 150 MHz by default (`MCUX_OS_TIMER` default n), 6.67 ns quantum.
- FlexPWM input capture compiles on the A, B and X channels (`FSL_FEATURE_PWM_HAS_CAPTURE_ON_CHANNELA/B` = 1).
- 8 LP_FLEXCOMMs; the LPUART supports `nxp,rs485-mode` (RTS as DE); board.c clocks FC0-FC5.
- 16-bit LPADC0.
- 192 KB sram0.

Pitfalls:
- **No DAC.** An MCP4728 on the mikroBUS I2C (LPI2C2, P4_0/P4_1 at J5-6/J5-5) is an It1 purchase (D41). **J5's only supply pin, J5-7, is P5V0 (5 V)** (UM12041 Table 25). The module takes VDD from J6-7 (VDD_BOARD, 3.3 V), and its I2C pull-ups go to that 3.3 V or are removed. Otherwise 5 V pull-ups would reach P4_0/P4_1 and the FXLS8974, and the DAC could output up to 4.095 V into the DUT's ai pins.
- **The board DTS enables only gpio0, gpio1 and gpio4.** The overlay enables gpio2 and gpio3; without them the build fails.
- Header conflicts, never wired on the stimulus shield:
  - D4 = P0_21 (FXLS8974 INT1);
  - A1 = P4_15 (CAN RXD, driven by the TJA1057);
  - A2 = P4_16 (CAN TXD);
  - A3 = P4_17 (blue LED);
  - J3-15 = P3_12, the same pin as D3;
  - D5 = P2_7, the same pin as J3-13;
  - J1-3 = P2_0 (= D2);
  - J1-1 = P3_16 (= J3-7);
  - inner-row duplicates of used channels: J1-11 and J3-5 (P3_17 = D6, do2), J2-9 (P0_22 = D7, do3), J3-1 (P4_13 = A5, di0), J3-11 (P3_14 = D9, do0);
  - J5-7 (P5V0); the 5V (J3-10) and VIN (J3-16) stacking pins are clipped on the shield.
- Rerouted: di3 goes to J3-3 (P4_21); the ai0/ai1 senses go to J2-7/J2-3; ao0 goes to J3-7.
- **D10-D13 (P1_3..P1_0) also go to the MCU-Link USB-SPI bridge** through R153-R156, populated by default (UM12041 §3.9). In that bridge the MCU-Link is the SPI host, so SCK (P1_1 = m1), host data (P1_2 = m0) and PCS0 (P1_3 = do1) are MCU-Link outputs. **R153-R156 are removed before the W2 wiring**; the stimulus never uses USBSIO. The removal is recorded in bench.yml.
- **D3 (P3_12, ao2) is shared with IO3 of the on-board QSPI flash U12** through a zero-ohm selection (UM12041 §2.6). A pull-up on IO3 and the 2.2 kΩ series resistor would lift the stimulus-side low level. W2 check: with the DUT driving D3 low, P3_12 reads < 0.8 V. Otherwise U12 is isolated with its zero-ohm resistor (the stimulus never uses the QSPI flash).
- **DUT, stimulus and peers are all MCU-Links.** Always select by full serial (`--probe`, D42), keep the stimulus out of the Twister hardware map, and key udev `/dev/hil/{dut0,stim0,peer3,peer4}` on `ID_SERIAL_SHORT` with `ID_MM_DEVICE_IGNORE=1`.
- Not 5 V tolerant, like the DUT. Nothing in the rig is above 3.3 V except the RS-485 bus, which only transceivers touch.

**Fallback: FRDM-MCXA156.** It has the only DAC among the candidates (J2-9). Its A4 and A5 are CAN RXD/TXD (P1_12/P1_13, UM12121) and are never wired. It needs:
- the upstream `pwm_mcux.c` capture guards (X-channel capture only: D3 PWM0_X0, D5 PWM0_X2, D10 PWM0_X1);
- `CONFIG_MCUX_OS_TIMER=n`;
- an LPUART2 node plus a clock hook for D0/D1.

The injector goes on LPUART1 (P3_20/P3_21, RTS P3_22).

### 5.2 Firmware `apps/hil_stimulus` (implementation; ported this session, committed in It1 W0)

The v2.2 port has three layers, all under `scratchpad/v22/`:
- `stimport/port.patch`;
- the lead's `mstp_rx` change;
- the review patch `editor/stim-v22-review.patch`, applied on top of `lead/stim-app`. The patched sources are in `editor/stim-app`.

Details:
- **Result.** The port builds for `frdm_mcxn236` with 0 warnings (warnings as errors): 100,244 B / 42,976 B. The same sources build for `nucleo_f767zi` at 92,324 B / 42,688 B. The F767 behaviour is unchanged, because its di0 sets `drive-high`.
- **Compile blocker.** The unported source stops at `stm32_ll_usart.h`. `rs485_tx_done()` now picks `ISR.TC` (STM32) or `LPUART STAT.TC` (`nxp,lpuart`) from the devicetree compatible.
- **`pwm_mcux` (4.4.2) defects handled in the app:**
  1. `PWM_CAPTURE_TYPE_BOTH` returns -ENOTSUP, so the app takes a PERIOD capture and then a PULSE capture.
  2. `capture_active` is never cleared after a single capture (the next call returns -EBUSY), so the app always calls `pwm_disable_capture()`.
  3. INIT and VAL1 are never programmed on a capture-only submodule.
  4. RUN is set only when no submodule runs.

  `cap_prepare()` handles 3 and 4: it sets INIT=0, VAL1=0xFFFF, LDOK and RUN. W2 bench check.
- **Other replacements:**
  - DAC: STM32 DAC → MCP4728 (`microchip,mcp4728`). A NAK sets ERR -19 (hardware absent).
  - Capture: TIM capture → FlexPWM1 SM0/SM2 with prescaler 16 (106.7 ns ticks).
  - ao pin level: IDR → `OCTRL.PWMx_IN`.
  - Watchdog: IWDG → WWDT0 (1 MHz clock, 4 s = 1,000,000 ticks).
  - Reset cause: RCC_CSR → CMC through `hwinfo_mcux_mcx_cmc.c`.
  - ADC full scale from `zephyr,resolution` (16-bit on MCX).
- **Lead change.** `mstp_rx` has no pull. A stimulus pull-down against the DUT pad pull-up would hold D0 near mid-rail, a permanent break, whenever RO is not driving.
- **Review patch (v2.2 reviews; built as above):**
  - New binding property `drive-high` (boolean, di only). Without it a di channel is 0/z only: `dout`, `pulse` and `lat` refuse level 1 with ERR -1. No P1 channel sets it. The F767 map sets it on di0, because B1 on PC13 is active high. `gpio_mcux` rejects `GPIO_SINGLE_ENDED` (-ENOTSUP), so open-drain flags cannot do this in devicetree.
  - `dac` refuses non-zero values while `pwr` is off **or** the `v3v3` sense reads < 3000 mV (4 samples), so uncommanded `usb_port` cuts are covered.
  - The DAC settle wait is 2.5 ms, at least 10 τ of the 2.2 kΩ / 100 nF node (it was 1 ms for the v2.1 1 kΩ node).
  - `nrst_sense` on P1 is GPIO_ACTIVE_HIGH behind Q3 (overlay only, D50).
  - `cmd_adc` and the new DAC gate share one `sense_mv()` helper.
- **Watchdog under debug.** Whether the WWDT keeps counting during a debugger halt is unknown [unverified: W2].
- **Channel map and profiles.** The stimulus overlay is `boards/frdm_mcxn236.overlay` with `profile = "P1"` (full text in the pin tables §2). `boards/nucleo_f767zi.overlay` stays the P1-F767 map of v2.1, plus `drive-high` on di0.

### 5.3 Protocol v0 summary (full specification: `stimulus_protocol`)

v2.2 note: protocol v0 is unchanged. The board-specific figures move into a per-board note (D49):

| Item | F767 | MCXN236 |
|---|---|---|
| Transport | ST-LINK VCP (USART3) | MCU-Link VCOM (FC4) |
| Quantum | 4.63 ns | 6.67 ns |
| dac | 200..3100 mV, on-chip | 0..3200 mV, MCP4728, 1 mV per LSB |
| dac refusal | `pwr` off, or `v3v3` < 3000 mV | same |
| di levels | 0/1/z (di0 has `drive-high`) | 0/z only |
| nrst_sense | through 10 kΩ, ACTIVE_LOW | Q3 gate sense, ACTIVE_HIGH (D50) |
| adc | – | 16-bit, 131 ADCK at 48 MHz, VDD_ANA reference (3300 mV nominal, DMM-corrected) |
| pwmcap | – | ±2 ticks ≈ ±213 ns; ≤ 6.99 ms per wrap; period and pulse from consecutive cycles |
| rs485 DE lead/lag | – | fixed by hardware, measured in R-08 |
| Safety invariant 1 (PA15 exception) | applies | does not apply: SWD pins P0_0..P0_3 are unused |

`caps=` stays `pwmcap,rs485,rstmon` plus the It2 additions.

**stimulus-protocol.md §6 for the MCXN236 stimulus** (the v2.1 texts stay for P1-F767):
1. Hardware fail-safe without firmware: unchanged in substance, without the PA15 exception. In addition, the state **"stimulus unpowered or halted, DUT on"** is defined and tolerated:
   - RESET_B sees only Q3's gate (D50).
   - The NRST and PWR FETs are held off by their 100 k gate pull-downs, so the DUT stays powered and out of reset.
   - Each DUT output that idles high (do0-do2 with the LEDs off, D1 TX, ao) pushes at most about (3.3 − 0.5)/2.2 k ≈ 1.3 mA into the unpowered stimulus. That is within ±3 mA per pin and ±25 mA per 16 contiguous pins.
   - The DUT's pulled-up DIs read "active" through the clamped stimulus pins.
3. While the DUT is unpowered (commanded, or `v3v3` < 3000 mV), analog sources refuse non-zero values (ERR -1). Digital outputs stay allowed: di is 0/z only, and every line has 2.2 kΩ, so injection into an unpowered MCXN pin is ≤ 1.36 mA (D40). The v2.1 sentence "DUT FT pins accept VDD + 4 V" applies to P1-F767 only.
4. One 2.2 kΩ resistor per DUT line, on the DUT shield (D40).
5. No stimulus pin ever sees more than 3.3 V. The MCP4728 runs from VDD_BOARD 3.3 V. The only 5 V sources near the rig are the +5VA bias PSU and the FTDI, which touch only the RS-485 bus. The 5V and VIN stacking pins are clipped.
6. NRST is driven open-drain only, through Q2. There is no pull-up on RESET_B. P1 senses it through Q3's gate, whose drain is pulled up to the stimulus 3.3 V; the sense input has no internal pull.
9. The ao pins stay in FlexPWM capture mode (pinctrl at init); `safe`, `edges` and `lat` never touch them. Interrupts are per-pin ICR, and "ao is never armed" stays as policy.

### 5.4 Host driver `hilrig.stim` (implemented) and It1 follow-ups

Replace follow-ups 6 and 9 with:

6. bench.py: `RIG_CHANNELS` += `lb`, `v3v3`, `v3v3_brd`, `mstp_rx`, `mstp_tx`, `mstp_de` for P1; `v5` only for P1-F767. The P1 catalog is the 15 MCXN947 channels with their polarity flags; the `hil-io` extras exist only on P1-F767. `hil/tools/check_catalog.py` checks them against `build/zephyr/edt.pickle` in the build job and adds the D38 and D44 rules.
9. Align `hil/host/bench.yml.example` with this design:
   - stimulus `board=frdm_mcxn236`, `profile=P1`, and the P1 `chans` (D49);
   - the LA map of §6;
   - `mqtt_client_id` from the UID (FW-02);
   - the probe comment `--probe <full MCU-Link serial>` (never `-i` or `#N`);
   - the MAC prefix AE:9A:22 with `mcxn947_mac()` in the `bench.py` docstring;
   - the records of the W1/W2 commissioning (R153-R156 removed, V_ON, RESET_B, Q3 offset).

Add follow-up 10: `hil/tools/hil-flash` (D42) and a unit test for its refusals.

---

## 6. Logic analyzer channel map

Probes sit on the DUT side of the 2.2 kΩ resistors, on a 2×5 header on the DUT shield, with one ground per 4 signals. RS-485 A/B is never probed with a digital channel. `bench.yml la.channels` uses the names below.

| CH | It1 FX2 (24 MS/s, 8 ch) | It2 DSLogic Plus (16 ch) | Source (P1) | Rate needed | Used by |
|---|---|---|---|---|---|
| 0 | `nrst` | `de` | RESET_B J3-6 / P0_24 D11 | 1 / ≥ 12 MS/s | RST-01, TIM-04 / MSTP-04, 05 |
| 1 | `m0` | `tx` | P0_26 D12 / P4_2 D1 | ≥ 12 MS/s | boot milestones / MS/TP |
| 2 | `m1` | `bus_ro` | P0_25 D13 / U3 RO | ≥ 12 MS/s | R-02a / Tturnaround |
| 3 | `di2` | `nrst` | P0_29 D2 / J3-6 | 1 MS/s | R-02a, IO-04 / reset |
| 4 | `do3` | `m0` | P0_31 D7 / D12 | ≥ 12 MS/s | IO-01 / events |
| 5 | `do0` | `m1` | P0_10 D9 (red LED = mqtt `led0`) / D13 | 4 MS/s | MQTT-03 / TIM |
| 6 | `vcp_tx` | `di2` | P1_9 at camera header J9-30 (no solder bridge) / D2 | 4 MS/s | log correlation |
| 7 | `sync` | `do3` | stim P1_7 J1-13 / D7 | 1 MS/s | clock fit |
| 8 | – | `rx` | P4_3 D0 (DUT side of the RO resistor) | ≥ 12 MS/s | MS/TP RX |
| 9 | – | `stim_de` | stim P1_14 J9-2 | ≥ 12 MS/s | collisions |
| 10 | – | `m2` | P4_0 SDA | | net-to-app |
| 11 | – | `m3` | P4_1 SCL | | idle/token |
| 12 | – | `ao0` | P2_6 J3-15 | ≥ 10 MS/s | IO-11 cross-check |
| 13 | – | `sync` | | 1 MS/s | R-05, R-09 |
| 14 | – | `vcp_tx` | | 4 MS/s | |
| 15 | – | `do0` (profile `pers`: `p3v3_brd`) | – / separate 100k/100k divider on J3-8 | 4 MS/s | – / PERS-02, OTA-04 cut time stamp (D51) |

- The FX2 goes on a **host root port**. R-02a checks the sample count on a 60 s capture.
- The DSLogic Plus rules of v2.1 are unchanged:
  - PID 0x0020 only;
  - 100 MHz × 16 in ≤ 0.16 s windows for latency tests;
  - MS/TP captures in stream mode at 20 MHz × 16;
  - no 400 MHz.
- **Capture profile `pers`** (It2, D51): CH15 takes `p3v3_brd` from its own 100k/100k divider on J3-8, not from the stimulus sense divider, so the real `usb_port` power loss is time-stamped next to the FW-16 marker. The threshold is chosen in It2 [proposed: DSLogic threshold setting to be confirmed].
- **nRF54L15 edge probe (It2, optional; D48).**
  - Preparation, before any cable is attached, in Board Configurator:
    - set VDD to 3.3 V;
    - disconnect the UART0 TXD/RXD group (P0.00-P0.03);
    - **disconnect the UART1 RTS/CTS group (P1.06/P1.07) from the debugger** (analog switches U4/U5).
  - Why the RTS/CTS step: the probe console on VCOM1 needs DTR, and on DTR the debugger runs automatic HWFC detection by driving CTS. If it detects HWFC it keeps driving P1.07 (= `bus_ro`) until a power-on reset (DK UG v1.0.0 §3.1.3).
  - Verify that P1.07 is Hi-Z with the VCOM1 terminal open.
  - Inputs through 4.7 kΩ at the DUT-side probe points: P1.11 `de`, P1.12 `tx` (GPIOTE20), P0.00-P0.03 `m0`-`m3` (GPIOTE30), P1.06 `sync`, P1.07 `bus_ro`.
  - Captures come from TIMER00 at 128 MHz through DPPI/PPIB. Effective resolution is about 62.5 ns (16 MHz peripheral synchronisation) [unverified: R-11].
  - It is a timing source only after R-11.
- The SYNC fit (R-05) is unchanged; the MCXN236's SYNC is P1_7.

---

## 7. Power and reset

### 7.1 DUT board configuration (P1; full solder-bridge checklist in the pin tables)

| Item | Setting | Why |
|---|---|---|
| J24 | Shunt removed; **TPS22918 fitted** in W2, on a keyed or labelled 2-pin housing. PWR-01, PERS-01 and OTA-04 use it only after the W1 back-feed check passes (D39). | J24 is the only feed of P3V3_MCU: VDD_P0..P4, VDD_USB, VDD_BAT/P5, VDD_ANA, VDD_CORE_SYS (J25-J28 are DNP). |
| J17 (MCU-Link USB-C) | on the uhubctl hub; the only USB path | power, SWD, VCOM; `usb_port` cuts |
| J11 (HS USB) | **never connected** | it would feed P5V0 during a `usb_port` cut |
| J3-10 (5V = P5V0 input), J3-16 (VIN) | **never connected**; the matching stacking pins are clipped on the DUT shield | board supply inputs |
| Shields | component-free Arduino proto shields: no power LED, reset button or ICSP header | the 5V and VIN rails of a stacked shield are the only 5 V near the MCX pins |
| J18 / J19 / J22 | open / open / shorted (defaults) | VCOM on; onboard SWD on; SWD clock |
| J21 | **never fitted** | forces MCU-Link ISP |
| SW3 / P0_6 (catalog di1) | never pressed or driven | ISPMODE_N: low at reset = ROM ISP |
| R154 | DNP (default: Y3 enabled). **Never populated.** | Populating it disables Y3 permanently: no PHY clock, no Ethernet (UM12018 §2.4). |
| W1 DMM checks | Board unpowered: J24 pin A reads 0 Ω to J3-8 (P3V3), pin B reads 0 Ω to AREF J2-16 (VDD_ANA via R152). On J17 with the shunt removed: back-feed per §7.3; RESET_B idle ≥ 3.0 V. | before the switch is fitted; a voltage check alone is ambiguous because pin B is back-fed |
| bench.yml | MCU-Link serial and firmware version, LinkServer version, UID, MAC, CMPA SHA-256 | commissioning |

### 7.2 Circuits (fixes v1 blocker 0.0; fail-safe = DUT on; no stimulus pin ever sees 5 V)

**MCU-rail switch (It1, D39)**: a short twisted pair to J24, under 15 cm, identified by continuity and keyed.

```
 J24 pin A = P3V3 (always on, LDO U2 from P5V0)                     J24 pin B = P3V3_MCU
     |                                                                     |
     +---+---> VIN(1)  U1 TPS22918DBV  VOUT(6) --+--[It2: INA226 0.1 R]---+---> also U1 THVD1450 VCC (It2)
     |   |             QOD(5) ------------------+   QOD tied to VOUT: 25 R typ / 35 R max discharge when OFF
   C1 1uF|             CT(4) --- C2 1 nF --- GND     tR ~ 1.7 ms at 3.3 V
     |   |             GND(2) -- DUT GND
    GND  +--[R1 100k]--+-- ON(3)       ON active high: VIH >= 1 V, VIL <= 0.5 V, must not float
                       |
                       D  Q1 BSS138P (2N7002 acceptable)
 STIM P3_18 'pwr' --[1k]--G      pwr pin high -> Q1 on -> ON low -> MCU OFF
                  |    S--GND
               [100k]            stimulus off, in reset, hung or ribbon unplugged -> Q1 off -> ON high -> DUT ON
                  |
                 GND
```

- The switch is powered from the DUT's own P3V3, so there is no 5 V in the path. TPS22918: 2 A, 52 mΩ.
- Commissioning check (W2, recorded in bench.yml):
  - V_ON > 2.5 V with the line released and < 0.3 V when cut;
  - `stim adc v3v3` < 300 mV within 200 ms of `off`.

**NRST (both iterations; D50):**

```
 DUT RESET_B J3-6 (internal RPU 33-75 k, SW1 0.1 uF, MCU-Link reset)
   +--[100 R]-- D  Q2 BSS138P  S -- GND      G <--[1k]-- STIM P2_8 'nrst' (1 = asserted), 100k G->GND
   +--[1 k]---- G  Q3 BSS138P  S -- GND      D ==ribbon 33==+--[10 k]-- stim VDD_BOARD (3.3 V, stimulus shield)
                  (DUT shield)                               +---------- STIM P3_6 'nrst_sense' (no pull; high = in reset)
 DUT AREF J2-16 (VDD_ANA, switched) -> 100k/100k + 100 nF -> 10 k -> STIM P0_29 'v3v3'
 DUT J3-8 (P3V3, always on)         -> 100k/100k + 100 nF -> 10 k -> STIM P0_27 'v3v3_brd'
```

- **Release** takes milliseconds: VIH is reached after about 1.2 × RPU × 0.1 µF ≈ 4-9 ms. Boot timing always starts from the **sensed** release edge.
- **RESET_B carries only Q3's gate**, so its level does not depend on the stimulus's power state. Through a plain 10 kΩ, an unpowered stimulus (input clamped near 0.5 V) would divide RESET_B to about 0.8-1.2 V, near VIL max 0.99 V.
- Q3 switches at VGS(th) 0.9-1.5 V, earlier than the MCU's input threshold. The offset between the sensed edge, the LA `nrst` edge and the boot marker is measured in W1/W2, and RST-01 uses that window.
- The 100 Ω limits contention if the MCU-Link reset driver is push-pull (W1) and limits the discharge of the 0.1 µF (τ ≈ 10 µs). The RESET_B passive filter needs ≥ 330 ns, so a 10 ms pulse is fine.
- Commissioning (W2, DMM): RESET_B ≥ 3.0 V with the stimulus idle **and with its hub port off**.
- Open questions for W1:
  - Does the MCXN drive RESET_B low on internal resets (WWDT, software)? `rstmon` and RST-03/RST-04 depend on it. If it does not, the reset cause comes from `HIL-BOOT reset=`.
  - Does RESET_B reset the LAN8741? RST-01 records whether the link drops.
- Never add a pull-up and never drive RESET_B push-pull.

### 7.3 Mechanisms

**P1 power model (UM12018 2.1).**
- P5V0 comes from J17 (default), J11 or J3-10. LDO U2 turns it into P3V3.
- P3V3 powers the MCU-Link LPC55S69, W25Q64, Y3 (50 MHz), the LAN8741 VDDIO/VDDA, the RGB LED, CAN (TJA1057) and the PTN5150A.
- J24 feeds P3V3_MCU from P3V3.
- So a J24 cut removes power from the MCU alone, with all MCU rails ramping together. The MCU-Link stays enumerated (VCOM and SWD), and the PHY keeps its link to the host NIC.
- The QSPI NOR stays powered and finishes any erase or program already started. **True NOR power-loss tests need `usb_port`** (D51). The internal flash, which holds the MCUboot slots, does lose power on `mcu_rail`.
- The DUT's clock does not depend on the probe, so the F767 MCO hazard (v2.1 risk 2) does not exist on P1.

| Mechanism | Command | Effect | Use |
|---|---|---|---|
| NRST pulse | `stim reset 10` | MCU reset; PHY reset unknown (W1); cause PIN | RST-01, default reset |
| Probe reset or flash | `hil/tools/hil-flash -d <b> --probe <full SN>` (linkserver) | SWD | flashing, recovery |
| Mass erase | – | **forbidden on P1** (D42): LinkServer `erase` runs without the board's flash overrides and may cover more than the internal flash [unverified: tools review] | OTA sessions use SMP `image erase` (D25) |
| Soft reboot / fatal | SMP `os reset`, `hil panic` (instrumented) | cause SOFTWARE; fatal → reboot per FW-05 (MQTT: WWDT via task_wdt; BACnet: missing) | RST-04 |
| MCU-rail cut (`mcu_rail`) | `stim power cycle 2000`, `off`/`on` | MCU only; MCU-Link, VCOM, PHY, NOR and Y3 stay up | PWR-01, PERS-01, OTA-04 (after W1; D51) |
| USB port cut (`usb_port`) | `uhubctl -l <hub> -p <dut port> -a cycle` | whole board, including the MCU-Link and NOR; the VCOM re-enumerates | PERS-02 (host-timed NOR power loss, D51); PWR-01, PERS-01 and OTA-04 until W1 passes; recovery |
| Stimulus recovery | uhubctl on the stimulus port | stimulus only; the DUT stays on (fail-safe; see the hazards) | PWR-02 |
| PPK2 / programmable PSU on J24 (VIN side) | host | MCU-rail brown-out ramps | It3 PWR-04 |

Sequences (fixtures):

```
power_cycle(off_ms=2000, method=mcu_rail):
    stim.safe()                                   # di z, MCP4728 0 V, NRST released
    stim.power("off")
    wait stim.adc("v3v3") < 300 mV                # > 200 ms -> FAIL "back-feed"
    assert stim.adc("v3v3_brd") >= 3100 mV        # board rail (MCU-Link, PHY, NOR) stayed up
    sleep(off_ms)
    stim.power("on")
    wait v3v3 >= 3000 mV
    boot timer starts at the nrst_sense release edge (plus the W1 offset)
    wait DHCP lease + I-Am/CONNACK (release) or HIL-READY (instrumented; assert HIL-BOOT reset = POR)
    # console stays open: the VCOM is on P3V3
power_cycle(off_ms, method=usb_port):
    stim.safe(); console.close(); uhubctl off; sleep(off_ms); uhubctl on
    # the stimulus refuses analog output meanwhile: its v3v3 sense reads < 3000 mV
    console.open(retry_s=10); wait network as above
reset():
    stim.reset(10) -> nrst_sense/LA confirm -> wait link + DHCP + I-Am/CONNACK
session start (D31):
    stim.info() (proto=0, board=frdm_mcxn236, profile=P1) -> stim.safe()
    -> R-07 guard (full probe serial present exactly once, static image guard)
    -> flash (Twister `dut` or hil-flash --probe) -> wait network
    -> rig_config (BACnet) / SMP mqtt/factory_reset (MQTT) -> R-02b (BACnet)
```

Hazards:
- Off times in tests are ≥ 200 ms.
- **Back-feed during a J24 cut** [unverified: W1]. Sources, all on the always-on P3V3:
  - Y3's 50 MHz CMOS output into P1_4;
  - the PHY RMII outputs, and the MDIO pull-up on P1_21;
  - the MCU-Link VCOM TX into P1_8 (R173), and its SWD and reset lines;
  - the TJA1057 RXD output into P1_11 (SJ26 1-2, the default) and its TXD pull-up on P1_10 (SJ16 1-2);
  - the PTN5150A, if it connects to MCU pins [likely];
  - the LED anodes, through the LEDs.

  QOD holds the rail near 0 V (25 Ω typ, 35 Ω max while the switch is disabled), so each source is limited only by its own output impedance.
  - W1 measurement: with J24 open, measure the open-circuit voltage on pin B and the current into a 22 Ω shunt from pin B to GND.
  - **Pass only if the current is < 3 mA (< 66 mV across 22 Ω) and pin B stays < 0.3 V.** No single pin can carry more than the aggregate, so this protects the ±3 mA per-pin limit. Otherwise `mcu_rail` is not used, and PWR-01, PERS-01 and OTA-04 run on `usb_port`. There is no rework fallback: populating R154 would stop Y3 and the Ethernet.
- **Stimulus unpowered or halted, DUT on** (PWR-02, stimulus recovery): a defined, tolerated state.
  - RESET_B sees only Q3's gate (D50).
  - The NRST and PWR FETs are held off by their 100 k gate pull-downs.
  - Each DUT output that idles high pushes ≤ 1.3 mA into the unpowered stimulus through its 2.2 kΩ, within ±3 mA per pin and ±25 mA per 16 pins.
  - The DUT's pulled-up DIs read "active"; PWR-02 records the BI states but does not assert them.
- Rig lines into an unpowered DUT:
  - The 2.2 kΩ resistors limit injection to 1.36 mA per pin.
  - **DI lines are 0/z only, enforced by the stimulus firmware** (level 1 → ERR -1; no P1 channel has `drive-high`).
  - Sense lines use ≥ 10 kΩ.
  - `stim safe` sets the MCP4728 to 0 V and the DI lines to z before every cut.
  - The analog sources refuse non-zero values while `pwr` is off **or the `v3v3` sense reads < 3000 mV**, which also covers `usb_port` cuts that the stimulus did not command.
- The P3V3 LDO U2 carries the MCU-Link, PHY, NOR, MCU and, in It2, U1 while it drives the bus. Its budget is not published [unverified: W2 current measurement].

---

## 8. Software stack

### 8.1 Repository layout (HIL branch; HIL-owned)

v2.2 changes to the tree (the rest of the v2.1 layout stands):

```
docs/SESSION_NOTES.md        HIL section; after D45 it moves to docs/hil/SESSION_NOTES.md on main
hil/site/                    mqtt-site-hil.conf, mqtt-site-hil-mtls.conf, variant-publish120.conf
                             (f767-app-pool.overlay retired, D24)
hil/tools/                   survey.sh, check_catalog.py (with the D38 and D44 rules), mcxn_flash_guard.py,
                             hil-flash (D42 wrapper)
apps/hil_stimulus/           boards/frdm_mcxn236.{overlay,conf} (P1), boards/nucleo_f767zi.overlay (P1-F767);
                             binding property drive-high (review patch)
apps/hil_mstp_node/ (It2)    MCXA153/MCXA156 peers; boards/frdm_mcxa153.conf (MAC 3, APDU 480),
                             boards/frdm_mcxa156.conf (MAC 4, APDU 480); alias mstp-uart0
apps/hil_edge_probe/ (It2)   nRF54L15 DK probe; nrf54lm20dk/nrf54lm20b/cpuapp overlay as the alternative
snippets/hil/boards/         frdm_mcxn947_mcxn947_cpu0.overlay (P1), nucleo_f767zi.overlay (P1-F767, planned UART)
snippets/hil-io/             P1-F767 only
```

### 8.2 Host package (implementation)

Changes for v2.2:
- `dut_power` supports both methods (`mcu_rail`, `usb_port`); PERS-02 uses its host-timed `usb_port` cut (D51).
- conftest `pytest_configure` exports `HIL_BUILD_DIR` from `config.option.build_dir` (the harness's `--build-dir`). The map.yml `pre_script` runs with no arguments and no cwd but inherits the pytest environment, so it finds the build (§8.4). `hil_session` runs the full R-07 guard itself before it requests `dut`.
- Replace the local Twister-harness command with:

  `pytest --twister-harness --device-type=hardware --platform=frdm_mcxn947/mcxn947/cpu0 --runner=linkserver --device-id=$DUT_SN --device-serial=/dev/hil/dut0 --build-dir=<b> --twister-fixture hil_bench:/etc/hil/bench1/bench.yml --dut-scope=session`

  `$DUT_SN` is the full MCU-Link serial. The harness adds `--probe=$DUT_SN` for linkserver (hardware_adapter.py:111-112). Do not add `-p`.

### 8.3 Firmware images per profile (workspace = a firmware branch checkout as the west manifest repo; `$HIL` = the HIL checkout)

| Image | Command | Tier |
|---|---|---|
| P1 BACnet release (plain) | `west build -b frdm_mcxn947/mcxn947/cpu0 $FW/firmware -d b/p1-bac-rel`. No site additions (FW-04 met). **`$FW` = the BACnet branch tip (at least 6d1a151) until main has the storage shrink (D24, D44).** Built at 4ee098e: 509,808 B / 358,680 B. main e62a095 builds (507,236 B / 358,144 B) but fails D44. | release |
| P1 BACnet release, MCUboot (It2, OTA-* only) | `west build --sysbuild -b frdm_mcxn947/mcxn947/cpu0 $FW/firmware -d b/p1-bac-mcuboot`, which is scenario `bacnet_uc.firmware.mcuboot.mcxn947` (sysbuild.conf applies swap-using-offset; slots in the internal flash). The OTA candidate adds `-Dfirmware_CONFIG_UC_FW_VERSION=\"<ver>-hilota\"` [likely: sysbuild per-image prefix]. Flash the sysbuild build dir. | release |
| P1 BACnet instrumented | plain + `-S hil -DSNIPPET_ROOT=$HIL -DZEPHYR_EXTRA_MODULES=$HIL/lib/hil` (no `hil-io` on P1). Built at 4ee098e: 511,676 B / 358,752 B (the same at 6d1a151). | instrumented |
| P1 MQTT release (plain) | `west build -b frdm_mcxn947/mcxn947/cpu0 $MQ/apps/mqtt_tls -d b/p1-mq-rel -- -DEXTRA_CONF_FILE=$HIL/hil/site/mqtt-site-hil.conf` | release |
| P1 MQTT release, mTLS | + `mqtt-site-hil-mtls.conf` | release |
| P1 MQTT release, MCUboot (It2) | `west build --sysbuild … -- -DFILE_SUFFIX=mcuboot` (scenario `app.mqtt_tls.mcuboot`). Built at 75e0620: app 291,924 B, MCUboot 48,052 B, swap-using-offset, dev key. | release |
| P1 MQTT variant (It2) | plain + `variant-publish120.conf` | release (variant) |
| P1 MQTT instrumented | plain + `-S hil` + module. Built at e859980: 315,144 B / 175,144 B. | instrumented |
| Stimulus | `west build -b frdm_mcxn236 $HIL/apps/hil_stimulus -d b/stim` (100,244 B / 42,976 B with the review patch) | rig |
| MS/TP peers (It2) | `west build -b frdm_mcxa153 $HIL/apps/hil_mstp_node` and `-b frdm_mcxa156`, with the committed board confs: `frdm_mcxa153.conf` = `CONFIG_MSTP_NODE_MAC=3`, `CONFIG_BACNET_MAX_APDU_SIZE=480`; `frdm_mcxa156.conf` = `CONFIG_MSTP_NODE_MAC=4`, `CONFIG_BACNET_MAX_APDU_SIZE=480`. Device instance = 260200 + MAC. The research builds used the Kconfig default MAC 10 on both boards and MAX_APDU 1476 on the MCXA156, which is the very defect §4.4 hunts. Research sizes (MAC 10 builds): 68.8 KB / 20.5 KB and 69.8 KB / 32.8 KB. The alias becomes `mstp-uart0`. | rig |
| nRF edge probe (It2) | `west build -b nrf54l15dk/nrf54l15/cpuapp $HIL/apps/hil_edge_probe` (probe build: 70.0 KB / 11.8 KB). Alternative: `-b nrf54lm20dk/nrf54lm20b/cpuapp` (the v0.7.0 DK carries the nRF54LM20B; built in research). | rig |
| P1-F767 | v2.1 commands, without the app-pool overlay (FW-04 met at e62a095). `-S hil` applies the planned-UART overlay (Appendix A). | – |

- Never build `//cpu0/qspi` or `/ns`.
- `lib/hil` and `hil.conf` are unchanged from v2.1.
- `lib/hil` prints `uid=` as the 16-byte hwinfo UID in hex, the input of D43.

### 8.4 Flashing and guards per profile

| Profile | Runner | Command | Guard (R-07, before every flash) |
|---|---|---|---|
| P1 | `linkserver` (default; factory CMSIS-DAP MCU-Link firmware; LinkServer version pinned; CI never runs `LinkServer probe … update`) or `pyocd` 0.45.1 with NXP.MCXN947_DFP 26.06.00 (`pyocd pack install mcxn947vdf`, pack file pinned) | `hil/tools/mcxn_flash_guard.py <b> && hil/tools/hil-flash -d <b> --probe $DUT_SN` | - Static guard:<br>  - `CONFIG_BOARD_TARGET` is exactly `frdm_mcxn947/mcxn947/cpu0`;<br>  - the runner's bin/hex and every ELF PT_LOAD with file data lie inside 0x10000000-0x10200000;<br>  - sysbuild `domains.yaml` is handled.<br>- The full probe serial is present exactly once.<br>- CMPA SHA-256 equals the commissioning value (read-only, daily; address and tool: W1).<br>- After the flash, the DHCPDISCOVER MAC equals bench.yml (D43).<br>- Never `-i`, `#N`, a missing `--probe`, or `--erase` with linkserver; never blhost, J21, `/qspi` or `/ns`. |
| P1 under Twister | runner `linkserver`, `id` = the full MCU-Link serial, `product` = any string (required by the schema, ignored for linkserver); Twister adds `--probe=<id>` (handlers.py:580-584) | map.yml (below) | `pre_script: /opt/hil/bin/pre-flash.sh` runs with no arguments and no cwd (hardware_adapter.py:188-191) and inherits `HIL_BUILD_DIR` from the conftest. It runs `mcxn_flash_guard.py "$HIL_BUILD_DIR"` and exits non-zero if the variable is unset (fail closed). Only the pytest-harness path aborts the flash on a failing pre_script; Twister's own DeviceHandler only logs it (handlers.py:499-509). Every HIL scenario is `harness: pytest`, and `hil_session` also runs the full guard first. |
| Stimulus | `hil-flash --probe $STIM_SN` (linkserver, `--device=MCXN236:FRDM-MCXN236` from board.cmake); pyocd `--target=mcxn236` fallback [unverified: W2; the MCXN236 pack is not installed] | | the same static guard, parametrised for the MCXN236 flash range [proposed] |
| MS/TP peers (It2) | `hil-flash --probe $PEER3_SN` / `$PEER4_SN` (linkserver, `--device=MCXA153:FRDM-MCXA153` / `MCXA156:FRDM-MCXA156` from board.cmake); udev `/dev/hil/peer3`, `/dev/hil/peer4` | | static guard per board [proposed]; R-10 checks the MAC and Device instance |
| P1-F767 | `openocd` from the SDK hosttools (v2.1 row) | | IDCODE, RDP and MAC (v2.1) |
| nRF probe (It2) | `nrfutil` (default runner of nrf54l15dk and nrf54lm20dk; needs SEGGER J-Link) `--dev-id <J-Link SN>`; `--recover` once per new DK; udev `/dev/hil/probe0` | | VDD ≥ 3.0 V check in firmware before arming |
| P3 | unchanged | | option-byte read only |

`/etc/hil/bench1/map.yml` (P1):

```yaml
- connected: true
  id: "<full DUT MCU-Link serial>"
  platform: frdm_mcxn947/mcxn947/cpu0
  product: "MCU-Link"          # required by hwmap-schema.yaml; not used for linkserver
  runner: linkserver
  serial: /dev/hil/dut0
  baud: 115200
  pre_script: /opt/hil/bin/pre-flash.sh
  fixtures: ["hil_bench:/etc/hil/bench1/bench.yml", hil-dut-mcxn947]
```

The 4.4.2 schema (`scripts/schemas/twister/hwmap-schema.yaml`) requires `connected`, `id`, `platform`, `product` and `runner`, and `hardwaremap.py` validates the map against it. The Twister alt configs (`gen.py`) use `-p frdm_mcxn947/mcxn947/cpu0`. `hil.bacnet.instrumented` requires only `[hil]` on P1.

### 8.5 Twister integration (It1 W3; D28, D31)

Changes for v2.2 (the rest of §8.5 is unchanged):
- BACnet `hil.bacnet.instrumented`: `required_snippets: [hil]` on P1; `[hil, hil-io]` only in the P1-F767 alt configs.
- Build command:
  ```
  west twister -T $FW/firmware --alt-config-root /opt/hil/out/$RUN/alt/bacnet -p frdm_mcxn947/mcxn947/cpu0 --tag hil \
    --device-testing --hardware-map /etc/hil/bench1/map.yml --build-only --allow-installed-plugin \
    -O /opt/hil/out/$RUN/bacnet
  ```
  `$FW` is the BACnet branch tip until main has the storage shrink (D24).
- MQTT uses the same `-p`.

### 8.6 Pinned tools

Replace the OpenOCD row with these rows:

| Tool | Version |
|---|---|
| LinkServer | pinned version, installed in W1 and recorded in bench.yml. CI never runs `LinkServer probe … update`; probe firmware is updated only by hand, never with J21 fitted on the bench. |
| MCU-Link firmware | factory CMSIS-DAP; version recorded per probe (DUT, stimulus, peers) in bench.yml |
| pyocd | 0.45.1 (installed and run this session) with NXP.MCXN947_DFP 26.06.00 (installed). NXP.MCXN236_DFP is added in W2; whether `--target=mcxn236` from board.cmake matches its part names is [unverified: W2]. Pack files are stored under `/opt/hil/packs`. |
| nrfutil + nrfutil-device, SEGGER J-Link | pinned when the It2 probe is set up; nrfutil is the default runner of `nrf54l15dk` and `nrf54lm20dk` (`boards/common/nrfutil.board.cmake`) |
| uhubctl | distro version recorded in bench.yml |
| OpenOCD | SDK hosttools (0.12.0 + Zephyr patches), P1-F767 only |

Board targets used: `frdm_mcxn947/mcxn947/cpu0`, `frdm_mcxn236`, `frdm_mcxa153`, `frdm_mcxa156`, `nrf54l15dk/nrf54l15/cpuapp`, `nrf54lm20dk/nrf54lm20b/cpuapp`.

---

## 9. Test pyramid, tiers and catalogue index

v2.2 changes:
- Pyramid, first line: "HIL: real PHY, MCU-rail (TPS22918) and USB-port power cuts, NRST, IO loop, RS-485 electrical, Clause 9 timing, flash/NOR persistence, OTA swaps".
- Tiers table, instrumented row: "`-S hil` (+ `hil-io` on P1-F767)".
- Catalogue index, changed rows:

| Area | It1 (23 HIL tests) | It2 | It3 |
|---|---|---|---|
| Rig | R-01, R-02a, R-02b, R-04, R-06, R-07 | R-05, R-08, R-09, R-10, R-11 | – |
| MS/TP | – | Rig side, not gated (D47): MSTP-01, MSTP-22a. DUT side, gated on FW-07: MSTP-03..08, 11, 22b, 23, 24. | MSTP-02, 09, 10, 12..21, 25..28 |

The other index rows and the "15 of the 23 It1 HIL tests are release tier" line are unchanged.

### 9.1 Criteria deltas for P1 = FRDM-MCXN947 (v2.2; apply to test-catalogue.md)

This is a new subsection after §9. General rules:
- In the Profiles column, **P1 now means the FRDM-MCXN947**. The v2.1 criteria written for the F767 apply to P1-F767. P2 entries are dropped, because P2 is retired.
- Counts: IT2 75 → 78 (R-10 moves in from IT3, R-11 is new, MSTP-22 splits into 22a/22b). IT3 41 → 40.

| Test | Change |
|---|---|
| R-01 | `board=frdm_mcxn236`, `profile=P1`; `chans` per D49. Link criteria unchanged. |
| R-02a | Replaces the v2.1 criteria.<br>- With the DUT powered: `stim dac ai0..ai2 450` and `1350` mV, read back by `stim adc` on the same node within ±(10 mV + 0.5 %). An absent MCP4728 (ERR -19) is a rig fault.<br>- `stim din nrst_sense` = 0 with the DUT running (ribbon and Q3 present).<br>- `stim lat sync 1 lb 1 10` succeeds (loopback).<br>- Each stimulus-driven LA signal (**di2**, sync) toggles on its bench.yml channel and nowhere else.<br>- The FX2 60 s capture has the expected sample count.<br>- Any failure is a rig fault. |
| R-02b | Replaces the v2.1 criteria.<br>- Every wired di (di0, di2, di3; di1 = SW3/ISP is excluded): `stim dout 0/z` gives the expected SMP `uc_io read` value at both levels.<br>- Every do (do0-do4): SMP `uc_io force 0/1` gives the matching `stim din` after hilrig applies the catalog polarity (do0-do2 are ACTIVE_LOW), then release.<br>- ai0-ai2: `stim dac 450/1350` mV read by `uc_io read` within ±50 mV; 2500 mV reads full scale (1800 mV). No dividers.<br>- Each line changes only its own channel. |
| SYS-01 | Flash through the D42 wrapper (`linkserver --probe <full serial>`) or the Twister `dut` fixture; exits 0 within 120 s. The bench MAC is AE:9A:22 + CRC-24 (D43). The other criteria are unchanged. |
| R-07 | D42/D43: full probe serial present exactly once, static image guard, CMPA hash (daily), and the post-flash DHCPDISCOVER MAC. It replaces the IDCODE/RDP checks, which stay for P1-F767. |
| RST-01 | `nrst` low for 10 ms, then release within the W1/W2-measured window (4-9 ms expected, including the Q3 offset) ± 1 ms. rstmon +1. Link drop recorded, not asserted, until W1 tells whether RESET_B resets the PHY. I-Am/CONNACK within 20 s. |
| RST-03 (It2) | WWDT0 (the MQTT task_wdt; BACnet after FW-05). If W1 shows no RESET_B pulse on an internal reset, the criterion becomes `HIL-BOOT reset` = WATCHDOG within [0.5, 1.6] × the nominal timeout. |
| RST-04 | After `hil panic` the DUT is back within 20 s plus the watchdog timeout, 5/5. rstmon +1 is asserted only if W1 showed a RESET_B pulse on internal resets. Otherwise the criterion is `HIL-BOOT reset` = SOFTWARE or WATCHDOG (the CMC hwinfo driver maps WWDT0 to RESET_WATCHDOG). BACnet xfail(strict) until FW-05. |
| MQTT-03 | `led on` / `led off` give `stim din do0` = **0 / 1** (wire level: the red LED is `GPIO_ACTIVE_LOW` in the board DTS), 20/20 within 1 s. The other criteria are unchanged. |
| PWR-01 | On `mcu_rail`: `v3v3` < 300 mV within 200 ms and for the rest of the off time; `v3v3_brd` ≥ 3.1 V throughout; no udev remove event for the DUT MCU-Link; instrumented images report `HIL-BOOT reset` = POR; back within 20 s. On `usb_port` (before W1 passes, or if it fails): the MCU-Link re-enumerates (expected), and there is no `v3v3_brd` or udev criterion. |
| PWR-02 | Replaces the v2.1 criteria.<br>- **Part 1.** With the DUT off after `stim safe` (`mcu_rail`, or `usb_port` if W1 failed): `v3v3` ≤ 300 mV; `stim dac ai0 1000` → ERR -1 (also refused on the sensed rail).<br>- **Part 2.** First the stimulus is held halted for 60 s (`pyocd reset --halt -t mcxn236 -u $STIM_SN`, target verified in W2; LinkServer gdbserver as fallback). Then its hub port is off for 60 s. Throughout:<br>  - the DUT keeps answering (ping 0 % loss plus BACnet RP or MQTT telemetry continuity);<br>  - its uptime never goes back;<br>  - the LA `nrst` channel stays high;<br>  - `v3v3_brd` ≥ 3.1 V before and after (replaces v5);<br>  - DUT DI/BI states are recorded, not asserted (the clamped stimulus pins read as active).<br>- **Commissioning only** (DMM, bench.yml): V_ON > 2.5 V released and < 0.3 V cut; RESET_B ≥ 3.0 V with the stimulus idle and with it unpowered. |
| PWR-03 (It2) | INA226 between TPS22918 VOUT and J24 pin B: MCU-rail current signature. The E5V 500 mA limit does not apply; the baseline is re-measured. |
| IO-04 | Uses **di2** (D2, no capacitor). di0 (A5, 0.1 µF, 4-9 ms release) is not used for debounce timing. |
| IO-05, IO-15, IO-16, ROB-05 (It2) | **di1 → di2** on P1 (di1 is SW3/ISPMODE_N, never wired). The BI or MSI under test is the object that rig_config binds to di2. IO-15 keeps do3. |
| IO-07 (It2) | Points 0.05/0.45/0.90/1.35/1.75 V; 2.0/3.0 V read full scale; provisional band ±(0.5 % FS + 4 LSB) until FW-14. |
| IO-11 (It2) | `stim pwmcap` on FlexPWM1 SM0/SM2 capture (106.7 ns ticks, ±2 ticks); the ao pins stay in capture mode after `stim safe`. Criteria unchanged. |
| PERS-01 | Power method per D39. |
| PERS-02 (It2) | `usb_port` only; the NOR stays powered on `mcu_rail`. Host-timed (D51): the cut is issued at a random delay after the FW-16 marker edge; the real loss is time-stamped on the LA (`pers` profile, `p3v3_brd`); results are binned by the measured offset. "The marker trigger (FW-16) is unchanged" now means only that the marker defines the reference edge. |
| PERS-03 (It2) | On P1 the W25Q64 is driven by the FlexSPI NOR driver, not `spi_nor`. Whether it waits for WIP at init after a reset mid-erase is [unverified: PERS-03 measures it]. The other criteria are unchanged. |
| OTA-01 (It2) | Slot 1 is cleared with SMP `image erase` at session start (D25); `west flash --erase` is forbidden on P1. |
| OTA-04 (It2) | `mcu_rail` with stimulus-timed random offsets once W1 passes (the MCUboot slots are in the internal flash, D51); before that, host-timed `usb_port` as PERS-02. |
| SEC-01 | BACnet release = unmodified build of the BACnet tip (no whitelist entry). The D38 build checks and the D44 partition check run in the build job (D44 warns on main until the merge). |
| SEC-03 (It2) | FW-19 is met: DCC/Reinit without a configured password → Error security/password-failure; with the rig's test password → success. The "default password filister" case is gone. |
| ROB-05 (It2) | CreateObject/DeleteObject are off by default → Reject unrecognized-service (expected pass); the object is the one bound to di2. |
| MSTP-01 (It2, rig, not gated) | `stim adc rs485_a/rs485_b` on two MCXN236 ADC0 inputs chosen in It2 (taps: 100k 0.1 % → midpoint with 100k 0.1 % + 100 nF to GND → 10 k). Criteria unchanged. |
| MSTP-04 (It2) | Software DE: DE rises before the start bit, with the lead distribution recorded. The "16/16 bit" expectation is removed. |
| MSTP-05 (It2) | 0 ≤ Tpostdrive ≤ 15 bit at 9600, 38400, 76800 and 115200 baud. The hardware-vs-GPIO comparison and the U1 selector are dropped. |
| MSTP-15, MSTP-16, MSTP-28 (It2/It3) | The Pico becomes the FRDM-MCXA153 (MAC 3) and FRDM-MCXA156 (MAC 4) peers; MSTP-28's MS/TP device is one of them. |
| **MSTP-22a (new split, It2, rig, not gated)** | DE stays low and there are 0 garbage octets while any node, including the DUT (DE pad DIS with its 10 k pull-down until FW-07 code drives it), is in reset, booting or unpowered. With the FTDI hub port off, the bus idle level stays ≥ 200 mV and frames between the other nodes stay valid. |
| **MSTP-22b (It2, release, FW-07)** | The DUT rejoins the ring within 3 s after boot. |
| R-10 (It3 → It2) | Characterises the MCXA153/MCXA156 peers (was the Pico), including their MAC (3/4) and Device instance (260203/260204). |
| **R-11 (new, It2, rig)** | The nRF54L15 probe and the DSLogic record the same 1000 DE/TX edges. Their differences after the SYNC fit lie within ±(62.5 ns + 2 LA samples). Only then is the probe a timing source (D48). |

---

## 10. CI (trigger model for this repo)

Replace the first facts bullet with:
- **`main` exists at e62a095** (the BACnet integration commit). Its `ci.yml` runs on push to main, `pull_request` and `workflow_dispatch`, on hosted runners. The **default branch is still** `claude/inter-session-communication-h989ye`, which has no `hil.yml` (`git ls-remote --symref origin HEAD`, 07:19 UTC). main shares no history with the HIL branch (D45).

Model item 1 now reads: `hil.yml` lives on the HIL branch and on `hil/nightly` until D45's integration PR merges into main. From then on **main is the single source**. HIL work continues on branches cut from main (`claude/hil-*`), the old HIL branch is frozen (its README points to main), and the nightly script runs `git checkout -B hil/nightly origin/main`.

Model item 2 (triggers), v2.2:
- `push`: `branches: [claude/hardware-in-loop-testing-x74tww, hil/nightly, main, "claude/hil-**"]`, with `paths` gaining `firmware/boards/**` (partition and pin changes on main). Every ref except `hil/nightly` runs the build job only.
- On main, check_catalog's D44 rule is a warning (job annotation) until main contains the storage shrink. After that it is an error there too, as it already is on every other ref. The BACnet session is told.
- `workflow_dispatch` becomes usable once `hil.yml` is on the default branch. That needs D45's integration branch to be merged into main **and** the user to switch the default branch to main. Then switch the nightly to `on: schedule` and keep the `hil/nightly` push as the fallback.
- A `pull_request` build job for same-repo PRs into main is optional: `if: github.event.pull_request.head.repo.full_name == github.repository`. It is never a required check.
- The build job runs on the self-hosted host. While the host is offline, jobs from main pushes wait in the queue; GitHub fails a job that no self-hosted runner picks up within 24 h [likely: GitHub docs not re-read for v2.2]. Because the job is never a required check, main is never blocked by it.

Item 6 (W3 verification): replace "`openocd … adapter serial`" with "`hil/tools/hil-flash --probe <DUT serial>` (linkserver) and pyocd with the pinned pack".

Replace the `on:` block of the workflow sketch with:

```yaml
on:
  push:
    branches: [claude/hardware-in-loop-testing-x74tww, hil/nightly, main, "claude/hil-**"]
    paths: [hil/**, apps/hil_stimulus/**, lib/hil/**, snippets/**, firmware/boards/**, .github/workflows/hil.yml, .hil-run.env]
  pull_request:          # optional; build job only, same-repo PRs
    branches: [main]
    paths: [hil/**, apps/hil_stimulus/**, lib/hil/**, snippets/**, firmware/boards/**, .github/workflows/hil.yml]
  workflow_dispatch: {inputs: {bacnet_ref: {}, mqtt_ref: {}, select: {default: "not destructive"}, cycles: {default: "20"}}}
```

The build job gains `if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository`. The hil job's `if:` is unchanged.

---

## 11. Bring-up plan

**Iteration 1** (Fri Sep 25 – Sun Oct 18): 3 weeks for one person, including one slack week. **No boards are bought** (D46).

It1 shopping list: buy from EU-stock distributors (2-3 days). Confirm the owned tools on day 0 (DMM, soldering station, bench PSU, oscilloscope; the FX2 can stand in for the scope in W1).

| # | Item | Qty | ≈ € |
|---|---|---|---|
| 1 | FX2 CY7C68013A 8-ch LA clone | 1 | 8-15 |
| 2 | MCP4728 4-ch 12-bit I2C DAC module (VDD 3.3 V from J6-7) | 1 (+1) | 6-12 |
| 3 | TI TPS22918DBV on SOT-23-6 adapters | 2 | 4-8 |
| 4 | BSS138P (2N7002 acceptable) on SOT-23 adapters: Q1 PWR, Q2 NRST, Q3 RESET_B sense, 3 spares | 6 | 5 |
| 5 | Passives:<br>- resistors: 2.2 kΩ ×40 (or 8-way SIP networks), 10 kΩ ×20, 100 kΩ ×20, 4.7 kΩ ×10, 1 kΩ ×10, 100 Ω ×5, 22 Ω ×2;<br>- capacitors: 100 nF ×20, 1 nF ×4, 1 µF ×4 | kit | 10-15 |
| 6 | Component-free Arduino-R3 proto shields with stacking headers (no power LED, reset button or ICSP; 5V and VIN pins clipped) | 2 | 10-16 |
| 7 | 2×20 IDC ribbon (20-30 cm) and 2 box headers; DuPont F-F/F-M ×40; single-row pins; a keyed 2-pin housing for the J24 leads | set | 8-12 |
| 8 | Powered USB hub (7+ ports) on the uhubctl list | 1 | 40-60 |
| 9 | USB-C data cables to match the hub (DUT J17, stimulus, spare), plus the FX2's cable | 3 (+1) | 8-15 |
| 10 | Cat6 patch cables | 2-3 | 6 |
| 11 | USB3 GbE NIC RTL8153 (only if the host has one NIC) | 0-1 | 0-15 |
| 12 | x86 N100 mini-PC, 16 GB, 2× i226 (0 if an existing Linux PC is reused) | 0-1 | 180-250 |

Total without the host: about €105-180. With the host: about €285-430.

Dropped from the v2.1 It1 list: 2× NUCLEO-F767ZI, the W25Q128 breakout, the Pololu 2810, the keyed morpho housing and pins, and the 5 V PSU (it moves to It2 for the RS-485 bias).

| Week | Work | Exit |
|---|---|---|
| **W0** (Sep 25-27) | - Order the It1 parts.<br>- Rerun `survey.sh` against the live tips and main just before committing.<br>- Commit v2.2:<br>  - `docs/HIL.md` and `docs/hil/*`;<br>  - the `snippets/hil` MCXN947 overlay and the F767 planned-UART overlay;<br>  - the `apps/hil_stimulus` port with the review patch (frdm_mcxn236 overlay/conf, `stim_cmds.c`, the `drive-high` binding property);<br>  - `hil/tools/mcxn_flash_guard.py`;<br>  - `check_catalog` rules D38/D44 (D44 warning mode for main);<br>  - `bench.yml.example` for P1. | Everything builds on the host SDK: P1 release and instrumented images of both apps (BACnet at its tip), and the stimulus. Survey output recorded. |
| **W1** (Sep 28 – Oct 4) | - Host: Ubuntu, SDK 1.0.1, LinkServer (pinned) + pyocd with its pack, udev by MCU-Link serial with `ID_MM_DEVICE_IGNORE`, dumpcap capabilities, docker mosquitto 2.1.2, bacnet-stack tools, `install-net-wrappers.sh`.<br>- DUT commissioning (J17 power only, J24 shunt still fitted):<br>  - probe serial and MCU-Link firmware version;<br>  - instrumented flash (`hil-flash --probe`) → `HIL-BOOT uid=` → MAC (D43) → DHCPDISCOVER confirmation;<br>  - CMPA read-only hash.<br>- W1 bench checks, in order:<br>  1. D0/D1 contention (DUT held in reset with SW1);<br>  2. J24 pin identity by continuity (board unpowered), then back-feed (22 Ω shunt; pass < 3 mA);<br>  3. RESET_B idle level, release time, and driver type;<br>  4. UART loopback D1→D0 at 38400/76800/115200.<br>- `console` fixture; network-only tests. | R-04, R-06, R-07, SYS-01, MQTT-01..03 and TLS-03 (TLS 1.3 halves), TLS-01, NET-03, BIP-01, BIP-03, SEC-01 green (or xfail citing an FW id). Back-feed verdict recorded (`mcu_rail` or `usb_port`). |
| **W2** (Oct 5-11) | - Stimulus preparation: remove R153-R156; clip the shields' 5V and VIN stacking pins; wire the MCP4728 VDD from J6-7.<br>- Flash the MCXN236 (`hil-flash --probe`), then R-01.<br>- Stimulus bench checks:<br>  - pwmcap twice per ao channel;<br>  - OCTRL level at 0/100 %;<br>  - `adc v3v3_brd` against the DMM;<br>  - D3 (P3_12) low level < 0.8 V with the DUT driving low (else isolate U12);<br>  - MCP4728 absent → ERR -19;<br>  - `stim dout di0 1` → ERR -1;<br>  - pyocd `mcxn236` target with its pack.<br>- Build both shields and the ribbon (pin tables §6): TPS22918 at J24 (if W1 passed), Q1/Q2/Q3, senses, MCP4728, FX2.<br>- Commissioning measurements into bench.yml: V_ON levels, RESET_B ≥ 3.0 V with the stimulus idle and unpowered, the Q3 release offset, VDD_ANA correction.<br>- hilrig follow-ups (§5.4, plus `mcxn947_mac()`, `hil-flash`, the P1 `chans`, `HIL_BUILD_DIR` in the conftest); svc second address and `point_broker` (D33); fixtures `hil_session`, `smp`, `rig_config` (with MQTT factory reset), `dut_power` (both methods), `tls_server`, `tls_front`; the S-03 wrapper. | R-01, R-02a, R-02b, RST-01, RST-04, PWR-01, PWR-02, IO-01, IO-04, PERS-01 green (RST-04 BACnet xfail until FW-05); TLS 1.2 halves of MQTT-01 and TLS-03 green |
| **W3** (Oct 12-18) | - CI: runner registration as user `hil`, `install-runner.sh`, `hil.yml` (with the v2.2 triggers of §10), the `nightly` script and timer with its fallback, Twister alt configs for `frdm_mcxn947/mcxn947/cpu0` + linkserver, `map.yml` with `product`, flock, job hook; the §10 item 6 checks.<br>- D45: cut `claude/hil-into-main` from main with the HIL-owned paths and open the PR.<br>- **Slack for rig debugging.**<br>- Order the It2 parts (below). | 3 consecutive green nightly runs of the 23 It1 tests |

**Iteration 2** (about 5 weeks, from Oct 19). Order:
- DSLogic Plus, only after the seller confirms USB 2a0e:0020; otherwise the Saleae;
- **MS/TP parts, no longer gated (D47), about €65-90:** 6 × 3.3 V RS-485 transceivers (THVD1450 class; U1, U2, U3, two peers, spare), the bus kit, a 5 V PSU for the bias, and the FTDI USB-RS485-WE (host node MAC 7, mstpcap, R-08);
- 3 USB cables for the two peers (USB-C) and the nRF DK;
- INA226;
- ADS1115 (optional reference);
- managed switch and a second NIC.

The FRDM-MCXN947, the second MCP4728, the second TPS22918 and the R3 shield are no longer It2 items: they are owned, not needed, or bought in It1.

| Work item (in order) | Tests / deliverables |
|---|---|
| DSLogic (PID check, firmware extraction, `sigrok-cli --scan`), SYNC fit, stimulus latency | R-05, R-09, TIM-04..06, IO-03, IO-05 |
| Analog accuracy (stimulus LPADC, optional ADS1115; FW-14) | IO-07, IO-09, IO-11 (`pwmcap`), PWR-03 |
| Power loss and NRST during writes (D51) | PERS-02 (host-timed `usb_port`), PERS-03, OTA-01..05 (MCUboot scenarios; slot 1 cleared by SMP `image erase`; OTA-04 on `mcu_rail` after W1) |
| Management and security | SMP-01..03, SEC-02 (BACnet and MQTT SMP), SEC-03, CFG-01/02 |
| WASM apps (clang-18, wamrc 2.4.5) | APP-01..03; the test app also provides MSO/MSV for BIP-13a/b |
| Remaining It2 MQTT, TLS, network, I/O, reset and BACnet tests | see §9: RST-03, PWR-05, MQTT-04..07/09/10, TLS-02/05/06, NET-01/02/05/06/07, BIP-02/05/06/07/10/11/13a/13b, FD-01..04, IO-15/16, ROB-01/05 |
| MS/TP rig side on owned peers, not gated (D47): injector, sniffer, peer images (MAC 3/4), optional nRF probe | R-08, R-10, R-11, MSTP-01, MSTP-22a; dry runs of the implemented MSTP test code against the host node, peers and injector |
| Software-DE spike (D47, optional, after the BACnet session agrees) | DE timing report for roadmap 3.1 |
| DUT MS/TP product tests (gated on BACnet Phase 3 / FW-07) | MSTP-03..08, 11, 22b, 23, 24 |

**Iteration 3** (on demand):
- Saleae (unless already bought in It2), PPK2, programmable PSU, relay fault box;
- optional stimulus-timed VBUS switch for PERS-02 (D51) [proposed];
- KiCad interposer, after the board set is frozen;
- a **spare FRDM-MCXN947** for soak and destructive OTA tests (replaces the spare F767);
- a commercial MS/TP device;
- P3; P1-F767 only if needed;
- labgrid for a second bench.

---

## 12. Risks

Rows changed in v2.2:

| # | Risk | Mitigation |
|---|---|---|
| 1 | BACnet default build does not link | **Retired**: FW-04 met at e62a095 (MCXN947 RAM at 91 % with `-S hil`, 4ee098e). Keep watching the margin: about 34 KiB is free. |
| 2 | DUT HSE comes from the ST-LINK MCO | P1-F767 only |
| 3 | Several identical probes (DUT, stimulus and peer MCU-Links); linkserver ignores `-i` and defaults to `--probe #1`; `--probe` matches serial substrings | The `hil-flash` wrapper requires `--probe <full serial>` and refuses a missing `--probe`, `#N` and `-i`; the serial must appear exactly once in `LinkServer probes`; udev by serial; R-07 checks the post-flash MAC |
| 4, 6, 7 | Morpho pins, ST-LINK reaction to E5V, E5V 500 mA | P1-F767 only |
| 8 | Back-feed; MCXN not 5 V tolerant (±3 mA injection) | 2.2 kΩ on every line (D40), 10 kΩ on senses, di 0/z enforced in firmware, `safe` before off, analog refused while off (commanded or sensed), U1 on the switched rail, 5V/VIN stacking pins clipped, MCP4728 on 3.3 V, PWR-02; plus risks 36 and 48 |
| 13 | MS/TP UART choice diverges from the BACnet roadmap | **Resolved** by the user decision (D37). The remaining risk is software DE (risk 47). |
| 20 | Schedules and dispatch need the workflow on the default branch | main exists but is not the default; D45 integration branch; nightly by push until the switch |
| 27 | MCXN analog configuration (VREF standby, 48 MHz ADC clock at power-level 0) | Now P1: FW-14; the first IO-07 run decides; provisional accuracy band |
| 34 | MS/TP bench effort with no DUT MS/TP code | D47: the rig side uses owned peers and costs about €65-90 including the FTDI; product tests stay gated |

New risks:

| # | Risk | Mitigation |
|---|---|---|
| 35 | D0/D1 may be driven by an MCU-Link UART path (UM12018 Table 17 note), contending with U1 RO | W1 test (DUT in reset, 10 k to GND reads < 0.1 V); if it fails, remove the path or move MS/TP (BACnet session decision) |
| 36 | J24 cut back-fed by Y3 (P1_4), the PHY RMII outputs and MDIO pull-up, the VCOM TX, SWD and reset lines, the TJA1057 RXD/TXD (P1_11/P1_10), the PTN5150A and the LEDs, so no clean POR and possible injection above 3 mA | W1 22 Ω shunt measurement with a **3 mA aggregate** pass limit; `usb_port` fallback; R154 never populated; `HIL-BOOT reset` = POR assert in PWR-01 |
| 37 | FC2 leaves combined mode (lpi2c2 disabled, or I2C/SPI/DAC enabled), so MS/TP or markers move or break | D38 build checks; FW-01; W1 loopback |
| 38 | main and the HIL/MQTT branches have unrelated histories, so PRs cannot merge | D45 integration branch with HIL-owned paths only, then main as the single source; survey prints merge-bases |
| 39 | `pwm_mcux` capture defects on the stimulus (ENOTSUP for TYPE_BOTH, stale EBUSY, INIT/VAL1, RUN) | app workarounds (§5.2); W2 double-capture check; fallback CTIMER capture (It2) |
| 40 | MCXN236 D10-D13 tied to the MCU-Link USB-SPI bridge (R153-R156), whose SCK, data and PCS0 are MCU-Link outputs on m1, m0 and do1 | remove R153-R156 before the W2 wiring; record it in bench.yml |
| 41 | nRF54L DKs default to VDD 1.8 V; a probe input at 3.3 V would be over-voltage. DTR-triggered HWFC detection can make the debugger drive P1.07 (bus_ro). | Set 3.3 V in Board Configurator and check it with a DMM before any cable; disconnect the UART1 RTS/CTS group and verify P1.07 Hi-Z with the terminal open; 4.7 kΩ inputs; the firmware refuses to arm below 3.0 V |
| 42 | Only one MCXN947: destructive, OTA and soak tests risk the only DUT; a mass erase might touch CMPA or the NOR | CMPA hash daily; `--erase` forbidden on P1; OTA destructive tests after a spare is bought (It3) |
| 43 | Slow RESET_B release (0.1 µF, 4-9 ms), an unknown MCU-Link reset driver, and the Q3 threshold offset | boot timing from the sensed edge plus the measured offset; 100 Ω drain resistor; W1/W2 measurement; RST-01 window from W1/W2 |
| 44 | W25Q64 shared between apps: overlap on main (e62a095) until the BACnet storage change is merged | FW-23; the D44 `check_catalog` rule (a warning on main until the merge); the rig builds the BACnet tip until main has it |
| 45 | MQTT stored settings override the rig's site Kconfig | SMP `mqtt/factory_reset` at session start (D25) |
| 46 | A probe firmware update during CI changes the probe behaviour | pin LinkServer; record the MCU-Link firmware; CI never runs `LinkServer probe … update`; update only by hand, never with J21 on the bench |
| 47 | Software DE may fail Tpostdrive or Tturnaround at 76800/115200 while BACnet/IP and apps run | This is a product finding the rig reports (MSTP-04/05), not a rig fault; the D47 spike gives an early answer; roadmap 3.2 (cpu1) is the product's mitigation |
| 48 | Stimulus unpowered, halted or hung with the DUT on: clamped stimulus pins load DUT lines, and RESET_B could sit in the undefined band | Q3 gate-sense (D50); FET gate pull-downs; ≤ 1.3 mA per line; PWR-02 asserts reachability, uptime and a steady LA `nrst`; W2 DMM check of RESET_B with the stimulus unpowered |
| 49 | Host-timed `usb_port` cuts (PERS-02) have millisecond jitter plus P5V0/LDO hold-up, so power-loss coverage is statistical | LA time stamp of the real loss (`pers` profile); offsets binned; OTA-04 stays stimulus-timed on `mcu_rail`; stimulus-timed VBUS switch as an It3 option |
| 50 | **The repository is public (2026-09-25).** A fork pull request can bring a workflow that targets the bench runner's labels, and the bench jobs run as root. GitHub recommends self-hosted runners only for private repositories. | Require approval for all external contributors' fork workflows; hil.yml stays push-only; unique runner labels; an ephemeral runner, or a private mirror for the bench (hil/host/README-host.md §7). Hosted CI minutes are free for public repositories, which removes the minutes argument of D27. |

---

## Appendix A: DUT HIL overlay for nucleo_f767zi (snippets; edtlib-validated with warnings as errors against the v4.4.2 bindings, for BACnet + hil + hil-io and for mqtt_tls + hil; the pin scan found no conflicts)

v2.2: the snippet now carries two board overlays. Zephyr 4.4 matches snippet board keys against the full `BOARD/BOARD_QUALIFIERS` target.

`snippets/hil/snippet.yml` (the `hil-io` file has the same shape, with `name: hil-io`, no conf and only the F767 key):

```yaml
name: hil
append:
  EXTRA_CONF_FILE: hil.conf
boards:
  nucleo_f767zi/stm32f767xx:
    append:
      EXTRA_DTC_OVERLAY_FILE: boards/nucleo_f767zi.overlay
  frdm_mcxn947/mcxn947/cpu0:
    append:
      EXTRA_DTC_OVERLAY_FILE: boards/frdm_mcxn947_mcxn947_cpu0.overlay
```

`snippets/hil/boards/frdm_mcxn947_mcxn947_cpu0.overlay` (P1, delivered separately, verbatim):
- lpi2c2 stays okay;
- lpuart2 at 38400 with the alias `mstp-uart0`;
- `dac0` and `flexcomm1_lpspi1` disabled;
- `hil-marker-gpios` = P0_26, P0_25, P4_0, P4_1;
- `mstp-de-gpios` = P0_24.

It was built with BACnet 4ee098e (and 6d1a151) and mqtt_tls e859980 (0 warnings). The generated devicetree has `DT_N_ALIAS_mstp_uart0` = `lpuart@94000`, lpuart2 and lpi2c2 okay, and dac0 and lpspi1 disabled.

`snippets/hil/boards/nucleo_f767zi.overlay` (P1-F767, **planned UART**, replaces the v2.1 USART2 hardware-DE overlay). Built with mqtt_tls e859980 + `-S hil` for `nucleo_f767zi`, 0 warnings; alias `mstp-uart0 = &usart6` and `mstp-de-gpios = <&gpiod 15 0>` are generated:

```dts
#include <zephyr/dt-bindings/gpio/gpio.h>
#include <zephyr/dt-bindings/pinctrl/stm32-pinctrl.h>

&pinctrl {
	/* usart6_rx_pg9 of hal_stm32 has no bias; RO is Hi-Z while DE = /RE = 1 */
	hil_usart6_rx_pg9_pu: hil_usart6_rx_pg9_pu {
		pinmux = <STM32_PINMUX('G', 9, AF8)>;
		bias-pull-up;
	};
};

&usart6 {                                   /* arduino_serial: TX PG14 D1 CN10-14, RX PG9 D0 CN10-16 */
	pinctrl-0 = <&usart6_tx_pg14 &hil_usart6_rx_pg9_pu>;
	pinctrl-names = "default";
	current-speed = <38400>;
	status = "okay";
};

/ {
	aliases { mstp-uart0 = &usart6; };
	zephyr,user {
		hil-marker-gpios = <&gpiog 0 GPIO_ACTIVE_HIGH>, <&gpiog 1 GPIO_ACTIVE_HIGH>,
				   <&gpiog 2 GPIO_ACTIVE_HIGH>, <&gpiog 3 GPIO_ACTIVE_HIGH>;  /* one BSRR write */
		mstp-de-gpios = <&gpiod 15 GPIO_ACTIVE_HIGH>;   /* D9 CN7-18, software DE, 10 k pull-down */
	};
};
```

`snippets/hil-io/boards/nucleo_f767zi.overlay` and `snippets/hil/hil.conf`: unchanged. `hil-io` has no MCXN947 key.

Side findings for other sessions (v2.2):
- main (e62a095) does not have the storage shrink yet; its build maps the whole W25Q64. (The BACnet `docs/hardware.md` §4 text was fixed at 4ee098e.)
- The stimulus research's `mstp_node` uses the alias `mstp-uart`; rename it to `mstp-uart0` when it is committed. Its research builds used MAC 10 on both peers and MAX_APDU 1476 on the MCXA156 (§8.3).

## Appendix B: Review findings disposition (v1 reviews: verdict_0 pins, verdict_1 tools, verdict_2 completeness)

| ID | Sev | Finding | Disposition |
|---|---|---|---|
| 0.0 | blocker | Pololu 2810 ON with 100 k pull-up never turns on; fail-safe inverted | **Fixed**: BSS138P inverter with 10 k to +5VA (V_ON ≈ 2.9 V when released), slide switch OFF; TPS22918 variant; commissioning DMM check (§7.2) |
| 0.1 | blocker | `local-mac-address` only octets 3-5 used | **Fixed (obsolete)**: 4.4.2 `net_eth_mac_load` honours the full address, but none is set (B7). MAC is learned from the UID and DHCPDISCOVER (D29). |
| 0.2 | blocker | PD8 VCP tap is an open pin by default | **Fixed**: on MB1137 the bridge is SB7 and the documents conflict. W1 continuity check; `vcp_tx` LA channel only after the check, else `m2`. |
| 1.0 | blocker | Platform baseline stale | **Fixed**: whole design on 4.4.2 / SDK 1.0.1 / F767 |
| 2.0 | blocker | Platform stale; survey by hand | **Fixed**: re-baselined; `survey.sh` (W0); board set confirmation in W0 |
| 0.3 | major | DE fail-safe only on U1; module pull-ups | **Fixed**: rules for every transceiver (§4.1); MSTP-22 includes stimulus/Pico reset and an unpowered FTDI |
| 0.4 | major | J-Link JP1 kills the VCP | **Not applicable**: no J-Link in the plan; MB1137 uses CN4, and the VCP needs its own check if a J-Link is ever added |
| 1.1 | major | Keylog shim TLS 1.2 only | **Fixed**: NSS labels for all Mbed TLS 4.1 export types; SEC-01 greps `KEYLOG `; TLS-1.2 front; MQTT-01 suite list for both versions |
| 1.2 | major | MAC octets / wrong fallback description | **Fixed**: exact recipe (D29); commissioning |
| 1.3 | major | Pololu ON pin (same as 0.0) | **Fixed** (§7.2) |
| 1.4 | major | Kconfig names removed in 4.4.2 | **Fixed**: FW-12/13 use `SNTP`, `NET_CONFIG_CLOCK_SNTP_INIT`, `NET_CONFIG_SNTP_INIT_SERVER_USE_DHCPV4_OPTION`, `NET_DHCPV4_OPTION_NTP_SERVER`, `MBEDTLS_HAVE_TIME_DATE`, `UART_NATIVE_PTY_0_ON_STDINOUT`, `ETH_NATIVE_TAP` (all read in v4.4.2); the build job builds every snippet combination |
| 1.5 | major | Twister `--test-only` / alt-config mismatch; `.config` dropped | **Fixed**: D28, identical `-T`, alt root and `-O` per app, `pytest_root` via `$HIL_TESTS`, checks in the build step (§8.5) |
| 1.6 | major | `-p twister_harness.plugin` double registration | **Fixed**: no `-p`; `--allow-installed-plugin` on Twister calls (D2); hilrig options for local runs (§8.2) |
| 1.7 | major | Pico overclaimed as a conforming master | **Accepted**: functional peer only (It3), R-10 characterisation, injector is the timing reference |
| 1.8 | major | Shell buffers too small for long frames | **Fixed**: chunked `rs485 load` with crc32, explicit shell buffers (implemented in the port) |
| 1.9 | major | sudo `ip netns exec` = root; dumpcap path | **Fixed** in the implementation: root-owned wrappers, `/usr/bin/dumpcap`. Residual risk: root test sessions (R19). |
| 1.10 | major | D1 rationale false | **Fixed**: D1 rests on uhubctl and build speed |
| 1.11 | major | DTLS-SMP use case does not exist | **Fixed, reworded in v2.1**: the keylog shim is only for third-party brokers (no DTLS keylog case). SMP over DTLS exists in 4.4.2 as FW-10's option; the HIL has a conditional DTLS client plan (§3.2); SEC-02 exposure test |
| 2.1 | major | H563 map collides with the F767 NOR and catalog | **Fixed**: catalog-based F767 map (B4), NOR pins excluded; W25Q128 in the It1 BOM; interposer deferred to It3 |
| 2.2 | major | OTA untested | **Fixed**: OTA-01..05 in It2 on the MCUboot release artifact (D24) |
| 2.3 | major | SMP, apps, security untested | **Fixed**: SMP-01..03, SEC-02/03, CFG-01/02 and APP-01..03 in It2; fuzz harnesses left to the firmware sessions (U-layer) |
| 2.4 | major | BBMD tests target the wrong role | **Fixed**: FD-01..04 for the product's foreign-device client; BBMD-* EPICS-gated in It3 |
| 2.5 | major | BACnet coverage thin | **Partly fixed**: BIP-10 Who-Has, 11 RPM, 12 WPM, 13a priority arrays over the commandable types, 13b AV/MSV last-writer-wins, 14 PSS consistency; hand-written EPICS golden (BIP-04). BTL/BIBB mapping is an open question. |
| 2.6 | major | I/O and timing tests measure lib/hil | **Fixed**: I/O criteria from docs/io.md polled scan; TIM-01/02 informational; IO-02/06 and v1 FC-17 dropped |
| 2.7 | major | No release black-box tier | **Fixed**: B12 tiers, D24 allowlist; 15 of the 23 It1 tests are release tier |
| 2.8 | major | No factory reset or known state | **Fixed**: `rig_config` (D25); FW-08 factory reset in It2 |
| 2.9 | major | TLS matrix stale after TLS 1.3 | **Fixed**: TLS-01/03 per version; TLS-05/06/07 added |
| 2.10 | major | MQTT failure modes missing | **Fixed**: MQTT-06 blackhole, 07 refusals, 08 duplicate id, 09 PINGREQ with 120 s publish interval (variant image), 10 retained/empty/identify; TIME-01; `e2e_native_sim.sh` run as-is |
| 2.11 | major | One bench cannot hold the schedule | **Fixed**: soak moved to the spare DUT, 24 h weekends; flock with local priority; push runs never touch the bench; nightly budget (§10) |
| 2.12 | major | CI triggers impossible in this repo | **Fixed in v2.1**: D27 (push to `hil/nightly`, direct fallback), D32 (§10) |
| 2.13 | major | Scope unrealistic | **Fixed**: It1 = 23 tests, protocol v0, contract v0 of 6 items; MS/TP gated (D34), PCB and full tier deferred |
| 0.5-0.17 | minor | PE9 bridges; ADC cells; SNTP Kconfig; DAC range; MCP4728 VDD; bias source; NRST sense bias; divider impedance; relay pins; µs dips; SB34; morpho/E5V sequencing; EXTI13 | **All fixed or N/A on F767**:<br>- PE9 goes straight to CN10-4.<br>- The stimulus overlay builds clean on 4.4.2.<br>- FW-12 carries the SNTP symbols.<br>- DAC range 200-3100 mV is enforced, and safe = EN=0.<br>- MCP4728 VDD = stimulus 3V3, LDAC to GND.<br>- The bias comes from +5VA.<br>- NRST sense has no pull, plus a ≥ 3.0 V check.<br>- 100 nF at each sense pin with 480-cycle sampling.<br>- 9 free stimulus pins are listed for the relays.<br>- Off times ≥ 200 ms.<br>- The SB34 note was H563-only.<br>- W1 bench prep (E5V before USB) plus the 50-cycle E5V check.<br>- `gpio_keys` disabled and `CONFIG_INPUT=n`. |
| 1.12-1.17 | minor | CRC example; Tusage_timeout source; MSTP-19 checksum; TLS 1.1 case; DSLogic buffer; wording | **Fixed / accepted**:<br>- Frames are generated, never hand-written.<br>- Tusage_timeout is labelled "bacnet-stack, edition TBD" and only the DUT's 30 ms is asserted.<br>- MSTP-19 asserts the decoded layers for frame types 32/33.<br>- The TLS 1.1 case is dropped; ClientHello `supported_versions` is asserted instead.<br>- Explicit LA windows (100 MHz × 16, ≤ 0.16 s).<br>- Wording corrected (§6, §8). |
| 2.14-2.22 | minor | Pico reference; host-timed PERS-02; time coverage; speed/duplex; identity per slot; guards per family; BOM gaps; hub duplication; EMC smoke | **Fixed or accepted**:<br>- R-10 characterises the Pico.<br>- PERS-02 is triggered from a marker edge (FW-16).<br>- TIME-01 and BIP-08 UTC-TS in It3 (FW-22).<br>- NET-06 added.<br>- D30 identity per slot.<br>- R-07 guard per family (F7 RDP, MCXN CMPA).<br>- Assumed-owned list; LA on a root port; USB cables.<br>- The BACnet SMP client is reused; HUB-01 in It3.<br>- EMC smoke is **rejected** for now (out of scope; revisit in It3). |

## Appendix C: Sources read this session

Add for v2.2:
- **Zephyr v4.4.2:**
  - `boards/nxp/frdm_mcxn947/*` (including the `frdm_mcxn947.dtsi` partitions and `board.cmake`), `frdm_mcxn236/*`, `frdm_mcxa156/*`, `frdm_mcxa153/*`, `boards/nordic/nrf54l15dk/*` (`board.cmake`: nrfutil default), `nrf54lm20dk/*` (`board.yml`: nrf54lm20a/nrf54lm20b);
  - `drivers/mfd/mfd_nxp_lp_flexcomm.c`, `drivers/serial/uart_mcux_lpuart.c`, `drivers/gpio/gpio_mcux.c` (no `GPIO_SINGLE_ENDED`), `drivers/pwm/pwm_mcux.c`, `drivers/dac/dac_mcp4728.c`, `drivers/watchdog/wdt_mcux_wwdt.c`, `drivers/hwinfo/hwinfo_mcux_{syscon,mcx_cmc}.c`, `drivers/ethernet/eth_nxp_enet_qos/*`, `subsys/crc/crc24_sw.c`;
  - `scripts/west_commands/runners/{linkserver,pyocd,nrfutil}.py`, twisterlib `handlers.py` (probe arguments, `run_custom_script`), `scripts/pylib/pytest-twister-harness/src/twister_harness/device/hardware_adapter.py`, `scripts/schemas/twister/hwmap-schema.yaml`.
- hal_nxp feature and pinctrl headers; hal_nordic 44fd3d4 MDK.
- The manuals listed in the header. In particular:
  - UM12041 Tables 19-26, §2.6 and §3.9;
  - UM12121 J4 pinout;
  - UM12018 §2.4 and the Table 17-19 CAN notes;
  - nRF54L15 DK UG §3.1.2-3.1.3;
  - MCXNP184M150F70 Table 9.
- BACnet e62a095, 6d1a151 and 4ee098e (`docs/hardware.md`, `docs/roadmap.md`, `docs/bacnet.md`, `docs/SESSION_NOTES.md`, `firmware/boards/*mcxn947*`, `firmware/boards/io/frdm_mcxn947_mcxn947_cpu0.dtsi`, `firmware/sample.yaml`, `git diff e62a095 4ee098e -- firmware`).
- MQTT 75e0620 and e859980 (`docs/SESSION_NOTES.md`, `apps/mqtt_tls`).
- 57orta b437af3 (`docs/SESSION_NOTES.md`).
- main e62a095 (`.github/workflows/ci.yml`).
- The research peer app (`scratchpad/v22/stim/mstp_node`: Kconfig, board confs, the build `.config` files).

## Appendix D: v2.0 review findings disposition (pins = P, tools = T, completeness = C)

| # | Sev | Finding | Disposition |
|---|---|---|---|
| P1 | major | "CN11-5..8 (E5V, GND)": CN11-5 = VDD, CN11-7 = BOOT0 | **Fixed**: only CN11-6/8 soldered, keyed housing, W1 DMM check (§7.1, pin tables §1.3/§1.5, BOM, W1) |
| P2 | major | `stim safe` reconfigures the ao capture pins | **Fixed in the port** (rebuilt, 0 warnings): ao excluded from `lines_safe`, `edges`, `lat` (§5.2, protocol §4/§6) |
| P3 | minor | ao shares EXTI 0/12/15 with m0, do4, nrst_sense | **Fixed**: ao never armed (din/pwmcap only); EXTI table corrected |
| P4 | minor | MSTP-01 taps without pins; accuracy | **Fixed**: rs485_a PC2, rs485_b PA6, 100k/100k 0.1 % + 100 nF, same ADC1, n = 256 |
| P5 | minor | second I2C bus unallocated | **Fixed**: I2C2 SCL PF1 CN9-19, SDA PF0 CN9-21 for MCP4728 #2 [proposed, It2 overlay] |
| P6 | minor | stimulus pins only at morpho positions | **Fixed**: PD12 CN10-21, PF4 CN10-11, PA0 CN10-29 |
| P7 | minor | ST-LINK power model incomplete | **Fixed**: §7.3 model, recovery sequence, PWR-03 records VBUS/E5V, budget allowance |
| P8 | minor | PWR-02 reads v5 while the stimulus is in reset | **Fixed**: PWR-02 split (DUT continuity while the stimulus is halted or unpowered; DMM at commissioning) |
| P9 | minor | JTAG pins not floating after reset | **Fixed**: exception in invariant 1 (ao1 PA15); proposed do6 moved from PB4 to PB6 |
| P10 | minor | TPS22918 rise time understated | **Fixed**: tON ≈ 1.95 ms, tR ≈ 2.5 ms |
| P11 | minor | ADS1115 back-feeds the stimulus 3V3 | **Fixed**: 10 kΩ in series with every ADS1115 input on a DUT net, calibrated |
| P12 | minor | transceiver VCC and RO rails unstated; FTDI/Pico | **Fixed**: §4.1 supplies, D6, MSTP-22 FTDI-off case |
| P13 | minor | shield positions change role on P2 | **Fixed**: per-profile source shunts, P2 A5 GPIO path, D0/D1/D14/D15 open (pin tables §6) |
| P14 | minor | relay rest state unspecified | **Fixed**: NC for K1-K3, NO for K4-K7 |
| T1 | blocker | Twister refuses an installed plugin | **Fixed**: `--allow-installed-plugin` on every call (D2, §8.5) |
| T2 | blocker | `workflow_dispatch` needs the file on the default branch | **Fixed**: nightly by push to `hil/nightly` + direct fallback (D27, §10) |
| T3 | major | Twister passes `--twister-config`, conftest never flashes | **Fixed**: D31, `hil_session` |
| T4 | major | scenario timeout 60 s; per-test 300 s | **Fixed**: `timeout: 3600`, per-test marks, `--timeout-multiplier` weekly |
| T5 | major | no per-app `-O` | **Fixed**: D28 |
| T6 | major | job containers cannot use `--network` | **Fixed**: D32 (no job container) |
| T7 | major | new DSLogic Plus revisions unsupported by sigrok | **Fixed**: D10 PID check, Saleae fallback, firmware extraction |
| T8 | major | 400 MHz samples only CH0-3 | **Fixed**: 100 MHz × 16 in ≤ 0.16 s windows for all latency tests |
| T9 | minor | tshark `-c` counts pre-filter packets | **Fixed**: capture filter in the commissioning command |
| T10 | minor | `rx-warning` vs `rx-warnings` | **Fixed**: both documented and accepted |
| T11 | minor | MAC recipe byte order ambiguous | **Fixed**: exact recipe and unit test (D29) |
| T12 | minor | two different hil.conf lists | **Fixed**: one list; HIL_SHELL defaults to y in lib/hil/Kconfig |
| T13 | minor | FW-12 symbols do not start SNTP in mqtt_tls | **Fixed**: FW-12 names both init paths |
| T14 | minor | "≤ 50 ms below the 77.7 ms wrap" is the wrong rule | **Fixed**: protocol §6 invariant 7 |
| T15 | minor | pre_script can collide with the stimulus tty | **Fixed**: pre_script is guard-only; stimulus opened exclusively |
| T16 | minor | Who-Has binary is `bacwh` | **Fixed**: BIP-10, §3.2 |
| C1 | major | build → test hand-off across runners | **Fixed**: `-O /opt/hil/out/<run>/<app>` outside `_work`, job output |
| C2 | major | every push queues a bench run | **Fixed**: push = build only |
| C3 | major | hil job environment undefined | **Fixed**: D32 |
| C4 | major | nightly has no fallback | **Fixed**: direct run-hil fallback, W3 check |
| C5 | major | ~20 indexed IDs without criteria | **Fixed**: all restored or added (MSTP-02/03/05/07/09/13/15/17/18/20/25/27/28, TIM-02, MEM-02, BBMD-02/03, APP-02/03) |
| C6 | major | BIP-13 assumes AV/MSV priority arrays | **Fixed**: BIP-13a/13b |
| C7 | major | criteria mix BACnet and MQTT images | **Fixed**: NET-03, BIP-09, ROB-02, NET-04 parametrised by image |
| C8 | major | R-02 cannot run in MQTT sessions | **Fixed**: R-02a (every session) and R-02b (BACnet, SMP only) |
| C9 | major | distributed-apps half untested | **Fixed**: APP-01 moved to It2, APP-02/03 added, wasm tools pinned |
| C10 | major | OTA artifact undefined | **Fixed**: D24 MCUboot artifact, scenario, allowlist, mass erase |
| C11 | major | W1 exit depends on W2 items; lead times | **Fixed**: console fixture in W1, TLS 1.2 halves in W2, EU-stock purchase, ownership check before ordering |
| C12 | minor | nightly near the 120 min timeout | **Fixed**: 20/50 cycles, budget table, timeout 300 min |
| C13 | minor | TLS-03 and MQTT-09 need other images | **Fixed**: `hil.mqtt.release.mtls`, variant image and mark |
| C14 | minor | SMP over DTLS would break rig_config | **Fixed**: D13 reworded, conditional DTLS client, FW-10 |
| C15 | minor | needs_fw hides missing interfaces | **Fixed**: FW-21, FW-22; IO-14, HUB-01, BIP-08 updated |
| C16 | minor | implemented behaviours without tests | **Fixed**: IO-15, IO-16, SMP-03, ROB-05, SEC-03, NET-07 |
| C17 | minor | RST-03 window ignores LSI tolerance | **Fixed**: [0.5, 1.6] × nominal |
| C18 | minor | MS/TP bench in It2 without product code (CHANGE B11) | **Applied as a gate** (D34), lead to confirm |
| C19 | minor | drift between design and implementation | **Fixed**: counts, S-03 W2, bench.yml.example, R-01, scenario marks, hil.conf |
| C20 | minor | stale shared copies on the HIL branch | **Fixed**: survey.sh fails on drift; W0 resync and SESSION_NOTES replacement |
| C21 | minor | FW-03 over-freezes; FW-06 misses real dependencies | **Fixed**: FW-03 shrunk; FW-06 extended |
| C22 | minor | job hook acts on the bench without the lock | **Fixed**: bench actions moved into run-hil (holds the lock) |
| C23 | minor | PWR-02/MSTP-01 not measurable | **Fixed**: see P4, P8 |
| C24 | minor | how TLS servers take over broker.hil.lan | **Fixed**: D33 |
| C25 | minor | persistence only against power loss | **Fixed**: PERS-03 |
| C26 | minor | cables, ModemManager, BOM total | **Fixed**: BOM row 12, udev `ID_MM_DEVICE_IGNORE`, total €165-245 |

## Appendix E: v2.2 contradictions between the research reports, and their resolution

| # | Topic | Reports | v2.2 resolution |
|---|---|---|---|
| E1 | "Create the main branch" | All three: main already exists at e62a095 | Verified live (`git ls-remote`, 07:19 UTC): main = e62a095; nothing was created. New finding: unrelated histories, so D45. |
| E2 | Where the series resistor sits | DUT report: stimulus end. Stimulus and port reports: DUT end. v2.1: "stim end", with the LA on the DUT side. | One 2.2 kΩ on the DUT shield; its DUT side is the probe point (D40). Electrically the position does not change the injection limit. |
| E3 | AI source resistor | Stimulus report: 1 kΩ. DUT report: 2.2 kΩ. v2.1 D16: 1 kΩ. | 2.2 kΩ + 100 nF (D40, D16). A 3.2 V source into an unpowered pin through 1 kΩ gives 2.9 mA, too close to 3 mA. |
| E4 | MCP4728 gain | Stimulus report and v2.1 pin tables: 2.048 V × 1. Port overlay: × 2. | × 2 (D41), as built; VDD 3.3 V from J6-7 caps the output |
| E5 | Truth ADC | DUT report: ADS1115. Port: stimulus LPADC. | Stimulus LPADC in It1; ADS1115 optional in It2 (D41) |
| E6 | FC2 mode for MS/TP | v2.1 §4.2 and profiles research: disable lpi2c2 for hardware DE. DUT report: keep lpi2c2 okay. | Keep it okay (D37, D38); read in `mfd_nxp_lp_flexcomm.c` |
| E7 | MCXN markers | Profiles research: SCL/SDA/D0/D1, with FC2 disabled. DUT report: D12/D13/SDA/SCL. | DUT report (D38); D0/D1 carry MS/TP |
| E8 | di0 release time | v2.1 pin tables: τ ≈ 0.2 ms. DUT report: 4-9 ms release through the internal pull-up. | DUT report; IO-04 moves to di2 (§9.1) |
| E9 | Peer MACs | Stimulus report: 10/11 (research builds: 10 on both). D17: Max_Master 10. | 3/4 (D17), set in the peer board confs (§8.3) |
| E10 | `mstp_rx` pull on the stimulus | Port overlay: pull-down on every marker | No pull on `mstp_rx`; rebuilt at the same size |
| E11 | Pin for the rs485 taps | Stimulus report: J2-5 P0_27. Port: J2-5 = `v3v3_brd`. | `v3v3_brd` keeps J2-5; the taps are chosen in It2 |
| E12 | U1 supply | v2.1: DUT 3V3. DUT report: rig 3V3, not J3-8. | P3V3_MCU at J24 pin B (D6) |
| E13 | W25Q64 overlap | DUT report: overlap, ask BACnet | Fixed on the BACnet branch at 6d1a151 (docs at 4ee098e; read this session) but not on main; FW-23, D44 |
| E14 | pyocd pack | v2.1 D3: NXP.MCXN947_DFP 17.0.0. DUT report: 26.06.00 installed and working. | Pin 26.06.00 (D42) |
| E15 | "0 live conflicts" | DUT report | The scan script prints 3 same-pin pairs: board `gpio-leds`/`gpio-keys` against do0, do1 and di0. No LED or INPUT driver is built, so there is no live conflict. D38 asserts that CONFIG_LED and CONFIG_INPUT stay off. |
| E16 | `--erase` | DUT report: never. v2.1 D25: mass erase before OTA sessions. | Never on P1 (D25, D42): SMP `image erase`; the linkserver erase path skips the board's flash overrides (v2.2 tools review) |
| E17 | MS/TP gate | v2.1 D34: gated on FW-07 acceptance | D47 |
| E18 | Profile names | Task and port: P1 = MCXN947 | D35 (P1, P1-F767, P2 retired) |
| E19 | BACnet and MQTT tips | Reports: e62a095 / 75e0620 | Live tips 4ee098e (firmware = 6d1a151 except one comment) / e859980; the BACnet builds were redone at 4ee098e |

## Appendix F: v2.2 review findings disposition (pins = P, tools = T)

This is a new appendix after Appendix E. All 28 findings were accepted. Three were partly rejected (see the open questions, "REJECTED").

| # | Finding | Disposition | Where |
|---|---|---|---|
| P1 | An unpowered stimulus loads RESET_B through 10 kΩ into the undefined band; DUT outputs back-power the stimulus | Fixed: Q3 gate-sense (D50). "Stimulus unpowered, DUT on" is documented as a tolerated state. New PWR-02 criteria. | D50, §5.3, §7.2, §7.3, §9.1, pin tables §1.3/§2/§6, stimulus overlay, risk 48 |
| P2 | The 10 mA back-feed criterion does not protect the 3 mA per-pin limit; the source list was incomplete | Fixed: < 3 mA aggregate (66 mV across 22 Ω); TJA1057, MDIO, PTN5150A, SWD/reset added | D39, §7.3, pin tables §1.6, risk 36 |
| P3 | MCP4728 on mikroBUS J5, whose only supply pin is 5 V | Fixed: VDD from J6-7; pull-ups to 3.3 V; J5-7 in the keep-out | D41, §2, §5.1, pin tables §2/§6, BOM, stimulus overlay |
| P4 | The R154 rework would disable Y3 and the Ethernet | Fixed: R154 never populated; `usb_port` fallback | D39, §7.1, §7.3, pin tables §1.5, risk 36 |
| P5 | J24 identity by voltage is ambiguous; a reversed switch loads P3V3 through QOD | Fixed: identity by continuity; keyed leads | D39, §7.1, pin tables §1.3/§1.6, BOM 7 |
| P6 | The MCXN236 USB-SPI bridge drives m0, m1 and do1 | Fixed: R153-R156 removed before the W2 wiring | §5.1, §11 W2, pin tables §2, risk 40, stimulus overlay |
| P7 | The DI 0/z invariant was not enforced; the analog gate missed `usb_port` cuts | Fixed in firmware (review patch, built) | D49, §5.2, §5.3, §7.3 |
| P8 | DTR-triggered HWFC detection drives nRF P1.07 (bus_ro) | Fixed: disconnect the UART1 RTS/CTS group; verify Hi-Z | §6, pin tables §4, risk 41 |
| P9 | Stacking headers pass 5V/VIN; the FTDI brings a 5 V wire | Fixed: clipped pins, component-free shields, FTDI wires insulated | D39, §4.1, §7.1, pin tables, BOM |
| P10 | ao2 (P3_12) is shared with U12 IO3 on the stimulus | Fixed: W2 low-level check; isolate U12 if it fails | §5.1, §11 W2, pin tables §2, stimulus overlay |
| P11 | MCXA156 A4/A5 contradiction; missing inner-row duplicates | Fixed | §5.1, pin tables §2/§2.1, stimulus overlay |
| P12 | Tap capacitor wording; U1 VCC undefined without the switch | Fixed | D6, §4.1, pin tables §1.3/§2 |
| T1 | map.yml lacks the required `product` | Fixed | §8.4 |
| T2 | pre_script gets no build dir | Fixed: `HIL_BUILD_DIR` from the conftest, fail closed; `hil_session` guard | D42, §8.2, §8.4 |
| T3 | Six It1 catalogue rows still F767 | Fixed: §9.1 rows | §9.1 |
| T4 | PERS-02/OTA-04 marker trigger does not work with `usb_port` | Fixed: D51 (PERS-02 host-timed; OTA-04 stays stimulus-timed on `mcu_rail`, partly rejected) | D51, §6, §7.3, §9.1, §11, risk 49 |
| T5 | The listed release artifact (main) fails D44; the main trigger | Fixed: baseline = BACnet tip, rebuilt at 4ee098e; D44 warning on main; `firmware/boards/**` paths | header, D24, D44, D45, §8.3, §10 |
| T6 | Peer images have MAC 10 and MAX_APDU 1476 | Fixed: board confs MAC 3/4, APDU 480; R-10 checks | D17, §8.3, §9.1 |
| T7 | Unreplaced F767/Pico sections | Fixed: replacement texts | §3.1, §4.3, §4.5, §5.3, §5.4, §8.1, §8.2, §8.5, §9, §10 |
| T8 | Default `--probe #1` and substring match | Fixed: wrapper requires the full serial, present exactly once | D42, §8.4, risk 3 |
| T9 | `--erase` skips the board overrides | Fixed: forbidden on P1 | D25, D42, §7.3, E16, risk 42 |
| T10 | §8.6 lists only OpenOCD; "auto-update disabled" is not a setting | Fixed | §8.6, D42, risk 46 |
| T11 | DAC refusal uses the commanded state; 1 ms settle | Fixed in firmware | §5.2, §7.3 |
| T12 | It2/It3 catalogue rows still F767/Pico/di1 | Fixed: §9.1 rows, counts, MSTP-22 split | §9, §9.1 |
| T13 | The It2 table lost the remaining-tests row; the MS/TP row mixed in DUT tests | Fixed | §11 |
| T14 | Branch survey stale | Fixed: survey at 07:19 UTC; builds at 4ee098e; stale side finding dropped | header, §0.1, Appendix A, FW-23, session notes |
| T15 | No single source after D45; the notes said pushes trigger nothing | Fixed | D45, §10, session notes |
| T16 | Cost excludes the FTDI; peers missing from flashing and BOM | Fixed | D47, §8.4, §11, BOM, risk 34 |

