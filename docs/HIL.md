# BACNet-uc hardware-in-the-loop (HIL) test rig: design v2

Design v2.1, 2026-09-25. Owner: HIL session, branch `claude/hardware-in-loop-testing-x74tww`.

Companion documents in `docs/hil/`: [pin tables](hil/pin-tables.md), [stimulus protocol](hil/stimulus-protocol.md), [test catalogue](hil/test-catalogue.md), [firmware contract](hil/firmware-contract.md), [bill of materials](hil/bom.md).

History:
- v1 (2026-09-24) targeted NUCLEO-H563ZI on Zephyr 3.7.2. The same evening both firmware branches moved to Zephyr 4.4.2 and the user's boards (STM32F767ZI, MCXN947), so v2 re-baselines the whole rig. Nothing below depends on v1's platform.
- v2.1 (2026-09-25 ~03:00 UTC) applies the three adversarial reviews of v2.0 (pins and electrical, tools and standards, completeness). Appendix D lists every finding and where it was fixed.

**Provenance.** Every fact here was read this session in a primary source, unless it is tagged:
- **[likely]**: inferred from sources, not read directly;
- **[unverified]**: still to be measured. The W-step that measures it is named.

Primary sources used:
- Zephyr v4.4.2 (dccb0959), hal_stm32 fc11896d, hal_nxp 7554bc0f, SDK 1.0.1;
- UM1974 Rev 8 and the MB1137 B-01 schematic, DS11532 Rev 8, UM12018 Rev 2.0;
- TI, Nexperia, Microchip and Pololu datasheets;
- Mbed TLS 4.1.0, mosquitto 2.1.2, libsigrok 0.5.2, DSView `dsl.h`, and the GitHub Actions docs;
- the branches themselves.

Builds made this session with SDK 1.0.1 against v4.4.2:
- the stimulus firmware with picolibc: 91,708 B flash, 42,624 B RAM, 0 warnings;
- the BACnet F767 release with the DTCM workaround: 494,444 B flash, 291,708 B RAM;
- mqtt_tls F767 with the HIL site configuration and mTLS: 247,416 B flash, 142,228 B RAM.

IDs (D-, FW-, R-, SYS-, …) are the same in this document, the pin tables, the BOM, the test catalogue, the firmware contract and the stimulus protocol.

---

## 0. Context

### 0.1 What each session is doing (origin, 2026-09-25 ~01:50 UTC)

| Branch @head | Session | State | Consequence for the rig |
|---|---|---|---|
| `claude/zephyr-bacnet-stm32-162k1g` @2161be7 (00:52). Board and config files unchanged since d238d24 (23:03). | BACnet | **Firmware (`firmware/`, Zephyr 4.4.2, SDK 1.0.1)**<br>- BACnet/IP B-ASC device with AI/AO/AV/BI/BO/BV/MSI/MSO/MSV. AV and MSV have no Priority_Array (docs/bacnet.md §3.1).<br>- `uc,io-channels` IO catalog, polled scan: `sample_ms` default 100, debounce default 20; edge → Present_Value ≤ sample + debounce + 5 ms (docs/io.md).<br>- LittleFS `/lfs`: on F767 it lives on an external W25Q128JV on SPI1 (PA5/PA6/PB5/PD14). `/lfs/log` is written continuously (LOG_BACKEND_FS).<br>- MCUmgr SMP over UDP 1337, unauthenticated, with the shell, fs and os groups plus custom groups 64-66; also SMP over the console shell.<br>- WAMR apps with BACnet client services and per-app permissions; MCUboot sysbuild scenarios; `NET_TCP=n` (no MQTT in this image); console `uc` shell.<br>- **F767 and MCXN default builds do not link** (RAM overflow of 29,564 B and 60,648 B). The DTCM/SRAMX app-pool workaround links.<br>- No watchdog and no fatal-handler override.<br>- MS/TP is roadmap Phase 3 (no RS-485 port code): USART6 on D0/D1 with GPIO DE on PD15.<br>**Harness (`harness/`)**: MCP server and CLI with SMP and BACnet clients. | - Canonical BACnet payload.<br>- Contract items FW-04 (link) and FW-05 (fatal).<br>- The rig restores a known state over SMP.<br>- MS/TP UART proposal FW-07; the MS/TP bench is gated on it (D34). |
| `claude/inter-session-communication-h989ye` @d9cae9e (23:51). This is the repo **default branch**; it has no `.github/`. | MQTT | **`apps/mqtt_tls` fw 0.3.0 on 4.4.2**<br>- MQTT 3.1.1 over TLS 1.3 (preferred) or 1.2, Mbed TLS 4.1.<br>- Suites: TLS1_3 AES-128/256-GCM and ECDHE-ECDSA/RSA AES-GCM; curves X25519, P-256, P-384.<br>- SNI on; `MBEDTLS_HAVE_TIME_DATE` off, so certificate dates are not checked.<br>- task_wdt on the IWDG.<br>- Client id `z` + base32(UID); retained `info` with fw/hwid/mac/caps; JSON commands with id echo.<br>- F767 image with the HIL site config and mTLS: 247,416 B flash / 142,228 B RAM.<br>- No shell, no BACnet. Not yet run on hardware. | - First networked payload.<br>- The release tier is possible without changing any firmware. |
| `claude/ai-harness-building-control-05nrgm` @c45c1e0 (00:56) | Hub | `hub/` (uc_hub gateway: SMP client, MQTT driver, MCP) and `web/`.<br>Requests B1-B8 (B1 identify, B2 force lease, B6 catalog paging), none answered yet. | The rig takes the catalog from the DUT build's devicetree, never from SMP. FW-21 carries B1/B2. |
| `claude/tests-docs-relevance-future-lpfqzn` @ace8910 | Experiment | spec-vs-code study | none |
| `claude/hardware-in-loop-testing-x74tww` @fd8d0f4 (01:40) | HIL | - The `hil/` package is pushed: `hilrig` plus unit, rig, SIL and MS/TP tests.<br>- Not on the branch yet: `apps/hil_stimulus` (scratchpad port, with the v2.1 ao fix), `lib/hil`, `snippets/`, `hil/twister`, `hil/site`, `hil/tools`.<br>- **Stale copies**: `docs/SESSION_NOTES.md` is the old MQTT 3.7.2 notes; `modules/wasm-micro-runtime` lags BACnet 2161be7 by 19 lines (WAMR_OS_THREAD_STACKS removed there). | W0 replaces SESSION_NOTES with the HIL section, resyncs `modules/`, and commits the missing trees. |

W0 adds `hil/tools/survey.sh`. It prints each branch head, the Zephyr revision from its west.yml and its `platform_allow`. It also diffs `west.yml`, `zephyr/module.yml` and `modules/` of the HIL branch against the BACnet tip and exits non-zero on any difference. Its output replaces this table before every design revision. Every build uses a workspace whose manifest is the firmware checkout under test (§8.3), never the HIL repo's copy.

### 0.2 Decision log (v1 D-numbers kept where they survive)

| # | Decision | v2 status | Why |
|---|---|---|---|
| D1 | HIL host: x86 N100 mini-PC, 2× i226, 16 GB, Ubuntu 24.04 | kept, **reason changed** | uhubctl per-port power (Pi 5 ports are ganged) and build speed. Logic 2 now ships official ARM64, and the ci image is multi-arch, so neither is a reason (B6). |
| D2 | pytest is the test language. Twister builds, flashes and reports in CI. Local runs use hilrig's own `--hil --bench`. One host venv holds pytest-twister-harness (pip -e from `$ZEPHYR_BASE`), so **every `west twister` call passes `--allow-installed-plugin`**. | **changed** | B8. No `-p twister_harness.plugin` when the plugin is pip-installed (pluggy reports a double registration). Without the flag, 4.4.2 Twister exits 1 at start-up when the plugin is installed (environment.py:1024-1029). |
| D3 | Flashing per profile:<br>- P1: `openocd` from the SDK 1.0.1 host tools, probe chosen with `--cmd-pre-init "adapter serial <SN>"`;<br>- P2: `linkserver --probe`, with pyocd plus pack NXP.MCXN947_DFP 17.0.0 as fallback;<br>- P3: stm32cubeprogrammer or pyocd. | **changed** | Runner sets are those in the 4.4.2 board.cmake files. `west flash --serial` does not choose the probe on nucleo_f767zi, because its openocd.cfg ignores `_ZEPHYR_BOARD_SERIAL`. |
| D4 | Stimulus: a second NUCLEO-F767ZI running `apps/hil_stimulus` (4.4.2). Pico 2 is only a functional MS/TP peer (It3). | **changed** | B3 confirmed (§5). The Pico port shares the bacnet-stack defects the rig hunts. |
| D5 | DUT I/O is the BACnet `uc,io-channels` catalog. HIL extras come from snippets `hil` (markers PG0-PG3, USART2 MS/TP) and `hil-io` (di3, di4, do5, do6, BACnet only). | **changed** (B4, refined) | `&uc_io` does not exist in mqtt_tls. Appended channels keep ids 0-16. Release images carry only the base catalog. |
| D6 | RS-485: DE and /RE tied. A 10 k pull-down on **every** DE net and a pull-up on every RO feeding an MCU, to that transceiver's own VCC. The sniffer is hard-wired listen-only. U2 and U3 run from the stimulus 3V3. | kept, extended | The fail-safe must hold for every transceiver while its MCU is in reset. No RO may drive more than 3.3 V into a stimulus pin or the LA. |
| D7 | DE lead and lag = 16/16 bit on the DUT (snippet), 8/16 on the injector | kept | 1 bit is far below Tpostdrive (15 bit). |
| D8 | Bias 510 Ω / 510 Ω from the **always-on +5VA**, at one point | kept, clarified | 278 mV idle. The bias survives DUT power cuts. |
| D9 | MS/TP timing only from the LA or the stimulus timebase | kept | FTDI latency and host jitter. |
| D10 | LA tiers: FX2 8 ch (It1), DSLogic Plus 16 ch (It2), Saleae Logic Pro 16 (It3). The DSLogic is bought only as a unit that enumerates as **2a0e:0020**; otherwise the Saleae backend (already in `hilrig.la`) moves to It2. | kept (B9), **refined** | libsigrok 0.5.2 and master know only PID 0x0020. The 2022 hardware revisions 0x0030/0x0034 (PGL12 FPGA) are DSView-only (DSView `dsl.h`). Each DSLogic capture declares its rate and length (256 Mbit buffer). |
| D11 | Capture on the DUT NIC inside netns `lan-a`, offloads off | kept | Implemented in `hil/net/up.sh`. |
| D12 | Subnet A 192.0.2.0/24 (DUT .10), subnet B 198.51.100.0/24 | kept (B7) | |
| D13 | TLS decryption:<br>- default: broker keylog (mosquitto 2.1.2 container);<br>- version pins: `openssl s_server -tls1_2/-tls1_3 -keylogfile` and a Python TLS-1.2 front;<br>- firmware shim (NSS labels for 1.2 and 1.3): optional, It2, third-party brokers only. | **changed** (B10) | TLS 1.3 is the DUT default. There is no DTLS *keylog* use case. SMP over DTLS does exist in 4.4.2 and is one of FW-10's options; if it is chosen, the HIL adds a DTLS SMP client (§3.2). |
| D14 | A committed TEST-ONLY CA and DUT client cert; per-run leaves from `mkpki.sh` | kept | Implemented in `hil/pki`. |
| D15 | P1 power: board-level cut at **E5V (JP3 1-2)** through a fail-safe load switch.<br>- It1: Pololu 2810 driven by an inverter (BSS138P) with 10 k to +5VA.<br>- It2: TPS22918 (quick output discharge) with the same inverter.<br>- Stimulus pin high = DUT off; released, reset or unpowered = DUT on. | **changed** | Fixes the v1 blocker. The ST-LINK LDO is diode-ORed from U5V, E5V and VIN (D5/D3/D4), so the VCP and the MCO survive an E5V cut. The E5V cut also powers down the PHY and the NOR, so PERS and OTA tests see a true power loss. |
| D16 | AI node: 1 kΩ + 100 nF at the DUT pin | kept | |
| D17 | MS/TP MAC plan: DUT 5, host node 7, injector spoofs any MAC, slave tests 130, Max_Master 10 | kept (Pico MAC 3 in It3) | |
| D18 | USART2 on the F767 Zio: CN9-4 = PD6 RX, CN9-6 = PD5 TX, CN9-8 = PD4 DE | **changed** (F767) | UM1974 Table 19. |
| D19 | IO description: the catalog only. `/zephyr,user` holds only `hil-marker-gpios`. | **changed** | No dual pin map. |
| D20 | Capture API: `tshark -T fields` after stop, plus a sentinel barrier | kept | Implemented in `hilrig.capture`. |
| D21 | Everything host-side lives under `hil/` | kept | Implemented. |
| D22 | Stimulus channels keep **DUT catalog names** (B5). Each DUT profile has its own stimulus channel overlay, and `stim info profile=` is checked against bench.yml. Levels on DUT channels are physical wire levels; hilrig applies catalog polarity. | **new** (rejects the position-naming proposal) | Tests read like the product (`dout di1` ↔ BI bound to di1). A DUT swap costs a 10 s stimulus reflash, which the session start automates. Position names would add a devicetree-parsing layer to every call. |
| D23 | MS/TP on the F767 DUT: the HIL proposes USART2 with hardware DE (FW-07). U1 has a 3-jumper selector, so the BACnet roadmap choice (USART6 D0/D1 + GPIO DE PD15) can be wired too. | **new** | The F7 USART6 DE pins (PG8/PG12) are morpho-only. GPIO DE timing depends on ISR latency, which MSTP-05 measures either way. |
| D24 | Release artifacts = each app built from its own sources plus a **site-configuration allowlist**:<br>- BACnet plain: only the app-pool workaround until FW-04;<br>- BACnet MCUboot (It2, OTA only): the firmware's own sysbuild configuration (`overlay-mcuboot.conf`, as scenario `bacnet_uc.firmware.mcuboot.f767`) plus the workaround; the OTA candidate differs only in a `CONFIG_UC_FW_VERSION` suffix (`-hilota`);<br>- MQTT plain: `APP_MQTT_BROKER_HOSTNAME`, `APP_MQTT_TLS_CA_CERT_FILE`;<br>- MQTT mTLS: plus `APP_MQTT_TLS_CLIENT_AUTH`, `_CERT_FILE`, `_KEY_FILE`.<br>**Variant** images add exactly one documented test symbol (MQTT-09: `APP_MQTT_PUBLISH_INTERVAL_SEC=120`); SEC-01 reports them separately.<br>The plain images are the release artifacts for every test except OTA-*, until the BACnet session says which image ships. | **new**, extended | B12 tier (a) needs our broker and CA, and nothing else may differ. OTA needs MCUboot; TLS-03 needs a client-auth image. |
| D25 | Known state: a session fixture `rig_config` pushes golden `device.json`, `io.json` and `apps.json` over SMP, reloads, verifies SHA-256 and reboots when `reboot_required`. OTA sessions start with `west flash --erase` (openocd `stm32f2x mass_erase 0`, boards/common/openocd-stm32.board.cmake). | **new** | Flashing never erases the F767 NOR, and openocd erases only the sectors it writes, so stale slot1/scratch images survive plain ↔ MCUboot switches. There is no factory reset yet (FW-08). |
| D26 | `lib/hil` is a separate Zephyr module (`-DZEPHYR_EXTRA_MODULES=<hil>/lib/hil`). Snippets come from `-DSNIPPET_ROOT=<hil checkout>`. | **new** | Instrumented images need **no edits** in the firmware branches. SNIPPET_ROOT is read with `zephyr_get` and deduplicated (cmake/modules/snippets.cmake). |
| D27 | CI runs only on the self-hosted host:<br>- push to the HIL branch: build, unit, SEC-01 and SIL only, never the bench;<br>- **nightly by push**: the host timer force-pushes the HIL tip plus one run commit to the machine branch `hil/nightly`, whose push runs build + hil;<br>- `workflow_dispatch` stays in the file for when hil.yml reaches the default branch;<br>- fallback: the timer runs `/opt/hil/bin/run-hil` directly when no Actions run starts.<br>Zero hosted minutes. | **changed** (CHANGE B13) | `schedule` runs only on the default branch, and `workflow_dispatch` triggers only if the workflow file exists on the default branch (GitHub docs). The default branch is the MQTT branch, which never carries hil.yml. `push` runs the workflow file of the pushed ref. |
| D28 | One Twister invocation per app, each with its own `--alt-config-root` **and its own `-O /opt/hil/out/<run>/<app>`**, identical for `--build-only` and `--test-only` | **new**, extended | The alt path is `relpath(suite, -T root)`, so a shared alt root would give both apps the same scenario file. A shared outdir is renamed to `twister-out.1` by the second build (twister_main.py:102-111), and `--test-only` then loads the wrong twister.json. |
| D29 | MAC is learned, never forced (B7):<br>- P1/P3: `02:80:E1:c0:c1:c2`, where c0..c2 are the low 3 bytes (little-endian) of `crc32_ieee` over the 12-byte UID as hwinfo returns it: be32(word @0x1FF0F428) ‖ be32(@0x1FF0F424) ‖ be32(@0x1FF0F420);<br>- P2: AE:9A:22 + CRC-24 (UID).<br>Computed from the UID read over SWD at commissioning, confirmed from the first DHCPDISCOVER. `hilrig.bench.stm32_mac()` implements it with a unit test. | **new**, made exact | eth_stm32_hal_common.c:139-156 (`memcpy(&mac[3], &crc, 3)`) and hwinfo_stm32.c (word2 first, each big-endian). A naive CRC over the `mdw` dump gives another MAC and would fail R-07 on every session. |
| D30 | BACnet instance and names per bench slot are written by `rig_config` from bench.yml. The MQTT client id stays UID-derived. | **new** | A spare DUT or a second profile never collides. |
| D31 | **Twister mode.** hilrig detects Twister by `config.getoption("twister_config")` (4.4.2 passes `--twister-config=<yaml>`, not `--twister-harness`). Twister mode implies `--hil`; the bench comes from the reserved DUT's `hil_bench:<path>` fixture; the session fixture runs the guard and then requests `dut`, whose flash is the only flash; a run with 0 executed tests fails. | **new** | Otherwise the conftest takes the pyserial path, never requests `dut` (the only thing that flashes), skips every `hil_only` test, and Twister reports the all-skipped suite as SKIP with exit 0 (harness.py:576). |
| D32 | The `hil` and `build` jobs run **directly on the host, without a job container**, as runner user `hil`. The only sudoers entry is the root-owned `/opt/hil/bin/run-hil`, installed by `hil/host/install-runner.sh`. | **new** | Job containers do not support `--network` (workflow syntax docs), so a container cannot see the DUT NIC, the host netns, the host dockerd's mosquitto, USB probes or the bench lock. R19 (root on the bench) is accepted. |
| D33 | **TLS endpoint takeover.** netns svc holds 192.0.2.1 (mosquitto) and 192.0.2.2 (TlsServer, TLS-1.2 front, malicious and refusing brokers). A fixture points `broker.hil.lan` at .2 through a dnsmasq `--addn-hosts` file plus SIGHUP, and back at teardown. | **new** | The DUT connects only to `broker.hil.lan:8883`, which it resolves before every attempt (APP_MQTT_BROKER_HOSTNAME help). |
| D34 | **MS/TP bench gate (proposed CHANGE B11).** The It2 MS/TP hardware (FTDI, THVD1450, bus kit, taps) is ordered and built only once the BACnet session accepts FW-07 and starts Phase 3. Until then It2 keeps the offline `hilrig.mstp` tests and spends the time on APP, OTA and SMP; if the gate is still closed at the end of It2, the bench moves to It3. | **new**, lead to confirm | The product has no RS-485 port code, so every DUT MS/TP test is blocked; before the gate opens the bench would only test the rig. |

Baseline changes proposed by the v2 research and reviews:

| Proposal | Verdict |
|---|---|
| B4 split into `hil` and `hil-io` | accepted |
| B5 physical-level clarification | accepted |
| B3 probe-selection addendum | accepted |
| Morpho soldering and JP3 = E5V from It1 | accepted (only CN11-6/8, 21, 23 and CN12-10, 28; never CN11-5/7) |
| B5 channel naming by harness position | rejected (D22) |
| F767 markers on D0/D1/D14/D15 | rejected: PG0-PG3 need no board node disabled |
| MCXN markers | decided in It2 |
| `pwmcap` in It1 | the firmware may advertise it, but no It1 test depends on it |
| B8 refinement: `--allow-installed-plugin` on every Twister call | accepted (D2) |
| B9 refinement: DSLogic only as PID 0x0020, else Saleae in It2 | accepted (D10) |
| CHANGE B13: nightly by push to `hil/nightly`, not schedule/dispatch | applied, forced by the GitHub rules (D27); lead to confirm |
| CHANGE B11: MS/TP bench after FW-07 acceptance | applied as a gate (D34); lead to confirm |

---

## 1. Goals and scope

Goal: every product behaviour that can only be seen on hardware has a test with a numeric pass criterion. The tests run on the shipped artifact wherever that is possible.

| Area | What "tested" means | Instruments | Iteration |
|---|---|---|---|
| Rig self-tests | stimulus link, wiring and LA map (stimulus side every session, DUT side over SMP), capture pipeline, carrier drop, flash guard and identity, SYNC fit, MS/TP capture | stimulus, LA, tshark, OpenOCD, SMP | It1 (R-01/02a/02b/04/06/07), It2 |
| System, reset, power | boot to network, NRST, fatal recovery, power cycle, back-feed, current signature, brown-out | stimulus, LA, INA226, PPK2 | It1, It2, It3 |
| BACnet/IP | Who-Is/I-Am, RP/RPM/WP, priority arrays (commandable types) and last-writer-wins (AV/MSV), COV, errors, Who-Has, FD registration, DCC/Reinit exposure, EPICS | bacnet-stack tools, bacpypes3, tshark | It1 → It3 |
| MQTT/TLS | boot to online, cadence, commands, LWT, backoff, blackhole, refusals; TLS 1.2 and 1.3 negatives; mTLS; property matrix | mosquitto 2.1.2 with keylog, s_server, TLS-1.2 front, PKI | It1 → It3 |
| I/O | BO write → pin, BI edge → PV, debounce, COV, Out_Of_Service, di as MSI, SMP force, AI accuracy, AO PWM | stimulus (DAC/ADC/capture), MCP4728/ADS1115, SMP | It1, It2 |
| Persistence, OTA | config survives power cycles; power cut and reset during write; power cut during upload or swap; signed-image rules | load switch, NRST, SMP (MCUboot) | It1 (PERS-01), It2 |
| Management, security, apps | SMP command coverage, malformed CBOR, force/release, exposure scans (SMP, DCC/Reinit), config reload; WASM app lifecycle, BACnet client services, autostart and kv persistence | smpclient 7.3.0, BACnet harness client, wasm toolchain | It2 |
| MS/TP (Clause 9) | electrical, every timer, token ring, 13 fault classes | RS-485 bench bus, LA, injector | It2 (gated, D34), It3 |
| Robustness, timing, memory | malformed input, storms, impairment, soak; boot times; stack and heap | netns, netem, LA, SYNC | It2, It3 |

Profiles:

| Profile | Board | Role | Scope |
|---|---|---|---|
| **P1** | `nucleo_f767zi` | canonical DUT | every area |
| **P2** | `frdm_mcxn947/mcxn947/cpu0` | second DUT | It2: network tests, then an Arduino-header I/O subset |
| **P3** | `nucleo_h563zi` | optional | MQTT only |

The two apps are separate images: BACnet criteria run on the BACnet image and MQTT criteria on mqtt_tls. Tests that apply to both are parametrised by image with separate criteria.

Out of scope: 0-10 V and 4-20 mA front ends, EMC/ESD compliance, BTL certification (the BIBB mapping is an open question), IPv6 and Wi-Fi.

---

## 2. Rig block diagram (P1)

```
+=========================== HIL HOST  x86 N100, Ubuntu 24.04, GitHub self-hosted runner (user hil) ================+
| pytest + hilrig | Twister 4.4.2 | west + SDK 1.0.1 (arm) + OpenOCD from SDK hosttools | tshark/dumpcap | sigrok-cli  |
| netns lan-a[br-a] svc(.1,.2) sim1 sim2 rtr lan-b[br-b] fd bbmdb | dnsmasq | mosquitto 2.1.2 (docker, --tls-keylog)    |
|   eth0 = uplink         eth1 = DUT NIC (moved into lan-a, offloads off)        USB3 -> uhubctl hub | LA on a root port |
+======|=====================|===============================================================|========================+
       |                     | Cat6 direct                                                   | hub ports:
   LAN/Internet              |                                                               |  P1 DUT ST-LINK CN1 (never switched in tests)
                             |                                                               |  P2 STIM ST-LINK   P3 FTDI RS-485 (It2, gated)
+----------------------------+-------------------------------+
| DUT  NUCLEO-F767ZI (MB1137)          RJ45 / LAN8742A (PHY  |       +5VA 5 V PSU (always on) --+--> RS-485 bias (It2)
|  JP3=1-2 E5V, JP1 OFF, CN4 ON, JP5/JP6/JP7 ON  nRST=NRST)  |                                  |
|  USART3 PD8/PD9 = ST-LINK VCP console (115200)             |<-- E5V CN11-6 (GND CN11-8) <-[It2 INA226]<- load switch
|  catalog di0-2 do0-4 ai0-5 ao0-2 (Zio + morpho pins)       |        (It1 Pololu 2810, It2 TPS22918) ON <- BSS138P <- STIM PE5
|  snippet hil: m0-m3 PG0-3, USART2 PD5/PD6/PD4 DE (It2)     |
|  snippet hil-io: di3 di4 do5 do6 PE10/12/14/15 (It2)       |
|  W25Q128JV NOR on SPI1 CN7-10/12/13/16 (/lfs)  NRST CN8-5  |===== USART2 -> U1 THVD1450 == RS-485 bench bus (It2, §4)
+-------------|--------------------------------|-------------+
              | harness: 1 kOhm at the stim end of every line, AI 100 nF at the DUT pin,
              | NRST: BSS138P + 100 R (drive) and 10 k (sense); 3V3 and E5V senses 100k/100k + 100 nF
+-------------+--------------------------------|-------------+        +---------------------------------------+
| STIM  NUCLEO-F767ZI   apps/hil_stimulus (4.4.2, shell 'stim') |       | Logic analyzer, DUT side of the R      |
|  di0-2 out (0/1/z)      do0-4, m0-3 in (EXTI, 64-bit cycles)  |------>|  It1 FX2 8 ch 24 MS/s (sigrok)         |
|  DAC PA4/PA5 -> ai0/ai3; ADC sense on A0-A5, v3v3, v5         | SYNC  |  It2 DSLogic Plus 16 ch (PID 0x0020)   |
|  TIM5/TIM2/TIM4 capture <- ao0/ao1/ao2 (TIM AF, never touched)| PE6   |  It3 Saleae Logic Pro 16               |
|  PE4 -> NRST FET   PE15 <- NRST sense   PE5 -> power FET      |       +---------------------------------------+
|  It2: USART2 HW-DE -> U2 injector; I2C1: MCP4728#1, ADS1115x2, INA226; I2C2: MCP4728#2; rs485 taps PC2/PA6 |
+----------------------------------------------------------------+
```

Ownership rules:
- Only the stimulus drives DUT pins.
- Each board's ST-LINK is the only USB path to that board.
- The LA only listens, always on the DUT side of the series resistors.
- The host never bridges `lan-a` to the uplink.

---

## 3. Network, capture and TLS

### 3.1 Topology (implemented in `hil/net/up.sh`, validated with bacserv stand-ins)

```
 subnet A 192.0.2.0/24 (br-a in netns lan-a)                     subnet B 198.51.100.0/24 (br-b in lan-b)
  DUT .10  = real NIC (--hil) | TAP zeth (--sil native_sim) | netns dut (bacserv stand-in)
  svc .1   dnsmasq, mosquitto 8883 TLS + 127.0.0.1:1883 observer, chrony, client tools
  svc .2   TlsServer, TLS-1.2 front, malicious/refusing brokers on 8883 (D33; up.sh adds the address)
  sim1 .11, sim2 .12  simulated devices (bacserv / bacpypes3)
  rtr .254 ---------------- ip_forward, plain router ---------------- rtr .254
                                                                      fd .10    foreign-device client
                                                                      bbmdb .2  peer BBMD
 capture: dumpcap in lan-a on the DUT NIC (offloads off) = wire view; netem only on veths, never on the DUT NIC
```

- `up.sh` is idempotent. It refuses to move the NIC that carries the default route.
- Root-owned wrappers `/usr/local/sbin/hil-net-up` and `hil-net-down` are installed by `host/install-net-wrappers.sh`. sudoers never allows `ip netns exec` or scripts from a work tree.
- Test sessions run as root, because `setns` needs CAP_SYS_ADMIN. Locally that is `sudo -E pytest`. In CI the hil job runs on the host (no job container) and calls the root-owned entrypoint `/opt/hil/bin/run-hil` through its single sudoers entry (D32, risk R19).
- **DUT identity (D29).** At commissioning (W1):
  1. Read the UID, IDCODE and RDP with OpenOCD (`mdw 0x1ff0f420 3`, `mdw 0xe0042000`, `mdw 0x40023c14`).
  2. Compute the MAC with `hilrig.bench.stm32_mac(w0, w1, w2)`: `bytes = be32(w2) + be32(w1) + be32(w0)`, `crc = zlib.crc32(bytes)`, `mac = 02:80:E1:{crc & 0xff}:{(crc >> 8) & 0xff}:{(crc >> 16) & 0xff}`. Compute the MQTT client id.
  3. Confirm the MAC from the first DHCPDISCOVER. The capture filter makes `-c` count only DHCP client frames: `tshark -i <nic> -f 'udp src port 68 and udp dst port 67' -Y 'dhcp.option.dhcp == 1' -T fields -e eth.src -c 1`. (tshark 4.2.2 counts every packet read for `-c`, not only those that pass `-Y`.)
  4. Write both into bench.yml.
- dnsmasq reserves 192.0.2.10 on that MAC. It serves `broker.hil.lan → 192.0.2.1` (switchable to .2, D33) and NTP option 42. Its lease is 2 min for the lifecycle tests.
- **SIL** uses the same namespaces. BACnet/IP in SIL needs the native_sim **TAP** driver (`CONFIG_ETH_NATIVE_TAP`), because NSOS passes no broadcast (FW-13).

| Capture point | Tier | Use |
|---|---|---|
| C1: DUT NIC in `lan-a` | It1 | one pcapng per test with a narrow BPF (`@pytest.mark.capture(...)`); ring buffer for soak |
| C2: managed switch mirror (TL-SG105E / GS305E) plus a second NIC | It2 | independent capture, port-disable link flap, third-party devices |
| C3: aggregating tap | It3 | host-independent capture |

**Capture rules** (implemented, `hilrig.capture`):
- One pcap per test, analysed only after it stops.
- A UDP/9 sentinel barrier before the test starts; sentinels are excluded from `rows()`.
- Any `_ws.malformed` frame from the DUT fails the test.
- Artifacts are pcapng files with TLS secrets injected (`editcap --inject-secrets tls,<keylog>`).
- `dumpcap` is at `/usr/bin/dumpcap` on Ubuntu 24.04. Give it capabilities with `dpkg-reconfigure wireshark-common`.

**Timing (B9) interpretation.** Sub-second latency and interval assertions come only from the LA or the stimulus timebase. Coarse functional deadlines use the host clock and are labelled *functional* in the catalogue: "back within 20 s", "30 ± 1 messages in 300 s".

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

### 4.1 Topology

```
 END L (K1 term, NC)                                                                    END R (K2 term, NC / FTDI)
 +-120R-o-o-+                                                                                  +-o-o-120R-+
D+ ===+=====+=======+==============+===============+==============+===============+===========+===========+
D- ===+=============+==============+===============+==============+===============+===========+===========+
      |             |              |               |              |               |           |
  bias K3 (NC): [fault box It3]   U1 DUT          U2 STIM         U3 sniffer      FTDI USB-RS485-WE   [Pico 2, It3]
  D+ 510R->+5VA K4/K5 open (NO)   THVD1450        injector        listen-only     mstpcap + host      functional
  D- 510R->GND  K6 short, K7 swap VCC = DUT 3V3   VCC = stim 3V3  VCC = stim 3V3  node MAC 7          peer only
  (one point, always-on 5 V)      MAC 5           USART2 HW-DE    DE=/RE=GND      latency_timer=1
                                  DE=/RE tied     DE 8/16 bit     RO -> LA (3.3 V)
GND ======================== second CAT5e pair, all nodes ===========================================================
 taps: stim rs485_a (PC2) / rs485_b (PA6) via 100k/100k 0.1 % dividers + 100 nF on D+ / D- (MSTP-01; mandatory)
```

- 3-6 m of CAT5e with one pair for data and one for GND; stubs under 0.3 m.
- Two 120 Ω terminators; bias at one point from +5VA (D8).
- Transceiver supplies: U1 from DUT 3V3 (CN8-7), U2 and U3 from the stimulus 3V3. Every RO pull-up goes to its own transceiver's VCC, so no RO drives more than 3.3 V into a stimulus pin or the LA (THVD1450 VOH = VCC − 0.2 V).
- Module rules on **every** transceiver:
  - remove the module's 120 Ω, its bias resistors and its logic-side pull-ups on DE, /RE, DI and RO;
  - fit a 10 k pull-down on each DE net (the THVD1450 internal 2 MΩ is too weak alone) and a 10 k pull-up on each RO that feeds an MCU;
  - hard-wire U3 DE and /RE to GND.
- The FTDI cable and the Pico cannot take these rules. MSTP-22 checks the bus with the FTDI hub port powered off (idle level and frames still valid). THVD1450 bus input current is specified at VCC = 0, so an unpowered U1 does not back-feed.
- The bias taps are mandatory: the tap pins are analog (not FT) and the dividers keep a 5 V driver (FTDI) at ≤ 2.5 V.
- Auto-direction transceivers are forbidden.
- Node plan per D17. Default 38400 baud; matrix 9600-115200.

### 4.2 DUT node (P1, snippet `hil`, FW-07)

| DUT pin | Signal | U1 | Extras |
|---|---|---|---|
| PD5 CN9-6 | USART2_TX | DI | LA `tx` |
| PD6 CN9-4 | USART2_RX, pinctrl `hil_usart2_rx_pd6_pu` (pull-up) | RO | 10 k pull-up to DUT 3V3, LA `rx` |
| PD4 CN9-8 | `usart2_de_pd4` (AF7, push-pull, no pull in hal_stm32 fc11896d) | DE + /RE | 10 k pull-down (the pin floats before pinctrl), LA `de` |

Zephyr 4.4.2 facts the node relies on:
- The binding has `de-enable`, `de-assert-time` and `de-deassert-time` (0-31, in 1/16 bit). `uart_stm32.c` sets DEM at init.
- A later `uart_configure()` clears DEM unless `flow_ctrl = UART_CFG_FLOW_CTRL_RS485`. `uart_config_get()` reports RS485 while DEM is set, so get-modify-set keeps DE.
- `hw-flow-control` must not be set.
- U1 VCC comes from DUT 3V3 (CN8-7), so a power cut takes the node down.
- A 3-jumper selector also allows the roadmap wiring (PG14 TX D1, PG9 RX D0, PD15 DE D9, CN10-14/16 and CN7-18).
- **P2** (later): hardware DE needs FC2 in LPUART-only mode (disable `flexcomm2_lpi2c2` and `dac0`; `nxp,rs485-mode`). The pin roles still need RM confirmation. Otherwise use a GPIO DE on D11.

### 4.3 Capture and decode

| Path | Timestamps | Use |
|---|---|---|
| FTDI + `mstpcap` → pcap DLT 165 → tshark `mstp.*` | host, ms jitter | protocol only; never timing |
| LA raw → sigrok `uart` + `hil/decoders/bacnet_mstp` → `hilrig.mstp` (frames, CRC, `de_timing`, `turnaround`, `usage_delays`, DLT 165 export) | LA | **all timing assertions**. Implemented, with offline unit tests. |
| `stim rs485 rx` | stimulus cycle counter | injector replies |
| LA pcap merged with the Ethernet pcap after the SYNC fit | host after fit | router tests (It3) |

The rig is validated before any DUT MS/TP code exists (once the gate opens):
- **R-08**: LA frames match mstpcap 1:1, and LA gaps are within ±(1 µs + 2 samples) of the injector's **commanded** gaps.
- The implemented MSTP-01/04/06/08/11/23 tests run against the FTDI host node and the injector.
- The Pico is characterised (R-10, It3) and never used as a timing reference.

### 4.4 Clause 9 limits (bacnet-stack 54544d02 `mstpdef.h`/`mstp.h`; ASHRAE 135 not read)

| Parameter | Limit | DUT setting (FW-17) | Measured as | 9600 | 38400 | 115200 |
|---|---|---|---|---|---|---|
| Tturnaround | ≥ 40 bit before the driver is enabled | µs silence timer | DE rise minus the end of the last stop bit on the bus | 4.167 ms | 1.042 ms | 0.347 ms |
| Tpostdrive | ≤ 15 bit | hardware DE, DEDT 16/16 | DE fall minus the end of the stop bit | 1.562 ms | 0.391 ms | 0.130 ms |
| Tframe_gap | ≤ 20 bit between octets | ISR/DMA TX | maximum inter-octet idle | 2.083 ms | 0.521 ms | 0.174 ms |
| Tframe_abort | ≥ 60 bit, ≤ 100 ms | 95 ms | injector gaps | 6.25 ms | 1.56 ms | 0.521 ms |
| Tusage_delay | ≤ 15 ms | – | first DUT octet after Token/PFM | | | |
| Tusage_timeout | 20-35 ms (bacnet-stack clamp; the 135 edition is TBD) | **30 ms** | token retry when NS is mute | | | |
| Treply_delay | ≤ 250 ms | ≤ 245 ms, then Reply Postponed | DER end to reply | | | |
| Treply_timeout | 255-300 ms | **260 ms** (the stack default of 250 is non-compliant) | next DUT frame after an unanswered DER | | | |
| Tno_token + Tslot | 500 + 10·TS ms | – | first PFM window | | | |

Defects the rig is designed to catch:
- Treply_timeout defaults to 250 ms.
- The PASS_TOKEN retry is compared with `>`.
- The Tturnaround timer has ms granularity.
- There is no receive-error path in `dlmstp_rs485_driver`.
- MAX_APDU is 1476 on MS/TP builds.
- bacnet-stack-zephyr has no RS-485 driver.

### 4.5 Fault injection (stimulus protocol v0 caps `rs485`)

| Fault | Injected by | Tier |
|---|---|---|
| bad header or data CRC, truncated frame, length > 501 | `stim rs485 load`/`tx @slot`; frames are always built by `hilrig.mstp.frame()`, never by hand | It2 |
| inter-octet gaps (50/60 bit, 101 ms) | `tx … gap=<idx>:<us>` | It2 |
| framing error, break, jam, baud skew ±1-3 % | `rs485 fe`, `break`, `jam`, `baud <n> <skew_ppm>` | It2 (tx/baud/load implemented; fe/break/jam still to build) |
| duplicate MAC, babbler | injector spoofing | It2/It3 |
| open, short, A/B swap, termination or bias removed | relay box on stimulus spare pins via ULN2803A. Rest state = normal bus: **NC** contacts for the terminators K1/K2 and the bias K3, **NO** contacts for the faults K4-K7, so a stimulus reset leaves the bus terminated and biased. | It3 |
| event-timed collision | Pico PIO agent | It3 |
| DUT reset or power loss mid-TX | `stim reset`, `stim power off` | It2 |

---

## 5. Stimulus board and protocol

### 5.1 Board (B3 confirmed)

A second NUCLEO-F767ZI. Compared with NUCLEO-H563ZI:
- 2 free DAC outputs, PA4 and PA5. On the 4.4.2 `nucleo_h563zi` board, PA4 is reserved for VBUS_SENSE.
- 32-bit TIM2 and TIM5 capture with slave mode, at 9.26 ns and up to 39.7 s.
- Hardware DE on all 8 U(S)ARTs.
- 64-bit SysTick cycle counter at 216 MHz (`CORTEX_M_SYSTICK_64BIT_CYCLE_COUNTER=y`, 4.63 ns quantum).
- Same HAL, board and runner as P1; it doubles as a spare DUT.
- Price: $34.39 against $43.50 (DigiKey, fetched this session [likely]).

Pitfalls:
- Both boards are ST-LINK V2-1, 0483:374b, platform `nucleo_f767zi`. Always select the probe by serial. Keep the stimulus out of the Twister hardware map. udev links `/dev/hil/{dut0,stim0}` by ST-LINK serial, and the same rule sets `ENV{ID_MM_DEVICE_IGNORE}="1"` for 0483:374b (and 0403:6001 in It2), so ModemManager never sends AT probes into the stimulus shell.
- The stimulus's own HSE also comes from its ST-LINK MCO.
- Every stimulus pin is on a Zio header (the stimulus gets no morpho soldering): ao0 PA0 at CN10-29 (SB179), ao2 PD12 at CN10-21, v5 PF4 at CN10-11.

### 5.2 Firmware `apps/hil_stimulus` (implementation; ported this session, committed in It1 W0)

- **Size and build.** Rebuilt this session with SDK 1.0.1 and picolibc against 4.4.2 (with the v2.1 ao fix): 91,708 B flash and 42,624 B RAM with 28 channels, 0 warnings, `CONFIG_PICOLIBC=y`. The It2 overlay (MCP4728, ADS1115, INA226 on I2C1) built at 99,316 B / 45,440 B with the earlier GCC 12.2 toolchain; it is rebuilt with SDK 1.0.1 when I2C2 and the rs485 taps are added. A `BUILD_ASSERT` pins the errno numbering (ENOTSUP = 134).
- **Channel map.** Binding `hil,stim-channels` (vendor prefix `hil` in `dts/bindings/vendor-prefixes.txt`). Node names are channel names. Each child carries `kind`, `gpios`, `io-channels` (named `src`/`sense`/`ref`), `pwms`, `range-mv`, `scale`, `stim-pin` and `dut-pin`. The table is built with `DT_FOREACH_CHILD_STATUS_OKAY_SEP`.
- **Profiles.** It1 ships the P1 map in `boards/nucleo_f767zi.overlay`. It2 moves the `hil-stim` node into `profiles/p1.overlay` and `profiles/p2.overlay` (D22), selected with `-DEXTRA_DTC_OVERLAY_FILE`, and adds `profile = "P1"`.
- **Shell settings.** `SHELL_CMD_BUFF_SIZE=512`, `ARGC_MAX=24`, RX ring 1024, TX ring 256, VT100 colours off, history off. Echo stays on, because the Ctrl-C resync needs metakeys.
- **Board nodes.** `gpio_keys` is disabled and `CONFIG_INPUT=n`, which frees EXTI13. `&mac`, `&mdio`, `&spi1`, `&can1`, `&usart6` and `&timers1` are disabled.
- **ADC.** `st,adc-prescaler = <4>` (27 MHz ≤ 36 MHz max); 480-cycle sampling on the sense channels.
- **Watchdog.** IWDG 4 s. main() stops feeding when a command overruns its budget + 5 s, so a hung stimulus resets into the hardware fail-safe state.
- **ao pins.** The capture pins stay in TIM AF mode with the pinctrl pull-down for the whole run. `is_input_kind()` no longer includes `ao`, so `lines_safe()` never reconfigures them and `edges`/`lat` refuse them (`ERR -1`). Before this fix, the autouse `stim safe` turned them into floating GPIO inputs and `pwmcap` silently reported a static 0 % or 100 % (the pwm driver applies pinctrl only at init, pwm_stm32.c:710). The ao pins also share EXTI lines 0/15/12 with m0, nrst_sense and do4, so they are never armed.
- **Defects fixed.** The port fixes 14 prototype defects and 3 in the host driver. The worst:
  - 32-bit ns timestamps;
  - no OK/ERR on a wrong argument count;
  - inverted power polarity;
  - a bare-CR resync that executes a half-typed line;
  - `safe` reconfiguring the ao capture pins (v2.1).

### 5.3 Protocol v0 summary (full specification: `stimulus_protocol`)

Shell group `stim`, prompt `stim:~$ `. Each command gets exactly one reply line, `OK k=v…` or `ERR -<errno> <text>`, with Zephyr/newlib numbering: ENOTSUP -134, ETIMEDOUT -116. Banner: `STIM READY proto=0 fw= board= rc=0x<reset cause> init=`.

The baseline commands plus compatible additions (proto stays 0):

| Command | Reply | Addition to the baseline |
|---|---|---|
| `info` | `proto fw board profile uptime_ms vdda_mv pwr rst_n caps chans` | `profile`, `vdda_mv`, `pwr`, `rst_n`, `caps` |
| `chan <name>` | – | new |
| `safe` | `OK pwr=1` | DUT power on, DAC EN=0; ao pins untouched |
| `dout` | `t_ns` | – |
| `din` | – | works on every channel with gpios, including ao (IDR read) |
| `pulse … [active]` | `mode=busy\|sleep` | optional `active` argument |
| `edges … [max]` | `t0_ns trunc lv` | optional `max` argument; inputs do, marker (m0-m3, lb), nrst_sense only |
| `lat` | `lat_ns t0_ns` | out-level may be `z`; same input kinds as `edges` |
| `dac <ai> <mV\|z>` | `code vdda_mv` | `z` accepted |
| `adc` | `vdda_mv` | – |
| `reset` | `held_ms t_ns` | – |
| `power` | `pwr t_ns` | – |
| `rstmon [clear]` | – | new |

It2 capabilities are announced in `caps=`, not by a protocol bump: `pwmcap`, `rs485` (baud/clear/load/tx/rx; fe/break/jam/de still to build), `ref` (ADS1115), `ina`, `sync`. The MCP4728s are not a separate command: each is the `src` of an ai channel.

Safety invariants:
- The stimulus fails safe without firmware: when it is in reset, unpowered or hung, every DUT line is Hi-Z (except the JTAG pin PA15, see the protocol §6), NRST is released, the DUT stays powered, RS-485 DE is low and the DAC is off.
- `power off` and `cycle` run lines-safe first.
- The analog sources refuse any non-zero value while the DUT is unpowered (`ERR -1`).
- IRQ-locked sections poll the cycle counter continuously and never block (the real SysTick rule is in the protocol §6).

### 5.4 Host driver `hilrig.stim` (implemented) and It1 follow-ups

Implemented:
- `Stim(port)` with `info() safe() dout() din() pulse() edges() lat() dac() adc() reset() power()`;
- typed replies (`StimInfo`, `Pulse`, `Edges`, `AdcReading`);
- `StimError(errno, text)` and `StimLinkError`;
- `stim.log`;
- read-only commands (`info`, `din`, `edges`, `adc`) retried once; state-changing commands never retried.

Follow-ups in It1 W2:
1. `sync()` sends 0x03 (Ctrl-C) before CR. A bare CR executes a half-typed line.
2. A `STIM READY` banner seen mid-session raises `StimRebooted` (a subclass of StimLinkError) instead of being flushed by `reset_input_buffer()`.
3. Refuse lines over 511 chars or with non-ASCII characters.
4. Parse the new keys and add `chan()`, `rstmon()`, `dac(chan, "z")`, `pulse(active=)` and `edges(max=)`.
5. Error subclasses: `StimTimeout` (-116), `StimNoDevice` (-19, which becomes a pytest skip), `StimNotSupported` (-134) and `StimBusy` (-16).
6. bench.py: `RIG_CHANNELS` += `lb`, `v3v3`, `v5`. The P1 catalog gains polarity flags and the `hil-io` extras. `hil/tools/check_catalog.py` checks them against `build/zephyr/edt.pickle` in the build job.
7. Open the port with `exclusive=True`, so no second process (pre-flash script, ModemManager, a second pytest) can write to the stimulus tty.
8. `tests/rig/test_r01_stim_link.py` checks the fields of one `stim info` and then times 1000 × `stim din do3` (today it times 1000 × `info`, whose estimated 24 ms round trip would fail p99 < 20 ms).
9. Align `hil/host/bench.yml.example` with this design: the LA map of §6, `mqtt_client_id` filled at commissioning from the UID (FW-02) instead of `hil-dut1`, the probe comment (`adapter serial`, not `--dev-id`), and the `bench.py` docstring MAC prefix (02:80:e1).

---

## 6. Logic analyzer channel map

Probes sit on the DUT side of the series resistors, with one short ground per 4 signals. RS-485 A/B is never probed with a digital channel. `bench.yml la.channels` uses the names below.

| CH | It1 FX2 (24 MS/s, 8 ch) | It2 DSLogic Plus (16 ch) | Source | Rate needed | Used by |
|---|---|---|---|---|---|
| 0 | `nrst` | `de` | DUT CN8-5 / PD4 CN9-8 | 1 / ≥ 12 MS/s | RST-01, TIM-04 / MSTP-04,05 |
| 1 | `m0` | `tx` | PG0 CN9-29 / PD5 CN9-6 | ≥ 12 MS/s | boot milestones / MS/TP |
| 2 | `m1` | `bus_ro` | PG1 CN9-30 / U3 RO | ≥ 12 MS/s | R-02a / Tturnaround |
| 3 | `di1` | `nrst` | PF15 CN10-12 / CN8-5 | 1 MS/s | R-02a, IO / reset |
| 4 | `do3` | `m0` | PF13 CN10-2 / PG0 | ≥ 12 MS/s | IO-01 / events |
| 5 | `do0` | `m1` | PB0 CN10-31 (LD1 = mqtt led0) / PG1 | 4 MS/s | MQTT-03 / TIM |
| 6 | `vcp_tx` (PD8 CN12-10, only after the SB7 check; else `m2`) | `di1` | | 4 MS/s | log correlation |
| 7 | `sync` (stim PE6) | `do3` | | 1 MS/s | clock fit |
| 8 | – | `rx` PD6 CN9-4 | | ≥ 12 MS/s | MS/TP RX |
| 9 | – | `stim_de` stim PD4 | | ≥ 12 MS/s | collisions |
| 10 | – | `m2` PG2 CN8-14 | | | net-to-app |
| 11 | – | `m3` PG3 CN8-16 | | | idle/token |
| 12 | – | `ao0` PE9 CN10-4 | | ≥ 10 MS/s | IO-11 cross-check |
| 13 | – | `sync` | | 1 MS/s | R-05, R-09 |
| 14 | – | `vcp_tx` | | 4 MS/s | |
| 15 | – | `do0` | | 4 MS/s | |

- The FX2 goes on a **host root port**, not the switched hub. R-02a checks the sample count on a 60 s capture.
- DSLogic Plus (libsigrok 0.5.2):
  - only USB 2a0e:0020 is supported (D10). The fx2 firmware and FPGA bitstreams are not packaged by Ubuntu: extract them from a DSView download with `sigrok-fwextract-dreamsourcelab-dslogic` (sigrok-util) [likely], and check `sigrok-cli --scan` at bring-up;
  - the buffer is 256 Mbit, which is about 168 ms at 100 MHz × 16 ch;
  - at 400 MHz the DSLogic samples only channels 0-3 (DSView `dsl.h`: "Use Channels 0~3"), which carry `de tx bus_ro nrst`, not the latency signals, so **no test uses 400 MHz**;
  - latency, boot and ISR timings (R-09, TIM-01/02/04) use 100 MHz × 16 ch (10 ns) in buffer windows of ≤ 0.16 s; the driver enables hardware RLE for longer captures (protocol.c:487-494);
  - MS/TP captures use stream mode at 20 MHz × 16;
  - every timing test declares its rate and length.
- Software: `hilrig.la` (sigrok or saleae backend, implemented). Saleae uses `xvfb-run <Logic AppImage> --automation --automationPort 10430`. The sigrok uart warning annotation class is `rx-warnings` in libsigrokdecode 0.5.3 (Ubuntu 24.04) and `rx-warning` in git master; hilrig accepts both. `sigrok-cli -C` with an input file mislabels channels, so export everything and select by name.
- **SYNC fit (It2, R-05).** The host sends `stim sync 20 50`. The stimulus toggles PE6 (LA `sync`) and reports each toggle's timestamp. `hilrig.sync.fit` (implemented) solves offset and skew between the stimulus/LA clock and the host clock. A residual above 250 µs makes cross-domain tests *inconclusive*, not failed. This is enough for the product's ms budgets: edge → PV ≤ 125 ms, write → pin ≤ 105 ms.
- **Preferred** for boot and ISR timings: both edges on the LA (stimulus edge and DUT marker), so one clock measures both.

---

## 7. Power and reset

### 7.1 DUT board configuration (P1; full solder-bridge checklist in the pin tables)

| Item | Setting | Why |
|---|---|---|
| JP3 | **1-2 = E5V** (from It1) | The power cut is on E5V. U5V reaches +5V only through the ST890 and JP3 pin 4, so USB cannot feed the board. |
| JP1 | OFF | required with E5V (UM1974 Table 7) |
| CN4 (2 jumpers), JP5, JP6, JP7, JP4 | ON | ST-LINK to the MCU; IDD; PA7 CRS_DV; PB13 TXD1 |
| Morpho CN11/CN12 | **solder single pins only**: CN11-6 (E5V), CN11-8 (GND), CN11-21 (PB7), CN11-23 (PC13), CN12-10 (PD8), CN12-28 (PB14). **Never populate CN11-5 (VDD) or CN11-7 (BOOT0).** Use a keyed 2-pin housing for the E5V/GND lead. | UM1974 §6.15: morpho headers are not soldered by default [likely for the boards bought: check]. UM1974 Table 21: CN11-5 = VDD, 6 = E5V, 7 = BOOT0, 8 = GND. A lead one pin off puts 5.1 V on VDD (abs max 4.0 V, DS11532 Table 14); a bridge 5-7 sets BOOT0 = 1 and the DUT boots the system bootloader. |
| W1 DMM check | CN11-6 to GND = E5V; CN11-5 and CN11-7 unwired | before the first power-up |
| SB7 (PD8 → CN12-10) | continuity check before using the `vcp_tx` tap | The schematic and Table 12 say closed; §6.9 and Table 9 say open. |
| CN7-14 (D11 = PA7 CRS_DV), CN7-5 (PB13 TXD1) | never wired | RMII |
| Silicon | prefer rev Z (IDCODE 0x10016451); rev A (0x10006451) gets a warning | Zephyr doc: cut-A Ethernet instability |
| Power-up order | +5VA (E5V) first, then the USB hub port | UM1974 §6.4.2: with E5V, power the board first, then connect USB, so the 300 mA enumeration succeeds. The fail-safe default (DUT on) keeps E5V present whenever the host boots. |

### 7.2 Circuits (fixes v1 blocker 0.0; fail-safe = DUT on; no stimulus pin ever sees 5 V)

**It1: Pololu 2810 (module).**

```
 +5VA (5.1 V PSU, always on, >= 2 A) --+------------------+------------------------------> RS-485 bias (It2)
                                       |                  |
                                  Pololu 2810 VIN      R1 10k
                                  slide switch = OFF      |
                                       |                  +--> Pololu ON  (inside: ON-10k->Q3 base, 10k B-E)
                                  VOUT -+-> DUT E5V CN11-6 |
                                        |  (GND CN11-8)    D
                                  100k/100k + 100nF      Q1 BSS138P      (VGS(th) 0.9-1.5 V)
                                  -> STIM PF4 'v5'         G <--100R-- STIM PE5 'pwr'  (1 = DUT OFF)
                                                           |   S -> GND
                                                         R2 100k -> GND
```

- Stimulus pin low, Hi-Z, in reset or unpowered: Q1 is off and V_ON ≈ (5.1 + 0.65)/2 ≈ 2.9 V, so the **DUT is on** (Pololu schematic values).
- PE5 high: Q1 is on, V_ON ≈ 0 and the DUT is off.
- Commissioning DMM check (W2, recorded in bench.yml): V_ON > 1.2 V released and < 0.3 V when cut. There is no stimulus sense channel on V_ON.

**It2: TPS22918 (sharp cut).**

```
 +5VA -> U1 TPS22918 VIN; ON = 100k to +5VA + the same Q1 inverter; CT 1 nF (tON ~1.95 ms, tR ~2.5 ms at 5 V);
         QOD -> 100R -> VOUT (discharge ~124 Ohm); VOUT -> INA226 shunt 0.1 Ohm -> DUT E5V
```

TPS22918 datasheet values (SLVSD76C): VIH(ON) ≥ 1 V, VIL ≤ 0.5 V, ON abs max 6 V (recommended ≤ 5.5 V), 2 A, 52 mΩ, RPD 24 Ω; at VIN = 5 V, CT = 1000 pF: tON 1950 µs and tR 2540 µs typ (§6.6). PERS-02 and OTA-04 on-time budgets include tON ≈ 2 ms.

**NRST (both iterations).**

```
 DUT NRST CN8-5 (internal RPU 30-50k, C53 100 nF, ST-LINK T_NRST, PHY nRST via SB177)
   +--100R-- D  Q2 BSS138P  S -- GND      G <--100R-- STIM PE4 'nrst' (1 = asserted), 100k G->GND
   +--10k--- STIM PE15 'nrst_sense' (input, NO pull, ACTIVE_LOW: 1 = in reset)
 DUT 3V3 CN8-7 -> 100k/100k + 100 nF -> STIM PB1 'v3v3'
```

- Release: NRST crosses VIH (≈ 1.79 V) 2.3-3.9 ms after `t_ns`.
- Internal resets (IWDG, software) drive NRST low for ≥ 20 µs, and `rstmon` counts them.
- Never add a pull-up and never drive NRST push-pull.
- Commissioning DMM check (W2, recorded in bench.yml): NRST ≥ 3.0 V with the stimulus idle.

### 7.3 Mechanisms

**ST-LINK power model (MB1137 B-01 sheet 3).** The ST-LINK LDO (LD3985M33R) is diode-ORed from VIN_5V (D4), E5V (D3) and U5V (D5, BAT60JFILM). Consequences:
- An E5V cut leaves the ST-LINK on USB, so the VCP and the MCO (the DUT's HSE) keep running.
- While E5V is on, a hub-port cycle does **not** power-cycle the DUT ST-LINK. Recovery of a wedged DUT ST-LINK = `stim power off` + hub port off, 2 s, hub port on, `stim power on`.
- The ST-LINK is unpowered only when E5V and the hub port are both off, and then the DUT is unpowered too. The remaining HSE hazard is a wedged ST-LINK (MCO stopped) while E5V is on: the DUT waits forever for HSE (`clock_stm32_ll_common.c` spins on HSERDY with no timeout). Symptom: no console, no DHCP.
- Part of the ST-LINK current comes from E5V whenever E5V is above VBUS, so it appears in the INA226 signature and in the 500 mA budget (allowance 50-100 mA [unverified: measured in W2]). PWR-03 records the VBUS and E5V voltages with each baseline.

| Mechanism | Command | Effect | Use |
|---|---|---|---|
| NRST pulse | `stim reset 10` | MCU **and PHY** reset (SB177); link renegotiates; cause PIN | RST-01, default reset |
| Probe reset or flash | `west flash -r openocd …` | SWD, srst_only | flashing, recovery |
| Mass erase | `west flash -r openocd --erase …` | `stm32f2x mass_erase 0` | start of every OTA session (D25) |
| Soft reboot / fatal | SMP `os reset`, `hil panic` (instrumented) | cause SOFTWARE; fatal → reboot per FW-05 | RST-04 |
| E5V cut | `stim power cycle 2000`, `off`/`on` | true power loss for MCU, PHY and NOR; the ST-LINK stays on USB through D5, so the VCP and MCO keep running | PWR-01, PERS-01, It2 PERS-02/OTA-04 |
| USB port power | `uhubctl -l <hub> -p <n> -a cycle` | cycles the stimulus (its only supply) or the FTDI; the DUT ST-LINK only together with an E5V cut (above) | recovery, PWR-02 |
| PPK2 on JP5, programmable PSU on E5V | host | brown-out ramps | It3 PWR-04 |

Sequences (fixtures):

```
power_cycle(off_ms=2000): stim.safe(); console.close(); stim.power("off"); wait v3v3 < 300 mV;
                          sleep(off_ms); stim.power("on"); console.open(retry_s=10);
                          wait DHCP lease (dnsmasq) + I-Am/CONNACK (release) or HIL-READY (instrumented)
reset():                  stim.reset(10) -> LA/rstmon confirm -> wait link + DHCP + I-Am/CONNACK
session start (D31):      stim.info() (proto=0, profile == bench) -> stim.safe() -> R-07 guard (OpenOCD)
                          -> flash (Twister: request `dut`; local: west flash) -> wait network -> rig_config -> R-02b (BACnet)
```

Hazards:
- Off times are ≥ 200 ms in tests. The protocol minimum is 10 ms, and µs dips are not meaningful.
- E5V is limited to 500 mA, including the ST-LINK share. DUT current at 216 MHz with Ethernet up is measured in W2 [unverified].
- Stimulus lines into an unpowered DUT: every rig pin is FT, so VIN ≤ VDD + 4 V and positive injection is not possible. The 1 kΩ resistors cap contention at 3.3 mA. `stim safe` still precedes every power-off.
- Whether the ST-LINK firmware reacts to PWR_EXT dropping is undocumented. W2 measures it with `udevadm monitor` over 50 cycles.

---

## 8. Software stack

### 8.1 Repository layout (HIL branch; HIL-owned)

```
docs/HIL.md                  this design         docs/SESSION_NOTES.md  HIL section only (contract, wiring, how to run)
hil/                         python package hilrig (pushed at fd8d0f4; pyproject, Python >= 3.11, runs on 3.12)
  src/hilrig/                bench stim capture netns services bacnet mqtt mstp la sync pki
  tests/                     conftest.py; unit/ rig/ sil/ mstp/ (implemented); system/ reset/ power/ mqtt/ tls/
                             bacnet_ip/ io/ persistence/ apps/ (It1/It2)
  net/ pki/ decoders/ saleae/   (implemented)   host/ (install-net-wrappers.sh; W3: install-runner.sh, run-hil, nightly.sh)
  twister/ gen.py -> {firmware,mqtt_tls}/sample.yaml (It1 W3)
  site/                      mqtt-site-hil.conf, mqtt-site-hil-mtls.conf, variant-publish120.conf, f767-app-pool.overlay (D24)
  tools/                     survey.sh check_catalog.py
apps/hil_stimulus/           stimulus firmware (from the scratchpad port, with the v2.1 ao fix)
lib/hil/                     Zephyr module 'hil' (zephyr/module.yml, CMakeLists.txt, Kconfig, hil.c, hil_shell.c)
snippets/hil/  snippets/hil-io/  snippets/hil-keylog/ (It2)
.github/workflows/hil.yml
```

### 8.2 Host package (implementation)

| Fixture | Scope | Provides | State |
|---|---|---|---|
| `bench` / `selected_bench` | session | validated `Bench` from `--bench` or `$HIL_BENCH`. In Twister mode (D31): from `twister_harness_config.devices[0].fixtures` (`hil_bench:<path>`). | implemented (Twister path in W3) |
| `hil_session` | session, autouse with `--hil` | the session start of §7.3: guard, flash (in Twister mode by requesting `dut`), network wait, rig_config. Fails the session if 0 tests ran. | It1 W2 (Twister part W3) |
| `artifacts` | session | output directory (pcaps, services, pki, stim.log, rig reports) | implemented |
| `netns`, `services`, `pki` | session | topology, dnsmasq, mosquitto (keylog), chrony, TlsServer | implemented |
| `stim` + autouse `safe()` around each test | session | stimulus driver (`exclusive=True`) | implemented |
| `la` | session | `la.acquire(seconds, workdir)` | implemented |
| `capture` | function | per-test pcapng; `@pytest.mark.capture(bpf)` | implemented |
| `bacnet`, `mqtt` | session / function | bacnet-stack wrappers (+ `.foreign()`); MQTT `Device` observer | implemented |
| `console` | session | Twister `dut` in Twister mode, else a pyserial console from bench.yml. Log capture only in the release tier. | **It1 W1** |
| `smp` | session | `bacnet_uc_harness` SMP client over UDP inside netns svc | It1 W2 |
| `rig_config` | session | known state (D25) | It1 W2 |
| `dut_power`, `dut_reset` | function | the sequences in §7.3 | It1 W2 |
| `tls_server`, `tls_front` | function | s_server or the TLS-1.2-pinned MQTT front on 192.0.2.2, with `broker.hil.lan` switched there and back (D33) | It1 W2 |

Marks: `hil_only timing slow destructive mstp release capture` (implemented), plus `instrumented`, `rig` and `variant` (new). Rig-gate tests carry `rig`; the conftest orders them first, and a gate failure skips the rest of the session as a *rig fault*.

Per-test budgets: the pytest-timeout ini default stays 300 s. Long tests carry `@pytest.mark.timeout(<criterion duration + 60 s>)`, for example MQTT-02 420 s, PWR-01 and PERS-01 `60 + 30 × cycles` s, NET-03 420 s.

Options:
- `--hil --sil --bench --artifacts --enable-slow --enable-destructive`, plus `--cycles N` (power-cycle and reset loops; default 10 locally, 20 nightly, 50 weekly) and `--hil-select EXPR` (a marker expression that narrows the scenario's `-m`).
- Local run: `sudo -E "$(command -v pytest)" --hil --bench /etc/hil/bench1/bench.yml -m "rig or release" hil/tests`.
- With the Twister harness locally: `pip install -e $ZEPHYR_BASE/scripts/pylib/pytest-twister-harness`, export `ZEPHYR_BASE`, then `pytest --twister-harness --device-type=hardware --platform=nucleo_f767zi --runner=openocd --device-id=$DUT_SN --device-product="STM32 STLink" --device-serial=/dev/hil/dut0 --build-dir=<b> --twister-fixture hil_bench:/etc/hil/bench1/bench.yml --dut-scope=session`. Do not add `-p`. hilrig treats `--twister-harness` like `--twister-config`.

### 8.3 Firmware images per profile (workspace = a firmware branch checkout as the west manifest repo; `$HIL` = the HIL checkout)

| Image | Command | Tier |
|---|---|---|
| P1 BACnet release (plain) | `west build -b nucleo_f767zi $FW/firmware -d b/p1-bac-rel -- -DEXTRA_DTC_OVERLAY_FILE=$HIL/hil/site/f767-app-pool.overlay -DCONFIG_UC_APP_POOL_SIZE=98304`. Links (494,444 B / 291,708 B, built this session). The workaround stays until FW-04. | release |
| P1 BACnet release, MCUboot (It2, OTA-* only) | `west build --sysbuild -b nucleo_f767zi $FW/firmware -d b/p1-bac-mcuboot -- -DEXTRA_CONF_FILE=overlay-mcuboot.conf -DEXTRA_DTC_OVERLAY_FILE=… -DCONFIG_UC_APP_POOL_SIZE=98304`; the OTA candidate adds `-DCONFIG_UC_FW_VERSION="<ver>-hilota"`. Flash the sysbuild build dir. Link status [unverified: first build in It2]. | release |
| P1 BACnet instrumented | plain + `-S hil -S hil-io -DSNIPPET_ROOT=$HIL -DZEPHYR_EXTRA_MODULES=$HIL/lib/hil` | instrumented |
| P1 MQTT release (plain) | `west build -b nucleo_f767zi $MQ/apps/mqtt_tls -d b/p1-mq-rel -- -DEXTRA_CONF_FILE=$HIL/hil/site/mqtt-site-hil.conf`. It sets `BROKER_HOSTNAME="broker.hil.lan"` and CA = `$HIL/hil/pki/ca.crt` (absolute paths are accepted by the app's CMake). | release |
| P1 MQTT release, mTLS | `mqtt-site-hil-mtls.conf` = plain + `APP_MQTT_TLS_CLIENT_AUTH=y` and the `hil/pki/dut-client.{crt,key}` paths. Links (247,416 B / 142,228 B, built this session). | release |
| P1 MQTT variant (It2) | plain + `$HIL/hil/site/variant-publish120.conf` (`APP_MQTT_PUBLISH_INTERVAL_SEC=120`) | release (variant, D24) |
| P1 MQTT instrumented | plain + `-S hil` + module | instrumented |
| Stimulus | `west build -b nucleo_f767zi $HIL/apps/hil_stimulus -d b/stim` | rig |
| P2 (It2) | `-b frdm_mcxn947/mcxn947/cpu0`; never the `//cpu0/qspi` variant | release, instrumented |

`lib/hil` in It1 (instrumented images only):
- Prints `HIL-BOOT board=<CONFIG_BOARD_TARGET> zephyr=<ver> reset=0x<hwinfo cause> uid=<hex>` from `SYS_INIT(APPLICATION, 99)`, then clears the reset cause.
- Prints `HIL-READY ip=<ip> mac=<mac>` on `NET_EVENT_IPV4_ADDR_ADD`. The net_mgmt handler takes `uint64_t mgmt_event` in 4.4.
- Toggles m0 at both lines.
- Shell `hil`: `info`, `mark <n> <0|1|t>`, `panic`. `wdt-stall` (`irq_lock(); for (;;);`) comes in It2.
- `lib/hil/Kconfig` defines `config HIL` (with a prompt) and `config HIL_SHELL` (`bool "hil shell commands"`, `depends on SHELL`, `default y`).
- `snippets/hil/hil.conf` is exactly `CONFIG_HIL=y`, `CONFIG_SHELL=y`, `CONFIG_HWINFO=y`, `CONFIG_REBOOT=y` (Appendix A). `CONFIG_HIL` is a module symbol, so the snippet needs the module. Kconfig warnings (undefined, unmet dependencies, promptless assignments) abort a build (kconfig.py:112-133), and the build job builds every snippet combination.

### 8.4 Flashing and guards per profile

| Profile | Runner | Command | Guard (R-07, before every flash) |
|---|---|---|---|
| P1 and stimulus | `openocd` from SDK 1.0.1 hosttools. `setup.sh -h` installs it at `<sdk>/hosttools/sysroots/x86_64-pokysdk-linux/usr/bin`; fork 91bd278a with `board/st_nucleo_f7.cfg`. | `west flash -d <b> -r openocd -- --cmd-pre-init "adapter serial $SN"` | IDCODE @0xE0042000 ∈ {0x10016451, 0x10006451 (warn)}; FLASH_OPTCR[15:8] @0x40023C14 == 0xAA; UID-derived MAC (D29) == bench. Never write option bytes (RDP 2 is permanent). |
| P1 under Twister | product `STM32 STLink` + runner `openocd`, so Twister adds `--cmd-pre-init "hla_serial <id>"` (an alias of `adapter serial`) | map.yml (below) | The `hil_session` fixture runs the guard before it requests `dut`. `pre_script: /opt/hil/bin/pre-flash.sh` repeats only the OpenOCD guard (defence in depth); it never opens the stimulus tty, because it runs as a separate process inside the pytest session (after connect(), before the flash) with no arguments. |
| P2 | `linkserver` (default; factory CMSIS-DAP MCU-Link firmware), or `pyocd --target=mcxn947` with pack NXP.MCXN947_DFP 17.0.0 | `west flash -r linkserver --probe <serial>` | never write PFR/CMPA (the doc writes CMPA at 0x01004000); never build `//qspi`; J21 never fitted; pin the probe firmware and LinkServer versions |
| P3 | `stm32cubeprogrammer --dev-id <SN>` or `pyocd --target=stm32h563zitx`. There is no openocd runner in 4.4.2 (the board.cmake include is commented "FIXME"). | | option-byte read only |

`/etc/hil/bench1/map.yml` (P1):

```yaml
- connected: true
  id: <DUT ST-LINK SN>
  platform: nucleo_f767zi
  product: STM32 STLink
  runner: openocd
  serial: /dev/hil/dut0
  pre_script: /opt/hil/bin/pre-flash.sh
  fixtures: ["hil_bench:/etc/hil/bench1/bench.yml", hil-dut-f767]
```

Twister matches the fixture on the part before `:` (hardwaremap.py:493), and the pytest harness receives the full strings.

### 8.5 Twister integration (It1 W3; D28, D31)

- `hil/twister/gen.py` renders the alt-config sample.yaml files with absolute paths (Twister does not expand variables in `extra_args`) into `/opt/hil/out/<run>/alt/{bacnet,mqtt}/sample.yaml`:
  - BACnet: `hil.bacnet.release`, `hil.bacnet.instrumented` (`required_snippets: [hil, hil-io]`), and in It2 `hil.bacnet.release.mcuboot` (`sysbuild: true`);
  - MQTT: `hil.mqtt.release`, `hil.mqtt.release.mtls`, `hil.mqtt.instrumented` (`required_snippets: [hil]`), and in It2 `hil.mqtt.variant`.
- Each scenario sets `harness: pytest`, `tags: [hil]`, **`timeout: 3600`** (the Twister default of 60 s would kill pytest with "Pytest timeout") and `harness_config: {fixture: hil_bench, pytest_root: ["$HIL_TESTS"], pytest_dut_scope: session, pytest_args: [...]}`:
  - release scenarios: `["--enable-slow", "-m", "rig or release"]`;
  - instrumented scenarios: `["--enable-slow", "-m", "rig or instrumented"]`;
  - variant scenario: `["-m", "rig or variant"]`.
  `pytest_root` goes through `expandvars` and is joined to the app dir (harness.py).
- Build (on the host, build job):
  ```
  west twister -T $FW/firmware --alt-config-root /opt/hil/out/$RUN/alt/bacnet -p nucleo_f767zi --tag hil \
    --device-testing --hardware-map /etc/hil/bench1/map.yml --build-only --allow-installed-plugin \
    -O /opt/hil/out/$RUN/bacnet
  ```
- Test (hil job): the **same** `-T`, `--alt-config-root`, `--tag` and `-O`, with `--test-only --allow-installed-plugin --pytest-args=--cycles=$CYCLES --pytest-args=--hil-select=$SELECT`; weekly runs add `--timeout-multiplier 2`.
- MQTT is a second invocation with its own alt root and `-O /opt/hil/out/$RUN/mqtt`.
- `.config` and ELF checks (SEC-01, check_catalog) run in the build step, because artifact packaging drops `.config`.
- W3 check: `west twister --list-tests -T $FW/firmware --alt-config-root … --allow-installed-plugin` exits 0 in the host venv.

### 8.6 Pinned tools

| Tool | Version |
|---|---|
| Zephyr / SDK | v4.4.2 / 1.0.1 minimal + `toolchain_gnu_linux-x86_64_arm-zephyr-eabi` |
| CI image (optional; reproducing builds elsewhere) | `ghcr.io/zephyrproject-rtos/ci:v0.29.4` (Dockerfile.ci ZSDK_VERSION=1.0.1) |
| Python | 3.12 venv (one venv; Twister runs with `--allow-installed-plugin`) |
| pytest | ≥ 8, with pytest-timeout |
| pytest-twister-harness | from `$ZEPHYR_BASE` |
| smpclient / smpmgr | 7.3.0 / 0.19.1 |
| paho-mqtt | 2.x |
| mosquitto | 2.1.2 (container) |
| tshark | 4.2.2 |
| sigrok-cli / libsigrok / libsigrokdecode | 0.7.2 / 0.5.2 / 0.5.3 |
| DSLogic firmware | extracted from DSView with `sigrok-fwextract-dreamsourcelab-dslogic` [likely] (It2) |
| logic2-automation | 1.0.11 (It3, or It2 if D10 falls back) |
| OpenOCD | SDK hosttools (0.12.0 + Zephyr patches) |
| WASM test apps (It2) | clang-18 with `wasm32` and `wasm-ld`, `wamrc` 2.4.5 (BACnet wasm/README.md) |
| python-mbedtls (conditional, FW-10 = DTLS) | chosen when needed |

---

## 9. Test pyramid, tiers and catalogue index

```
            +-------------------------------+  HIL: real PHY, E5V power, NRST, IO loop, RS-485 electrical,
            |  ~15 % of cases               |  Clause 9 timing, flash/NOR persistence, OTA swaps
          +-+-------------------------------+-+  SIL: native_sim in netns lan-a (TAP for BACnet broadcast,
          |  same pytest files, no timing     |  NSOS allowed for MQTT); bacserv stand-in; e2e_native_sim.sh as-is
        +-+-----------------------------------+-+  Unit: firmware ztest (26/26 pass today), hilrig unit tests
        |  object logic, config parsing, MS/TP   |  (fake stim pty, fake MQTT DUT, decoders), bacnet-stack ctest
        +----------------------------------------+
```

**Test tiers (B12):**

| Tier | Image | May use | Must not use | Identity from |
|---|---|---|---|---|
| **release** (black box) | a release artifact (D24); SEC-01 guards its `.config`. *Variant* tests (mark `variant`) run on a release image plus one documented test symbol, reported separately. | Ethernet (BACnet/IP, MQTT, SMP, DHCP), stimulus, LA; the console is **recorded** for diagnostics | console input; console lines as pass/fail criteria (except `formatting /lfs`, FW-03) | DHCP lease MAC, I-Am instance, MQTT `info`, SMP `uc_node info` |
| **instrumented** | `-S hil` (+ `hil-io`) | the above plus the console (`HIL-BOOT/READY`, `hil` shell, `uc` shell), markers m0-m3, extra channels | – | HIL-READY |
| **rig** | stimulus plus either image | anything | – | – |
| **sil** | native_sim / bacserv | netns only | timing asserts | bench-sil.yml |

Every HIL session starts with the rig gate R-01, R-02a, R-04 and R-07. BACnet sessions add R-02b right after `rig_config`. MQTT sessions accept an R-02b pass for this bench that is less than 24 h old (state file next to bench.yml). If a gate test fails, the rest of the run is skipped as a *rig fault*, which is reported separately from DUT failures.

**Catalogue index by iteration** (criteria are in the test catalogue):

| Area | It1 (23 HIL tests) | It2 | It3 |
|---|---|---|---|
| Rig | R-01, R-02a, R-02b, R-04, R-06, R-07 | R-05, R-08 (gated), R-09 | R-10 |
| System, reset, power | SYS-01, RST-01, RST-04, PWR-01, PWR-02 | RST-03, PWR-03, PWR-05, TIM-04..06 | PWR-04, TIM-01/02 (informational), MEM-01/02 |
| MQTT / TLS | MQTT-01..03, TLS-01, TLS-03 | MQTT-04..07, 09 (variant), 10, TLS-02, 05, 06 | MQTT-08, TLS-04, 07, TIME-01 |
| Network | NET-03 | NET-01, 02, 05, 06, 07 | NET-04 |
| BACnet/IP | BIP-01, BIP-03 | BIP-02, 05, 06, 07, 10, 11, 13a, 13b; FD-01..04 | BIP-04, 08, 09, 12, 14; BBMD-01..03 (EPICS-gated) |
| I/O | IO-01, IO-04 | IO-03, 05, 07, 09, 11, 15, 16 | IO-14 |
| Persistence / OTA | PERS-01 | PERS-02, 03, OTA-01..05 | – |
| Management / security / apps | SEC-01 | SMP-01..03, SEC-02, 03, CFG-01, 02, APP-01..03 | – |
| MS/TP | – | MSTP-01, 03..08, 11, 22..24 (gated, D34) | MSTP-02, 09, 10, 12..21, 25..28 |
| Robustness | – | ROB-01, 05 | ROB-02..04, HUB-01 |
| SIL / unit | S-01, S-02 (implemented), S-03 (W2) | S-04, S-05, U-01 | – |

15 of the 23 It1 HIL tests are release tier: SYS-01, RST-01, PWR-01, MQTT-01..03, TLS-01, TLS-03, NET-03, BIP-01, BIP-03, IO-01, IO-04, PERS-01 and SEC-01.

**Rules applied to I/O and timing tests** (from verdict_2 #6):
- I/O tests assert the product's polled behaviour from docs/io.md: edge → PV ≤ `sample_ms` + `debounce_ms` + 5 ms; write → pin ≤ `sample_ms` + 5 ms; pulses shorter than debounce are ignored.
- ISR latency (TIM-01/02) is informational rig characterisation.
- IO-02 (8-edge skew) and IO-06 (20k edge count) are dropped.

---

## 10. CI (trigger model for this repo)

Facts (GitHub docs read this session):
- The repo has no `main`. The default branch is `claude/inter-session-communication-h989ye` (`git ls-remote --symref origin HEAD`), and it has no `.github/`.
- `schedule` runs only on the default branch.
- `workflow_dispatch` "will only trigger a workflow run if the workflow file exists on the default branch" (events page and manual-run page). "Once a workflow has run at least once, you can dispatch it against any branch." With hil.yml absent from the default branch, neither schedule nor dispatch can start it.
- `push` runs the workflow file of the pushed ref.
- Job containers do not support the `--network` option.
- Firmware sessions push WIP snapshots seconds apart.

Model (D27, D32):
1. `.github/workflows/hil.yml` lives on the HIL branch and on the machine branch `hil/nightly`. Firmware branches never carry it, so their pushes trigger nothing.
2. Triggers:
   - **push to the HIL branch** (`paths: [hil/**, apps/hil_stimulus/**, lib/hil/**, snippets/**, .github/workflows/hil.yml, .hil-run.env]`): only the `build` job (stimulus, P1 images, SEC-01, check_catalog, unit tests, SIL). It never touches the bench. Concurrency `{group: hil-build-${{ github.ref }}, cancel-in-progress: true}`, so WIP bursts collapse.
   - **push to `hil/nightly`**: build + hil. The host timer `hil-nightly.timer` (OnCalendar 01:17) runs `/opt/hil/bin/nightly`: fetch, `git checkout -B hil/nightly origin/<HIL branch>`, write `.hil-run.env` (`BACNET_REF`, `MQTT_REF`, `SELECT=not destructive`, `CYCLES=20`; Sundays `CYCLES=50`), commit, `git push -f`. Manual HIL runs use the same script with arguments.
   - `workflow_dispatch` (inputs `bacnet_ref`, `mqtt_ref`, `select`, `cycles`) stays in the file. It becomes usable once hil.yml is on the default branch (open question 3: create `main`); then switch the nightly to `on: schedule`.
   - **Fallback**: 10 min after its push the timer checks `gh run list --branch hil/nightly --limit 1`. If no run exists for that commit, it runs `hil/host/build.sh` and `/opt/hil/bin/run-hil --local` directly under the same flock and stores JUnit, twister.json and `hil/**` under `/var/lib/hil/nightly/<date>`.
   - The push token is a fine-grained token (contents:write and actions:read, this repository only) in the `hil` user's keyring, never in a job. It can push to any branch of this repo (risk R19).
3. Jobs (both directly on the host, no job container, runner user `hil`):
   - `build`: labels `[self-hosted, linux, hil-build]`. Checks out HIL, BACnet (`BACNET_REF`, default the branch tip, path `ws/fw`) and MQTT; runs `west update --narrow` in the persistent `/opt/hil/ws`; renders the alt configs; builds with `-O /opt/hil/out/${{ github.run_id }}/<app>` (outside `_work`); runs SEC-01, check_catalog, the unit tests and SIL (hil/tests/sil and the MQTT session's `e2e_native_sim.sh` as-is). Output: `out_dir`.
   - `hil`: `needs: build`, `if: github.ref == 'refs/heads/hil/nightly' || github.event_name == 'workflow_dispatch'`, labels `[self-hosted, linux, hil, bench1]`, `timeout-minutes: 300`. It runs `sudo /opt/hil/bin/run-hil "${{ needs.build.outputs.out_dir }}"`. run-hil takes `flock -w 1800 /run/hil/bench1.lock` (local runs hold the same lock and win), then runs `stim safe` and turns all hub ports on, reflashes the stimulus if its fw hash differs, and runs Twister `--test-only` per app with the build's `-T`, `--alt-config-root`, `--tag` and `-O`.
   - Artifacts (7-day retention): JUnit (with per-test durations), `twister.json`, `handler.log`, `device.log`, and `hil/**` (pcaps with keys injected, `.sr`, `stim.log`, service logs, rig reports). A tmpfiles rule prunes `/opt/hil/out` after 7 days.
4. Workflow settings:
   - hil job: `concurrency: {group: hil-bench1, cancel-in-progress: false, queue: max}` (`queue: max` allows up to 100 pending runs and cannot be combined with cancel-in-progress). Only nightly and manual runs reach it.
   - `permissions: contents: read`; no secrets; fork PR workflows disabled.
5. The runner hook `ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/hil/hooks/job-started.sh` wraps its work in `timeout 60`, because hooks have no timeout. It only cleans this job's own `_work` subtree. It does **no** bench actions: those happen inside run-hil after it holds the lock.
6. W3 verification: the runner is registered as user `hil`; `west twister --list-tests … --allow-installed-plugin` exits 0; a push to `hil/nightly` starts a run; the fallback path writes results; `hil-net-up`, the mosquitto container and `openocd … adapter serial` all work from inside the hil job.

```yaml
on:
  push:
    branches: [claude/hardware-in-loop-testing-x74tww, hil/nightly]
    paths: [hil/**, apps/hil_stimulus/**, lib/hil/**, snippets/**, .github/workflows/hil.yml, .hil-run.env]
  workflow_dispatch: {inputs: {bacnet_ref: {}, mqtt_ref: {}, select: {default: "not destructive"}, cycles: {default: "20"}}}
permissions: {contents: read}
jobs:
  build:
    runs-on: [self-hosted, linux, hil-build]
    concurrency: {group: "hil-build-${{ github.ref }}", cancel-in-progress: true}
  hil:
    needs: build
    if: github.ref == 'refs/heads/hil/nightly' || github.event_name == 'workflow_dispatch'
    runs-on: [self-hosted, linux, hil, bench1]
    timeout-minutes: 300
    concurrency: {group: hil-bench1, cancel-in-progress: false, queue: max}
```

**Nightly budget** (It1, estimate [likely]; JUnit durations replace it after the first week):

| Session | Content | ≈ min |
|---|---|---|
| hil.bacnet.release | gate, SYS-01, RST-01 ×20, PWR-01 ×20, PERS-01 ×20, NET-03 ×10, BIP, IO, R-02b | 30 |
| hil.bacnet.instrumented | gate, RST-04 ×5, HIL-BOOT checks | 5 |
| hil.mqtt.release | gate, SYS-01, RST-01 ×20, PWR-01 ×20, MQTT-01..03, TLS-01, NET-03 ×10 | 35 |
| hil.mqtt.release.mtls | gate, TLS-03 | 5 |
| hil.mqtt.instrumented | gate, RST-04 ×5 | 5 |
| flashes, stimulus check, service start | | 10 |
| **total** | | **≈ 90** (weekly with 50 cycles ≈ 130) |

---

## 11. Bring-up plan

**Iteration 1** (Fri Sep 25 – Sun Oct 18): 3 weeks for one person, including one slack week.

It1 shopping list. Confirm the board set and owned tools with the user on day 0, **before** ordering. Buy the FX2 clone, the W25Q128 breakout and the Pololu 2810 from EU-stock distributors (2-3 day delivery); marketplace orders (1-2 weeks) only for spares.

| # | Item | Qty | ≈ € |
|---|---|---|---|
| 1 | NUCLEO-F767ZI (DUT + stimulus; buy 1 if a DUT is already owned) | 2 | 60-70 |
| 2 | W25Q128JV SPI NOR breakout, 3.3 V (/lfs on the DUT) | 1 (+1) | 3-6 |
| 3 | FX2 CY7C68013A 8-ch LA clone | 1 | 8-15 |
| 4 | Pololu 2810 mini MOSFET switch LV | 1 (+1) | 6-12 |
| 5 | 5 V ≥ 2 A regulated PSU (+5VA) plus DC jack/terminal | 1 | 10-15 |
| 6 | BSS138P (SOT-23) on adapters | 4 | 5 |
| 7 | Resistors (1 k ×30, 10 k, 20 k, 33 k, 100 k, 100 R), 100 nF ×20, 1 µF | kit | 8-12 |
| 8 | Perfboards ×2, 2.54 mm single male pins (morpho), keyed 2-pin housing, DuPont wires F-F/F-M ×80 | set | 12-18 |
| 9 | Powered USB hub on the uhubctl list | 1 | 40-60 |
| 10 | Cat6 patch cables | 3 | 6 |
| 11 | USB3 GbE NIC RTL8153 (only if the host has one NIC) | 0-1 | 0-15 |
| 12 | USB data cables matching the ports (ST-LINK V2-1 micro-B [likely]; FX2 clone mini-B or micro-B, check the unit) | 3 (+1) | 6-10 |
| 13 | x86 N100 mini-PC, 16 GB, 2× i226 (0 if an existing Linux PC is reused) | 0-1 | 180-250 |
| – | Assumed owned (confirm): DMM, soldering station, bench PSU with current limit | – | 0 |

Total without the host: about €165-245 (low ends without the NIC, high ends with it). With the host: about €345-495.

| Week | Work | Exit |
|---|---|---|
| **W0** (Sep 25-27) | - Confirm the board set and owned tools with the user, then order the parts.<br>- Commit `docs/HIL.md`; replace the stale `docs/SESSION_NOTES.md` with the HIL section (contract v0); resync `modules/` from the BACnet tip.<br>- Commit `apps/hil_stimulus` (port with the ao fix), `snippets/hil` and `hil-io`, the `lib/hil` skeleton, `hil/tools/survey.sh`, `hil/site/*`. | Everything builds on the host SDK; survey output recorded and clean |
| **W1** (Sep 28 – Oct 4) | - Host: Ubuntu, SDK 1.0.1 + `setup.sh -h`, OpenOCD udev rules (`<sdk>/hosttools/sysroots/x86_64-pokysdk-linux/usr/share/openocd/contrib/60-openocd.rules`) plus the HIL rule with `ID_MM_DEVICE_IGNORE`, dumpcap caps, docker mosquitto 2.1.2, bacnet-stack tools, `install-net-wrappers.sh`.<br>- DUT prep: solder only CN11-6, CN11-8, CN11-21, CN11-23, CN12-10 and CN12-28; DMM check (CN11-6 = E5V, CN11-5/7 unwired); JP3 1-2 with E5V from the PSU directly (no switch yet), E5V before USB; continuity checks (SB7, CN7-14).<br>- Commissioning: OpenOCD UID/IDCODE/RDP read, MAC calculation (D29), DHCPDISCOVER confirmation, bench.yml.<br>- `console` fixture; network-only tests. | R-04, R-06, R-07, SYS-01, MQTT-01..03 and TLS-03 (TLS 1.3 halves), TLS-01 (both versions via s_server), NET-03, BIP-01, BIP-03, SEC-01 green (or xfail citing an FW id) |
| **W2** (Oct 5-11) | - Stimulus: flash the SDK 1.0.1 build with its serial.<br>- Hand-wire the harness from the pin tables.<br>- Pololu + inverter, NRST FET, senses, FX2 (8 ch).<br>- Commissioning measurements into bench.yml: V_ON levels, NRST idle ≥ 3.0 V; 50 E5V cycles under `udevadm monitor` (no ST-LINK re-enumeration); DUT current.<br>- hilrig follow-ups (§5.4); svc second address and `point_broker` (D33); fixtures `hil_session`, `smp`, `rig_config`, `dut_power`, `tls_server`, `tls_front`; S-03 wrapper. | R-01, R-02a, R-02b, RST-01, RST-04, PWR-01, PWR-02, IO-01, IO-04, PERS-01 green (RST-04 BACnet xfail until FW-05); TLS 1.2 halves of MQTT-01 and TLS-03 green |
| **W3** (Oct 12-18) | - CI: runner registration as user `hil`, `install-runner.sh`, `hil.yml`, `nightly` script and timer with its fallback, Twister alt configs (`gen.py`), flock, job hook; the §10 item 6 checks.<br>- **Slack for rig debugging.**<br>- Order the It2 parts (MS/TP parts only if the D34 gate is open). | 3 consecutive green nightly runs of the 23 It1 tests |

**Iteration 2** (≈ 5 weeks, from Oct 19). Order: DSLogic Plus (only after the seller confirms USB 2a0e:0020; else the Saleae), FRDM-MCXN947, MCP4728 ×2, ADS1115 ×2, INA226, TPS22918 ×2, R3 proto shield, managed switch. MS/TP parts (FTDI USB-RS485-WE, THVD1450 ×4, bus kit, tap dividers) only when the D34 gate opens.

| Work item (in order) | Tests / deliverables |
|---|---|
| DSLogic (PID check, firmware extraction, `sigrok-cli --scan`), SYNC fit, stimulus latency | R-05, R-09, TIM-04..06, IO-03, IO-05 |
| Analog reference (I2C1 and I2C2) | IO-07, IO-09, IO-11 (`pwmcap`), PWR-03 |
| TPS22918 power cut, NRST during writes | PERS-02, PERS-03, OTA-01..05 (MCUboot sysbuild artifact) |
| Management and security | SMP-01..03, SEC-02, SEC-03, CFG-01/02 |
| WASM apps (clang-18, wamrc 2.4.5) | APP-01..03; the test app also provides MSO/MSV for BIP-13a/b |
| Remaining It2 MQTT, TLS, network, I/O and BACnet tests | see §9 |
| P2 network tests | SYS-01, BIP-01/03, MQTT-01..03 on frdm_mcxn947 via linkserver, power by uhubctl |
| Hardware | R3 shield on a protoboard (2.2 kΩ, profile shunts, pin tables §6) |
| MS/TP bench (gated, D34) | R-08, MSTP-01/03..08/11/22..24; DUT MS/TP tests once FW-07 lands. If the gate is still closed at the end of It2, this row moves to It3. |

**Iteration 3** (on demand): Saleae (unless already in It2), PPK2, programmable PSU, relay fault box, KiCad interposer (after the board set is frozen), spare-DUT 24 h weekend soak (not 72 h weekly on the main bench), Pico peer, commercial MS/TP device, P3, labgrid for a second bench.

---

## 12. Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | BACnet F767 default build does not link (RAM +29,564 B) | FW-04; the app-pool workaround (links, built this session) whitelisted in SEC-01; `lib/hil` kept tiny |
| 2 | DUT HSE comes from the ST-LINK MCO, and Zephyr waits on HSERDY forever | The ST-LINK LDO is fed from U5V, E5V and VIN, so it is off only when the DUT is off too. Remaining case: a wedged ST-LINK with E5V on (symptom "no console, no DHCP") → recovery sequence in §7.3. |
| 3 | Two identical ST-LINKs; `west flash --serial` does not choose the probe | `adapter serial` always; R-07 checks the UID before flashing; udev links by serial |
| 4 | Morpho pins unpopulated; SB7 ambiguous; CN11-5 (VDD) and CN11-7 (BOOT0) next to E5V/GND | W1 single-pin soldering, keyed power housing, DMM and continuity checks |
| 5 | NRST also resets the PHY | every reset budget includes autonegotiation and DHCP |
| 6 | ST-LINK reaction to E5V cuts unknown | 50-cycle measurement in W2; fallback: power via VIN |
| 7 | E5V 500 mA limit, shared with part of the ST-LINK current | measure in W2; PWR-03 records VBUS and E5V; TPS22918 is rated 2 A |
| 8 | Back-feed; MCXN not 5 V tolerant (±3 mA injection); ADS1115 inputs on DUT nets | 1 k (P1) / 2.2 k (shield) resistors; 10 kΩ in series with every ADS1115 input on a DUT net; `safe` before off; analog refused while off; PWR-02 |
| 9 | Persistent state leaks (NOR survives flashing; `FORMAT_ON_FAIL`; stale MCUboot slots) | `rig_config` push; the `formatting /lfs` line fails PERS tests; mass erase before OTA sessions; FW-08 |
| 10 | Fatal error halts the DUT (4.4.2 default) | FW-05; RST-04 |
| 11 | SMP unauthenticated (shell/fs/reset) on the LAN; DCC with the default password, Reinit with none | SEC-02, SEC-03; `lan-a` never bridged to the uplink |
| 12 | SMP catalog reply exceeds 1024 B with 17+ channels | catalog read from the build's devicetree; FW-09 |
| 13 | MS/TP UART choice diverges from the BACnet roadmap | FW-07 proposal; U1 selector; MSTP-04/05 quantify GPIO DE |
| 14 | TLS 1.3 hides alerts; client-cert rejection is late | keylogs; accept both forms (TLS-03) |
| 15 | mosquitto `tls_version` is only a minimum | s_server and the TLS-1.2 front on 192.0.2.2 (D33) |
| 16 | Certificate dates unchecked | `xfail(strict)` until FW-12 |
| 17 | tshark "Capturing on" printed before the capture is live; `-c` counts pre-filter packets | sentinel barrier (implemented); capture filters where `-c` is used |
| 18 | NIC offloads distort the capture | `ethtool -K` in `up.sh` (implemented) |
| 19 | CI jobs run as root on the bench (run-hil via sudo; D32) and the nightly push token can push to any branch | private repo, owner-only pushes, no fork workflows, no secrets in jobs, token only in the host keyring |
| 20 | Schedules and dispatch need the workflow on the default branch | nightly by push to `hil/nightly`; direct run-hil fallback; `main` later |
| 21 | DSLogic buffer limits; new DSLogic Plus revisions (2a0e:0030/0034) are DSView-only; 400 MHz = channels 0-3 only | PID check before ordering, Saleae fallback; 100 MHz × 16 windows ≤ 0.16 s; sample-count checks |
| 22 | FTDI 16 ms latency; host timestamps | `latency_timer=1`; timing only from LA or stimulus |
| 23 | bacnet-stack MS/TP defaults non-conformant | FW-17; MSTP-10/12 expected to fail until fixed |
| 24 | Shared-bug blindness (bacnet-stack on both sides) | bacpypes3 and Wireshark as independent decoders; commercial device in It3 |
| 25 | One bench: contention between nightly, local and soak runs | flock with local priority; push runs never touch the bench; soak only on the spare DUT |
| 26 | MCUboot layout and merged images | flash the sysbuild dir; mass erase first; OTA destructive tests on the spare DUT |
| 27 | MCXN analog config (VREF standby, 48 MHz ADC clock with power-level 0) | FW-14; the first P2 AI test decides |
| 28 | Scope creep for one person | It1 gate; MS/TP gate (D34); It2/It3 items are pulled only when needed |
| 29 | Stimulus built with another toolchain during research | **retired**: rebuilt with SDK 1.0.1 + picolibc this session (0 warnings); the errno `BUILD_ASSERT` stays |
| 30 | Branches move under the design; stale shared copies on the HIL branch | `survey.sh` (fails on drift) before each revision; FW-06 announcements |
| 31 | A Twister run that skips everything reports green | D31: Twister mode implies `--hil`; 0 executed tests fails the session |
| 32 | Nightly outgrows the job timeout | 20 cycles nightly, 50 weekly; per-app scenarios of ≤ 3600 s; JUnit durations tracked |
| 33 | FW-10 chooses SMP over DTLS and plain SMP stops (rig_config breaks) | conditional DTLS client (§3.2); console SMP fallback for instrumented images |
| 34 | MS/TP bench effort with no DUT MS/TP code | D34 gate |

---

## Appendix A: DUT HIL overlay for nucleo_f767zi (snippets; edtlib-validated with warnings as errors against the v4.4.2 bindings, for BACnet + hil + hil-io and for mqtt_tls + hil; the pin scan found no conflicts)

`snippets/hil/snippet.yml` (the `hil-io` file is the same with `name: hil-io` and no conf):

```yaml
name: hil
append:
  EXTRA_CONF_FILE: hil.conf
boards:
  nucleo_f767zi:
    append:
      EXTRA_DTC_OVERLAY_FILE: boards/nucleo_f767zi.overlay
```

`snippets/hil/boards/nucleo_f767zi.overlay` (usable with both apps; never references `&uc_io`):

```dts
#include <zephyr/dt-bindings/gpio/gpio.h>
#include <zephyr/dt-bindings/pinctrl/stm32-pinctrl.h>

&pinctrl {
	hil_usart2_rx_pd6_pu: hil_usart2_rx_pd6_pu {
		pinmux = <STM32_PINMUX('D', 6, AF7)>;
		bias-pull-up;                      /* RO is Hi-Z while DE = /RE = 1 */
	};
};

&usart2 {                                   /* MS/TP (It2), alias mstp-uart0; FW-07 */
	pinctrl-0 = <&usart2_tx_pd5 &hil_usart2_rx_pd6_pu &usart2_de_pd4>;
	pinctrl-names = "default";
	current-speed = <38400>;
	de-enable;
	de-assert-time = <16>;                 /* 1/16-bit units: 1 bit = 26 us at 38400 */
	de-deassert-time = <16>;
	status = "okay";
};

/ {
	aliases { mstp-uart0 = &usart2; };
	zephyr,user {
		hil-marker-gpios = <&gpiog 0 GPIO_ACTIVE_HIGH>, <&gpiog 1 GPIO_ACTIVE_HIGH>,
				   <&gpiog 2 GPIO_ACTIVE_HIGH>, <&gpiog 3 GPIO_ACTIVE_HIGH>;  /* one BSRR write */
	};
};
```

`snippets/hil-io/boards/nucleo_f767zi.overlay` (BACnet only; appended after ao2, so ids 0-16 are unchanged):

```dts
#include <zephyr/dt-bindings/gpio/gpio.h>
&uc_io {
	di3 { kind = "di"; gpios = <&gpioe 10 (GPIO_ACTIVE_HIGH | GPIO_PULL_DOWN)>; description = "HIL CN10-24 PE10"; };
	di4 { kind = "di"; gpios = <&gpioe 12 (GPIO_ACTIVE_HIGH | GPIO_PULL_DOWN)>; description = "HIL CN10-26 PE12"; };
	do5 { kind = "do"; gpios = <&gpioe 14 GPIO_ACTIVE_HIGH>; description = "HIL CN10-28 PE14"; };
	do6 { kind = "do"; gpios = <&gpioe 15 GPIO_ACTIVE_HIGH>; description = "HIL CN10-30 PE15"; };
};
```

`snippets/hil/hil.conf` (the one list, also quoted in §8.3): `CONFIG_HIL=y`, `CONFIG_SHELL=y`, `CONFIG_HWINFO=y`, `CONFIG_REBOOT=y`. `CONFIG_HIL` and `CONFIG_HIL_SHELL` (`depends on SHELL`, `default y`) come from `lib/hil/Kconfig`, so the snippet must be used together with `-DZEPHYR_EXTRA_MODULES=$HIL/lib/hil` (`hil/host/build.sh` passes both).

`lib/hil/zephyr/module.yml`: `name: hil`, `build: {cmake: ., kconfig: Kconfig}`.

Side findings for other sessions:
- `snippets/uc-ramfs/snippet.yml` has no `boards:` section, so its board overlays are never applied.
- The v4.4.2 board DTS declares B1 (PC13) `GPIO_ACTIVE_LOW`, but pressed = VDD. The catalog's `ACTIVE_HIGH` is correct.
- The MQTT branch's `modules/wasm-micro-runtime` still has WAMR_OS_THREAD_STACKS, which BACnet 2161be7 removed.

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

- **Zephyr v4.4.2:**
  - boards `nucleo_f767zi`, `frdm_mcxn947`, `nucleo_h563zi` (dts, board.cmake, openocd.cfg), `boards/common/openocd-stm32.board.cmake`;
  - drivers `uart_stm32.c`, `eth_stm32_hal_common.c`, `hwinfo_stm32.c`, `clock_stm32_ll_common.c`, `gpio_stm32.c`, `intc_gpio_stm32.c`, `pwm_stm32.c`, `cortex_m_systick.c`, `uart_mcux_lpuart.c`, `adc_mcux_lpadc.c`, `regulator_nxp_vref.c`, `spi_nor.c`;
  - `kernel/fatal.c`, `kernel/busy_wait.c`, `cmake/modules/{dts,snippets}.cmake`, `scripts/kconfig/kconfig.py`, twisterlib (`environment.py`, `twister_main.py`, `testplan.py`, `harness.py`, `hardwaremap.py`, `handlers.py`, `config_parser.py`), `schemas/twister/testsuite-schema.yaml`, pytest-twister-harness (`plugin.py`, `fixtures.py`, `hardware_adapter.py`), `scripts/west_commands/runners/openocd.py`;
  - `subsys/net/lib/config/{Kconfig,CMakeLists.txt,init.c}`, `subsys/net/lib/dhcpv4/Kconfig`, `subsys/mgmt/mcumgr/transport/Kconfig.udp`, `modules/mbedtls/Kconfig.mbedtls`, `drivers/serial/Kconfig.native_pty`, `drivers/ethernet/Kconfig.native_tap`;
  - `west.yml`.
- **HALs and SDK:** hal_stm32 fc11896d (F767 pinctrl, `stm32f767xx.h`, `stm32f7xx_hal_gpio.c` header); hal_nxp 7554bc0f; sdk-ng v1.0.1 and its OpenOCD fork 91bd278a; docker-image v0.29.4 `Dockerfile.ci`.
- **Datasheets and manuals:** UM1974 Rev 8 (Tables 7, 12, 19, 21; §6.4.2, §6.15), MB1137 B-01 schematic, DS11532 Rev 8, UM12018 Rev 2.0, MCXNP184M150F70 Rev 8.2, TPS22918 SLVSD76C, THVD1450, Nexperia 2N7002 / BSS138P, MCP4728 DS22187E, pololu.com/product/2810.
- **LA software:** libsigrok 0.5.2 and master `dreamsourcelab-dslogic` (api.c, protocol.c), DSView `libsigrok4DSL/hardware/DSL/dsl.h`, libsigrokdecode 0.5.3 and master `uart/pd.py`.
- **TLS and broker:** Mbed TLS 4.1.0 (`ssl.h`, `ssl_tls.c`, `ssl_tls13_generic.c`); mosquitto 2.1.2 (man pages, ChangeLog); tshark 4.2.2 (live `-c` test).
- **GitHub docs:** events that trigger workflows (`schedule`, `workflow_dispatch`, `push`), manually running a workflow, workflow syntax (`container.options`), concurrency (`queue`), runner job hooks.
- **Branches:** BACnet 2161be7 / d238d24 (docs/io.md, bacnet.md, management-protocol.md, configuration.md, architecture.md, roadmap.md, hardware.md, prj.conf, Kconfig, sample.yaml, wasm/README.md, firmware sources); MQTT d9cae9e (prj.conf, Kconfig, boards/nucleo_f767zi.conf, CMakeLists, SESSION_NOTES); hub c45c1e0 (SESSION_NOTES); HIL fd8d0f4 (hil/, docs/SESSION_NOTES.md).

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
