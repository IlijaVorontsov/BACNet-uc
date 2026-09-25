# HIL bill of materials

Prices are approximate (±30 %, EUR). It1 is the first purchase; It2/It3 are bought only when their tests are scheduled (see [HIL.md](../HIL.md) §11).

## IT1

| Item | Qty | Purpose | ≈ € | Note |
|---|---|---|---|---|
| NUCLEO-F767ZI (MB1137) | 2 | P1 DUT and stimulus board (B3) | 60-70 | Confirm ownership with the user on day 0 before ordering; buy 1 if a DUT is already owned. DigiKey $34.39 (fetched this session, likely). Prefer silicon rev Z (IDCODE 0x10016451): check at commissioning. |
| W25Q128JV SPI NOR breakout, 3.3 V, /WP and /HOLD tied high | 1-2 | /lfs storage required by the BACnet nucleo_f767zi overlay (SPI1 PA5/PA6/PB5, CS PD14) | 3-6 | Without it the BACnet image has no persistent storage, and PERS-01 cannot run. Buy from an EU-stock distributor (2-3 days); marketplace only for the spare. |
| FX2 CY7C68013A 8-channel 24 MS/s logic analyzer clone (sigrok fx2lafw) | 1 | It1 LA map: nrst, m0, m1, di1, do3, do0, vcp_tx, sync | 8-15 | Connect to a host root port, not the switched hub. EU stock (2-3 days). |
| Pololu 2810 Mini MOSFET Slide Switch with Reverse Voltage Protection, LV | 1 (+1 spare) | E5V power switch driven by the BSS138P inverter (fail-safe = DUT on) | 6-12 | Slide switch must be set to OFF so the ON pin controls it. EU Pololu distributor (2-3 days). |
| 5 V regulated PSU, at least 2 A, with a terminal adapter (+5VA always-on rail) | 1 | Feeds the load switch (DUT E5V) and later the RS-485 bias | 10-15 | E5V must be 4.75-5.25 V and at most 500 mA (UM1974), including the ST-LINK share that flows from E5V while E5V is above VBUS. |
| BSS138P (Nexperia) SOT-23 on SOT-23 adapters | 4 | NRST open-drain driver and power-switch inverter (plus spares) | 5 | VGS(th) 0.9-1.5 V. 2N7002 (1-2.5 V) is the fallback. |
| Passive kit: 1 kOhm x30, 10k, 20k, 33k, 100k, 100R resistors; 100 nF x20, 1 uF capacitors | 1 set | Series resistors on every DUT line, AI nodes, dividers, senses, gate pull-downs | 8-12 | Bourns 4116R-1-102 networks are optional. |
| Perfboards x2, 2.54 mm single male pins, a keyed 2-pin housing for the E5V/GND lead, DuPont jumper wires F-F and F-M (80) | 1 set | Hand-wired harness; single morpho pins soldered on the DUT: CN11-6, CN11-8, CN11-21, CN11-23, CN12-10, CN12-28 | 12-18 | Never populate CN11-5 (VDD) or CN11-7 (BOOT0). The stimulus needs no morpho pins. |
| Powered USB hub from the uhubctl compatibility list | 1 | Per-port power recovery of the stimulus ST-LINK and the FTDI; enough ports | 40-60 | The DUT ST-LINK port is never switched in tests: its MCO is the DUT HSE, and it only powers down together with an E5V cut. |
| Cat6 patch cables | 3 | DUT to host NIC, uplink | 6 |  |
| USB3 GbE NIC, RTL8153 class | 0-1 | Dedicated DUT NIC if the host has only one | 0-15 | R-06 checks that link-down on this NIC is seen as carrier loss by the DUT. |
| USB data cables matching the ports: ST-LINK V2-1 (micro-B, likely) x2 and the FX2 clone (mini-B or micro-B, check the unit) | 3 (+1 spare) | DUT ST-LINK, stimulus ST-LINK, FX2 | 6-10 | Data cables, not charge-only. |
| HIL host: x86 N100 mini-PC, 16 GB, 512 GB SSD, 2x 2.5GbE (Intel i226), Ubuntu 24.04 | 0-1 | pytest/Twister, self-hosted runner, SDK 1.0.1 builds, netns services, tshark, sigrok | 180-250 | 0 if an existing Linux PC with 2 NICs is reused. |
| Assumed owned (confirm with the user): DMM, soldering station, bench PSU with current limit | - | W1/W2 measurements (CN11 check, V_ON, NRST idle, DUT current), morpho soldering | 0 | If a bench PSU is missing, add a Korad KA3005P (It3 item) earlier. |

## IT2

| Item | Qty | Purpose | ≈ € | Note |
|---|---|---|---|---|
| FRDM-MCXN947 | 1 | P2 DUT: network tests first, then the Arduino-header I/O subset | 30-40 | Keep the factory MCU-Link CMSIS-DAP firmware. Never fit J21. |
| DreamSourceLab DSLogic Plus 16 ch (sigrok) | 1 | 16-channel LA map, MS/TP DE timing, stimulus latency calibration | 140 | Buy only after the seller confirms USB ID 2a0e:0020: the 2022 revisions 0x0030/0x0034 are DSView-only and sigrok-cli will not detect them. Otherwise buy the It3 Saleae now. Firmware and bitstreams are extracted from DSView (sigrok-fwextract-dreamsourcelab-dslogic). 256 Mbit buffer: every capture declares rate and length. |
| FTDI USB-RS485-WE-1800-BT cable | 1 | mstpcap sniffer and host MS/TP node (MAC 7); built-in 120 R as bus end | 25-30 | Gated on FW-07 acceptance (D34). udev latency_timer=1 and ID_MM_DEVICE_IGNORE. |
| TI THVD1450 on SOIC-8 adapters, with 10k DE pull-downs, 10k RO pull-ups and 100 nF | 4 | U1 DUT (VCC = DUT 3V3), U2 injector and U3 listen-only sniffer (VCC = stimulus 3V3), spare | 15-20 | Gated (D34). Each RO pull-up goes to its own VCC. Any module used must have its pull-ups, 120 R and bias removed. |
| MS/TP bus kit: 3-6 m CAT5e, WAGO 221, 120 R 1% x2, 510 R 1% x2, 10k; tap dividers 100k 0.1% x4 + 100 nF x2 | 1 set | Daisy-chain bench bus; bias from +5VA at one point (278 mV idle); rs485_a/rs485_b taps on PC2/PA6 for MSTP-01 | 15-20 | Gated (D34). The tap dividers are mandatory (analog, non-FT stimulus pins). |
| MCP4728 4-ch 12-bit I2C DAC module | 2 | #1 on I2C1 drives P1 ai1/ai2/ai4/ai5; #2 on I2C2 (PF1 SCL CN9-19, PF0 SDA CN9-21) drives the P2 ai0-ai2 sources via the shield (0.05-2.0 V) | 12-16 | VDD = stimulus 3V3, LDAC to GND, internal 2.048 V reference. Both keep address 0x60 on separate buses. |
| ADS1115 16-bit ADC module | 2 | Independent reference at the DUT AI nodes, DUT AVDD, DUT 3V3 | 10 | PGA +-4.096 V for P1, +-2.048 V for P2. 10 kOhm in series with every input tied to a DUT net (limits back-feed into the stimulus 3V3 when the stimulus is off); the resulting gain error is calibrated once against a DMM and stored in bench.yml. |
| INA226 current/voltage monitor, 0.1 Ohm shunt | 1 | DUT E5V current signature (PWR-03), off-state check | 5 | The signature includes the ST-LINK share from E5V; PWR-03 records VBUS and E5V. |
| TI TPS22918DBV load switch on SOT-23-6 adapters, CT 1 nF, 100 R QOD resistor | 2 | Sharp, repeatable E5V cuts for PERS-02 and OTA-04 (quick output discharge) | 5 | Same BSS138P inverter; ON 100k to +5VA. tON ~1.95 ms, tR ~2.5 ms at 5 V with CT 1 nF. |
| Arduino-R3 proto shield, stacking single-row headers, 2.2 kOhm networks, 2-pin shunts and 3-pin selectors, 2x20 IDC cable | 1 set | One harness for P1 and P2 with profile shunts (analog-source selectors on A2-A5, P2 A5 GPIO path, D5 open on P2, D10-D13 open on P1, D0/D1/D14/D15 always open) | 15-20 | Outer rows only. |
| Managed switch with port mirroring (TP-Link TL-SG105E or Netgear GS305E) and a second USB3 NIC | 1 + 1 | Independent mirror capture, port-disable link flap | 40-55 | Storm control off on the DUT port. |

## IT3

| Item | Qty | Purpose | ≈ € | Note |
|---|---|---|---|---|
| Saleae Logic Pro 16 | 1 | Hardware triggers, analog on every channel (RS-485 A/B levels, MSTP-02) | 1000-1450 | logic2-automation 1.0.11. Moves to It2 if no DSLogic with PID 0x0020 is available. |
| Nordic PPK2 | 1 | Brown-out ramps on JP5 and current profiling | 95-110 |  |
| Programmable PSU with serial control (Korad KA3005P class) | 1 | Slow E5V ramps and dips (PWR-04) | 100-150 |  |
| Relay fault box: 6x reed relay, 1x DPDT, ULN2803A, perfboard | 1 | Termination, bias, open D+/D-, short, A/B swap under stimulus control (MSTP-26) | 30 | Driven from 7 of the listed free stimulus pins. NC contacts for the terminators K1/K2 and the bias K3, NO contacts for the faults K4-K7, so a de-energized box (stimulus in reset) is the normal bus. |
| KiCad HIL interposer PCB, 5 pcs + connectors | 1 batch | Reproducible wiring once the board set is frozen | 30-40 | Only after the It2 shield has been proven. |
| Spare NUCLEO-F767ZI | 1 | 24 h soak and destructive OTA/PERS tests without blocking the main bench | 30-35 | Own NIC, MAC, instance and client id. |
| Raspberry Pi Pico 2 with bacnet-stack ports/pico (patched MAC and baud) | 1 | Functional token-ring peer only (never a timing reference) | 6 | Characterised first (R-10). |
| Commercial BACnet MS/TP device or router (used) | 1 | Independent-stack interop peer | 50-500 |  |
| NUCLEO-H563ZI | 0-1 | Optional P3 MQTT-only profile | 40 | Flash with stm32cubeprogrammer or pyocd. |
| 4-channel oscilloscope (Rigol DHO804 class) | 0-1 | Differential drive level (MSTP-02), DE glitches, power ramps | 350-500 | Skip if already owned or if the Saleae analog channels suffice. |
| Isolated RS-485 node (ADM2587E or ISO1410) + isolated supply; 100-300 m cable spool | 1 + 1 | Common-mode range and termination/reflection tests | 80-180 |  |
| Yepkit YKUSH3 | 0-1 | Switch VBUS and data to hard-disconnect a wedged probe | 125 | Only if uhubctl recovery proves insufficient. |
