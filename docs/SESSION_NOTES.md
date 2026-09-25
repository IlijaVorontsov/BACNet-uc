# Session notes: HIL rig (session `claude/hardware-in-loop-testing-x74tww`)

The HIL session builds a hardware-in-the-loop rig that runs your firmware on real boards:
- a host PC;
- a NUCLEO-F767ZI DUT;
- a second NUCLEO-F767ZI as the stimulus board;
- a logic analyzer;
- network namespaces with dnsmasq, mosquitto and bacnet-stack tools.

The full design is `docs/HIL.md` on the HIL branch. This section lists what the rig needs from you, what you must not break, and how to run it. (It replaces the old MQTT notes that were copied to the HIL branch by mistake.)

### Platform (same as yours)
- Zephyr v4.4.2, SDK 1.0.1, Python 3.12. We build from a workspace whose manifest is your branch checkout (your west.yml, zephyr/module.yml and modules/). Our survey script fails if our copies drift from the BACnet tip.
- Profiles:
  - P1 `nucleo_f767zi` (canonical);
  - P2 `frdm_mcxn947/mcxn947/cpu0` (It2, network tests first);
  - P3 `nucleo_h563zi` (optional, MQTT only).

### Two tiers, both first-class
- **release**: your unmodified artifact, driven only over Ethernet (BACnet/IP, MQTT, SMP, DHCP) and by the stimulus pins. The only additions are site settings, and a build check enforces that nothing else differs:
  - BACnet: the documented DTCM app-pool workaround until FW-04 lands; in It2 also your own MCUboot sysbuild build for the OTA tests;
  - mqtt_tls: `APP_MQTT_BROKER_HOSTNAME="broker.hil.lan"` and the CA file; an mTLS variant adds the client-cert files.
- **instrumented**: `-S hil` (both apps) and `-S hil-io` (BACnet only).
  - `lib/hil` is added as an extra Zephyr module and the snippets come from `-DSNIPPET_ROOT`, so **nothing changes in your branches**.
  - `lib/hil` prints `HIL-BOOT` and `HIL-READY` lines, drives marker pins PG0-PG3 and adds a `hil` shell (info, mark, panic).

### Contract v0: what we need from you (It1, 6 items)
| ID | Owner | Item | Today |
|---|---|---|---|
| FW-01 | both | Keep the rig pins on nucleo_f767zi free: PD4/PD5/PD6 (USART2), PG0-PG3, PE10/PE12/PE14/PE15, NRST, E5V. Console stays on USART3 at 115200. Keep the 17 catalog channels in `firmware/boards/io/nucleo_f767zi.dtsi` in order, **append only** (ids are devicetree order). | holds |
| FW-02 | both | No forced MAC (keep 02:80:E1 + crc32 of the UID). DHCPv4 on. BACnet default instance 260001, overridable by device.json. MQTT client id `z`+base32(UID). MQTT info keeps hwid/mac/fw. | holds |
| FW-03 | both | Keep these log lines stable: `formatting /lfs` (our persistence tests fail on it), and the banners `BACnet-uc <ver> on <board>` and `MQTT over Ethernet + TLS on <board>`. Nothing else is frozen. | holds |
| FW-04 | BACnet | The default nucleo_f767zi build must link (RAM overflow of 29,564 B today). Until then the rig uses your documented DTCM app-pool workaround, which links (we built it: 494,444 B flash, 291,708 B RAM); please keep it valid. | fails |
| FW-05 | both | Never halt on a fatal error. Either override `k_sys_fatal_error_handler()` to `sys_reboot(SYS_REBOOT_COLD)`, or keep a hardware watchdog armed (the 4.4.2 default handler halts). | MQTT ok (IWDG), BACnet missing |
| FW-06 | both | Before pushing, announce in your SESSION_NOTES any change to: nucleo_f767zi overlays or confs, partitions or MCUboot, the FW-03 lines, default ports (47808, 1337, 8883), the SMP/MQTT interfaces, the `APP_MQTT_*` site symbols we set, the `schemas/{device,io,apps}.schema.json` fields we push, or the `bacnet_uc_harness.smp` client API we reuse. | informal |

The contract is negotiable. Please answer under this heading on your branch ("FW-0x: accepted / declined because ... / changed to ..."). We read your notes before every nightly triage.

### Coming in It2/It3 (proposals, not yet requests)
- **BACnet:**
  - FW-07: MS/TP on USART2 with hardware DE (TX PD5 CN9-6, RX PD6 CN9-4, DE PD4 CN9-8, `de-enable`, 16/16). Your roadmap has USART6 + GPIO DE on PD15. The F7 USART6 DE pins are morpho-only, and GPIO DE timing is exactly what our MSTP tests measure. We can wire either. **We build the MS/TP bench only once you accept FW-07 and start Phase 3**, so please tell us when.
  - FW-08: factory reset over SMP and the console.
  - FW-09: hwid/mac in `uc_node info`, and catalog paging. The 17-channel catalog exceeds the 1024-B SMP buffer (hub B6).
  - FW-10: SMP security posture for release builds. If you choose DTLS, plain SMP stops, so please also name a test PSK or certificate we may embed.
  - FW-11: MCUboot keys and layout for HIL-signed images.
  - FW-14: MCXN VREF mode, ADC clock and AI range.
  - It3: FW-17/18 MS/TP timing and statistics, FW-19 DCC/Reinit passwords, FW-21 hub B1 identify and B2 force lease, FW-22 a wall-clock time base for TimeSync.
- **MQTT:** FW-12, a certificate time-validation policy (4.4.2 names: `SNTP`, `NET_CONFIG_CLOCK_SNTP_INIT`, `NET_DHCPV4_OPTION_NTP_SERVER`, `NET_CONFIG_SNTP_INIT_SERVER_USE_DHCPV4_OPTION`, `MBEDTLS_HAVE_TIME_DATE`). Note: these symbols alone do not sync the clock in mqtt_tls. Either call `net_init_clock_via_sntp()` after the lease (declare the extern yourself), or adopt `NET_CONFIG_SETTINGS` + `NET_CONFIG_AUTO_INIT` and drop `APP_DHCPV4`.
- **Both:**
  - FW-13: a native_sim TAP variant for BACnet broadcast in SIL;
  - FW-16: optional weak `hil_event()` hooks for triggering power-cut and reset tests.

### Wiring on the DUT you must not break (P1, NUCLEO-F767ZI)
- **Catalog pins in use:**
  - di0 PC13, di1 PF15, di2 PF14;
  - do0-do4 PB0/PB7/PB14/PF13/PF12;
  - ai0-ai5 PA3/PC0/PC3/PF3/PF5/PF10;
  - ao0-ao2 PE9/PE11/PE13 (1 kHz TIM1).
- **Rig pins:**
  - NRST at CN8-5, open drain from the stimulus;
  - E5V at CN11-6 with GND at CN11-8 (JP3 = 1-2, JP1 off): the rig power-cycles the board there, and the ST-LINK stays on USB. Only these two CN11 power pins are populated; CN11-5 (VDD) and CN11-7 (BOOT0) are never wired;
  - 3V3 is sensed at CN8-7.
- **Storage:** the W25Q128JV NOR on SPI1 (PA5/PA6/PB5/PD14) as in your overlay. Flashing never erases it, so the rig restores a known state by pushing device.json, io.json and apps.json over SMP at session start.
- **Keep-out:** RMII pins, PD8/PD9, USB, SWD/SWO, the clock pins. Never wire CN7-14 (D11 = PA7 = CRS_DV).

### What the rig tests in It1 (23 tests)
| Group | Tests |
|---|---|
| Rig self-tests | R-01 (stimulus link), R-02a (stimulus-side wiring, every session), R-02b (DUT-side wiring over SMP, BACnet sessions), R-04, R-06, R-07 (flash guard) |
| System and power | SYS-01 (flash, DHCP, I-Am/CONNACK), RST-01 (NRST), RST-04 (fatal recovery), PWR-01 (power cycle), PWR-02 (back-feed and fail-safe) |
| MQTT and TLS | MQTT-01..03, TLS-01 (TLS 1.3 and 1.2 certificate negatives), TLS-03 (mTLS, on the mTLS site build) |
| Network, BACnet, I/O | NET-03 (link flap), BIP-01 (Who-Is/I-Am), BIP-03 (Device RP), IO-01 (BO → pin), IO-04 (pin → BI and debounce) |
| Persistence and release guard | PERS-01 (config survives power cycles), SEC-01 (release artifact guard) |

15 of the 23 run on the release artifact. The I/O pass criteria come from your docs/io.md: edge → PV ≤ sample + debounce + 5 ms, and write → pin ≤ sample + 5 ms. Timing bounds are asserted in It2, after the clock fit. BACnet and MQTT criteria run on their own images; tests that apply to both are parametrised by image.

### How to run (on the HIL host)
```sh
python3.12 -m venv ~/.venvs/hil && . ~/.venvs/hil/bin/activate && pip install -e hil
sudo hil/host/install-net-wrappers.sh                                   # once per host
# builds (FW = your checkout used as the west manifest repo, HIL = the HIL checkout)
west build -b nucleo_f767zi $FW/firmware -d b/p1-bac-rel -- \
  -DEXTRA_DTC_OVERLAY_FILE=$HIL/hil/site/f767-app-pool.overlay -DCONFIG_UC_APP_POOL_SIZE=98304
west build -b nucleo_f767zi $MQ/apps/mqtt_tls -d b/p1-mq-rel -- -DEXTRA_CONF_FILE=$HIL/hil/site/mqtt-site-hil.conf
west build ... -S hil [-S hil-io] -- -DSNIPPET_ROOT=$HIL -DZEPHYR_EXTRA_MODULES=$HIL/lib/hil   # instrumented
# flash: ALWAYS select the probe (west flash --serial does not select it on this board)
west flash -d b/p1-bac-rel -r openocd -- --cmd-pre-init "adapter serial $DUT_SN"
# tests
sudo -E "$(command -v pytest)" --hil --bench /etc/hil/bench1/bench.yml -m "rig or release" hil/tests
sudo -E "$(command -v pytest)" --sil hil/tests/sil                     # no hardware
# Twister (CI): every west twister call needs --allow-installed-plugin (the harness plugin is pip-installed)
```
- Artifacts go to `/tmp/hil-artifacts/<UTC>`: pcapng files with TLS keys injected, stim.log, service logs and rig reports.
- CI runs only on the self-hosted HIL host. A push to the HIL branch builds and runs unit and SIL tests, but never touches the bench. The nightly hardware run starts from a host timer that pushes to the machine branch `hil/nightly` and tests your branch tips. Your pushes trigger nothing and use no hosted minutes.

### Findings for you (read this session)
- BACnet:
  - `snippets/uc-ramfs/snippet.yml` has no `boards:` section, so its `boards/*.overlay` files are never applied.
  - The Zephyr board DTS declares B1 (PC13) active-low, but pressed = VDD. Your catalog's ACTIVE_HIGH is right.
  - IO- and application-owned objects can be deleted by a network DeleteObject (your docs say so); our ROB-05 records it as xfail until you fix it.
- MCXN catalog:
  - no `regulator-initial-mode` on `&vref`, while the Zephyr adc_dt sample uses HIGH_POWER;
  - ADC0 is clocked at 48 MHz with power-level 0, where the datasheet allows at most 24 MHz.
- MQTT: your `modules/wasm-micro-runtime` still has `WAMR_OS_THREAD_STACKS`, which BACnet 2161be7 removed.
- The 4.4.2 default fatal handler halts (kernel/fatal.c), so a BACnet crash needs a manual power cycle today.
