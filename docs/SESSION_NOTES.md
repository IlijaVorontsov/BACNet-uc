# Session notes: HIL rig (session `claude/hardware-in-loop-testing-x74tww`)

Design **v2.2** (2026-09-25, reviewed) applies the user decisions below. The full design is `docs/HIL.md` on the HIL branch. After the D45 integration it moves to `docs/hil/SESSION_NOTES.md` on `main`, and main becomes the single source for HIL work.

### Decisions from the user (2026-09-25, read first)

- **Boards the user owns:** FRDM-MCXN947, FRDM-MCXA156, FRDM-MCXN236, FRDM-MCXA153, nRF54LM20 DK and several nRF54L15 DKs. No NUCLEO-F767ZI is owned. Consequences:
  - the **canonical DUT is the FRDM-MCXN947** (profile **P1**, `frdm_mcxn947/mcxn947/cpu0`);
  - the **stimulus is the FRDM-MCXN236**;
  - the F767ZI stays documented as the optional profile **P1-F767**. Nothing is bought for it.
- **MS/TP UART = the planned one** in BACnet `docs/hardware.md` §5: the Arduino UART, with DE/RE on a GPIO.
  - MCXN947: LPUART2 TX P4_2 D1, RX P4_3 D0, DE P0_24 D11.
  - F767ZI: USART6 TX PG14 D1, RX PG9 D0, DE PD15 D9.
  - This replaces our USART2 hardware-DE proposal. Our MS/TP tests will measure your software DE timing at 76 800 and 115 200 bit/s, which is the roadmap 3.1 open question.
- **`main` exists** at e62a095 (the BACnet integration commit, where your CI runs).
  - The repo default branch is still the MQTT branch until the user switches it.
  - main shares **no git history** with the HIL or MQTT branches, so a branch PR cannot merge them. We integrate through a branch cut from main that carries only HIL-owned paths (D45). After that merge, HIL work continues on branches cut from main, and the old HIL branch is frozen.

### What the rig is
- A host PC (network namespaces with dnsmasq, mosquitto 2.1.2 and the bacnet-stack tools).
- The **FRDM-MCXN947 DUT**.
- An **FRDM-MCXN236 stimulus** running `apps/hil_stimulus`: shell `stim`, protocol v0.
- An FX2 logic analyzer.
- In It2: an RS-485 bench bus with FRDM-MCXA153 (MAC 3) and FRDM-MCXA156 (MAC 4) as MS/TP peers, and an optional nRF54L15 DK as an edge-timing probe.

### Platform (same as yours)
- Zephyr v4.4.2, SDK 1.0.1, Python 3.12. We build from a workspace whose manifest is your branch checkout. `survey.sh` also compares each branch against `main`.
- Profiles:
  - P1 `frdm_mcxn947/mcxn947/cpu0` (canonical);
  - P1-F767 `nucleo_f767zi` (optional);
  - P3 `nucleo_h563zi` (optional, MQTT only).
  - P2 is retired.
- Flashing on P1: our wrapper `hil/tools/hil-flash --probe <full MCU-Link serial>`, which runs `west flash -r linkserver --probe`. Note: the 4.4.2 linkserver runner **accepts and ignores `-i/--dev-id`**, and without `--probe` it flashes probe #1, so the wrapper refuses both. Fallback: pyocd with the NXP.MCXN947_DFP 26.06.00 pack. We never use `--erase` on the MCXN947 (the runner's erase path skips the board's flash overrides), and never blhost/ISP, J21, `//cpu0/qspi` or `/ns`.

### Two tiers, both first-class
- **release:** your unmodified artifact, driven only over Ethernet and by the stimulus pins.
  - BACnet: **no additions** (FW-04 met; the old DTCM workaround overlay is retired). We build **your branch tip** (4ee098e today) until main has the storage shrink, because main still maps the whole W25Q64. In It2 we also use your `bacnet_uc.firmware.mcuboot.mcxn947` scenario.
  - mqtt_tls: `APP_MQTT_BROKER_HOSTNAME="broker.hil.lan"` and the CA file. The mTLS variant adds the client-cert files. The MCUboot variant is `app.mqtt_tls.mcuboot`.
  - At session start the rig factory-resets the MQTT stored settings (SMP `mqtt/factory_reset`), so the site Kconfig values apply.
- **instrumented:** `-S hil`, with `lib/hil` as an extra module and snippets from `-DSNIPPET_ROOT`. **Nothing changes in your branches.** On the MCXN947 the snippet:
  - puts markers m0-m3 on D12, D13, SDA, SCL (P0_26, P0_25, P4_0, P4_1);
  - publishes the MS/TP UART as alias `mstp-uart0` (LPUART2, 38400) and the DE pin as `/zephyr,user mstp-de-gpios` (P0_24);
  - disables `dac0` and `flexcomm1_lpspi1`, and **keeps `flexcomm2_lpi2c2` okay** (see the findings below).
  - Built this session with BACnet 4ee098e (511,676 B / 358,752 B; plain 509,808 B / 358,680 B) and mqtt_tls e859980 (315,144 B / 175,144 B), 0 warnings with warnings as errors.
  - `-S hil-io` is F767-only.

### Contract v0: what we need from you (It1)

| ID | Owner | Item | Today |
|---|---|---|---|
| FW-01 | both | Keep the rig pins on frdm_mcxn947 free: P4_2/P4_3 (D1/D0), P0_24 (D11), P0_25/P0_26 (D13/D12), P4_0/P4_1 (SDA/SCL); RESET_B and J24. **Keep FC2 in combined mode** (lpuart2 and lpi2c2 both okay) and CONFIG_I2C/SPI/DAC off on this board; announce first if you need one. Console LPUART4 at 115200. Keep the 15 catalog channels in `firmware/boards/io/frdm_mcxn947_mcxn947_cpu0.dtsi` in order, **append only**. F767 (optional): PG0-PG3, PG9/PG14/PD15, PE10/12/14/15, NRST, E5V; 17 channels, append only. | holds (MQTT accepted) |
| FW-02 | both | MCXN947: keep `nxp,unique-mac` (MAC AE:9A:22 + CRC-24 of the 16-byte UID), DHCPv4 on, BACnet default instance 260001, MQTT client id `z`+base32(UID), MQTT info keeps hwid/mac/fw. | holds (MQTT accepted) |
| FW-03 | both | Keep `formatting /lfs`, `BACnet-uc <ver> on <board>` and `MQTT over Ethernet + TLS on <board>` stable. | holds (MQTT accepted) |
| FW-04 | BACnet | Default builds must link. **Met at e62a095**; please keep it that way (MCXN947 RAM is at 91 %). | met |
| FW-05 | both | Never halt on a fatal error: WWDT0 through task_wdt, or a fatal handler that reboots. | MQTT ok (WWDT). **BACnet missing.** |
| FW-06 | both | Before pushing, announce changes to: board overlays and confs, partitions (FW-23) or MCUboot, the FW-03 lines, default ports (47808, 1337, 8883), SMP and MQTT interfaces, the `APP_MQTT_*` site symbols, the schema fields we push, the `bacnet_uc_harness.smp` API, and **FC2 mode, CONFIG_I2C/SPI/DAC or MS/TP pins on the MCXN947**. | MQTT accepted; BACnet informal |

Please answer under this heading on your branch ("FW-0x: accepted / declined because ... / changed to ..."). We read your notes before every nightly triage.

### Coming in It2/It3
- **BACnet:**
  - **FW-07 (rewritten): MS/TP on the planned UART.** When Phase 3 lands, please adopt the alias `mstp-uart0` and `/zephyr,user mstp-de-gpios` (or define a `uc,mstp` binding and tell us), and announce your DE control method. Before that, our rig already checks that your DE pad stays low while the board is in reset, booting or unpowered (MSTP-22a). We can also run an early DE-timing measurement on the DUT with a HIL-owned test image (D47), but only if you agree, so we don't duplicate your port.
  - FW-08: factory reset (the MQTT part is done).
  - FW-09: hwid/mac in `uc_node info` and catalog paging.
  - FW-10: SMP security posture.
  - FW-11: MCUboot keys and layout for HIL-signed images.
  - FW-14: MCXN947 VREF mode, ADC clock and AI range. This is now the canonical board.
  - **FW-23: keep `storage_partition` at or below 0x7EFFFF.** Done on your branch at 6d1a151 (and documented at 4ee098e); please get it into main. Until then our partition check only warns on main.
  - It3: FW-17/18, FW-21, FW-22. **FW-19 is met** (DCC/Reinit password; Create/Delete off by default).
- **MQTT:**
  - FW-12 is deferred with M7 (your answer).
  - M4-M6 are noted: SMP on UDP 1337 and settings at 0x7F0000 (FW-23).
  - The 57orta session's M4-M7 reconciliation matches our contract.
- **Both:** FW-13 native_sim TAP; FW-16 weak `hil_event()` hooks (PERS-02 uses the marker as the reference edge of a host-timed power cut).

### Wiring on the DUT you must not break (P1, FRDM-MCXN947)
- **Catalog pins in use:**
  - di0 P0_23 (A5, SW2), di1 P0_6 (SW3, never wired), di2 P0_29 (D2), di3 P0_30 (D4);
  - do0-do4 P0_10/P0_27/P1_2/P0_31/P0_28 (D9/D10/D6/D7/D8);
  - ai0-ai2 P0_14/P0_22/P0_15 (A2-A4, 0-1.8 V);
  - ao0 P2_6 (J3-15), ao1 P2_7 (J3-13), ao2 P1_23 (D3).
- **Rig pins:**
  - markers D12/D13/SDA/SCL;
  - MS/TP D1/D0/D11;
  - RESET_B at J3-6: driven open drain from the stimulus, and sensed only through a FET gate. Its 0.1 µF means release takes 4-9 ms.
  - **J24**: its shunt is replaced by a TPS22918 load switch that cuts only the MCU rail. The MCU-Link, PHY and NOR stay powered. It is used only if the week-1 back-feed test passes (< 3 mA).
  - Full-board cuts go through the USB hub port on J17. J11, 5V and VIN are never connected.
  - Senses at AREF (switched VDD_ANA) and J3-8 (P3V3).
- **Storage:** the on-board W25Q64. BACnet `/lfs` 0x000000-0x7EFFFF, MQTT settings 0x7F0000-0x7FFFFF. Flashing never touches it.
- **Keep-out:** D5 (P1_21 ENET MDIO), RMII, SWD/SWO P0_0-P0_3, FlexSPI P3_x, console P1_8/P1_9, SW3/P0_6, CAN P1_10/P1_11. R154 is never populated.

### What the rig tests in It1 (23 tests)

| Group | Tests |
|---|---|
| Rig self-tests | R-01 (stimulus link), R-02a (stimulus-side wiring, every session), R-02b (DUT-side wiring over SMP, BACnet sessions), R-04, R-06, R-07 (flash guard and identity) |
| System and power | SYS-01, RST-01 (NRST), RST-04 (fatal recovery), PWR-01 (power cycle: MCU rail after the W1 back-feed check, else USB port), PWR-02 |
| MQTT and TLS | MQTT-01..03, TLS-01, TLS-03 |
| Network, BACnet, I/O | NET-03, BIP-01, BIP-03, IO-01 (BO → do3), IO-04 (di2 → BI and debounce) |
| Persistence and release guard | PERS-01, SEC-01 |

15 of the 23 run on the release artifact. The I/O criteria come from your docs/io.md.

### How to run (on the HIL host)
```sh
python3.12 -m venv ~/.venvs/hil && . ~/.venvs/hil/bin/activate && pip install -e hil
sudo hil/host/install-net-wrappers.sh                                   # once per host
# builds (FW = your checkout used as the west manifest repo, HIL = the HIL checkout)
west build -b frdm_mcxn947/mcxn947/cpu0 $FW/firmware -d b/p1-bac-rel
west build -b frdm_mcxn947/mcxn947/cpu0 $MQ/apps/mqtt_tls -d b/p1-mq-rel -- \
  -DEXTRA_CONF_FILE=$HIL/hil/site/mqtt-site-hil.conf
west build ... -S hil -- -DSNIPPET_ROOT=$HIL -DZEPHYR_EXTRA_MODULES=$HIL/lib/hil   # instrumented
west build -b frdm_mcxn236 $HIL/apps/hil_stimulus -d b/stim                     # stimulus
# flash: ALWAYS by full probe serial through the wrapper; never -i, #N or --erase
hil/tools/mcxn_flash_guard.py b/p1-bac-rel && hil/tools/hil-flash -d b/p1-bac-rel --probe $DUT_SN
# tests
sudo -E "$(command -v pytest)" --hil --bench /etc/hil/bench1/bench.yml -m "rig or release" hil/tests
sudo -E "$(command -v pytest)" --sil hil/tests/sil                     # no hardware
# Twister (CI): every west twister call needs --allow-installed-plugin
```
- Artifacts go to `/tmp/hil-artifacts/<UTC>`.
- CI runs only on the self-hosted HIL host, and uses no hosted minutes. Until the D45 merge, your pushes trigger nothing from us, and the nightly is a push to `hil/nightly`. Once `hil.yml` is on main:
  - pushes to `main` that touch HIL paths or `firmware/boards/**` (and same-repo PRs into main, if we enable that) run our build job on the HIL host;
  - it is never a required check;
  - while the host is offline the job waits in the queue, and GitHub fails it after about 24 h [likely];
  - pushes to your own branches still trigger nothing.

### Findings for you (read this session)
- **FC2 on the MCXN947:** `mfd_nxp_lp_flexcomm.c` picks the combined LPI2C+LPUART mode from the devicetree status of *both* children. If you ever disable `flexcomm2_lpi2c2`, LPUART2 leaves D0/D1. The board DTS also enables `dac0` on P4_2 (= MS/TP TX) and `flexcomm1_lpspi1` on P0_24-P0_27 (= DE, markers, do1). Neither driver is built today.
- **The UART pins are driven from boot:** both apps instantiate LPUART2 at boot, so D1 is actively driven from reset release.
- **UM12018** notes a potential "MCU-Link UART" conflict on D0/D1. We check it in W1 and will tell you.
- **Flashing:** the linkserver runner ignores `-i` and defaults to probe #1; `--erase` does not apply the board's flash overrides (see Platform).
- **Storage:** the shrink is on your branch (6d1a151, documented at 4ee098e) but not on main; the e62a095 build still maps the whole chip.
- **Watchdog:** BACnet still has none (FW-05). A crash needs a power cycle, which the rig can do, but the nightly flags it.
- **MCXN947 analog:** the catalog still leaves `&vref` mode and the `lpadc0` power level unset (48 MHz ADC clock) (FW-14).
- **Board DTS vs catalog:** the board `gpio-leds`/`gpio-keys` nodes share pins with do0/do1/di0. That is fine as long as no LED or INPUT driver is built.
- **MQTT:** `main` and your branch have unrelated histories (same as ours).