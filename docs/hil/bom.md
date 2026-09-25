# HIL bill of materials (v2.2)

Prices are approximate (±30 %, EUR). It1 is the first purchase. It2 and It3 items are bought only when their tests are scheduled (HIL.md §11).

**v2.2 buys no boards.** The user owns every board the rig needs (D46).

## Owned: no purchase

| Board | Qty | Role |
|---|---|---|
| FRDM-MCXN947 | 1 | P1 DUT (canonical) |
| FRDM-MCXN236 | 1 | Stimulus (`apps/hil_stimulus`, profile P1) |
| FRDM-MCXA156 | 1 | Fallback stimulus; otherwise MS/TP peer, MAC 4 (It2) |
| FRDM-MCXA153 | 1 | MS/TP peer, MAC 3 (It2) |
| nRF54L15 DK | several | #1: precision edge probe (It2, optional; set VDD to 3.3 V and disconnect the UART1 RTS/CTS group first). The rest are spares. |
| nRF54LM20 DK | 1 | Alternative or bus-side edge probe (It3) |

## IT1 (buy now, from EU-stock distributors, 2-3 day delivery)

| # | Item | Qty | Purpose | ≈ € | Note |
|---|---|---|---|---|---|
| 1 | FX2 CY7C68013A 8-channel 24 MS/s logic analyzer clone (sigrok fx2lafw) | 1 | It1 LA map: nrst, m0, m1, di2, do3, do0, vcp_tx, sync | 8-15 | Connect to a host root port, not to the switched hub. |
| 2 | MCP4728 4-channel 12-bit I2C DAC module | 1 (+1 spare) | Sources for ai0-ai2, since the MCXN236 has no DAC (D41). Also needed for R-02a. | 6-12 | Wired to the stimulus mikroBUS J5-5/J5-6 (SCL/SDA, LPI2C2) and J5-8 (GND). **VDD 3.3 V from J6-7 (VDD_BOARD), never J5-7 (P5V0 = 5 V).** The module's I2C pull-ups must go to that 3.3 V or be removed. LDAC to GND, address 0x60. Internal 2.048 V reference at gain 2. |
| 3 | TI TPS22918DBV load switch on SOT-23-6 adapters | 2 | Fail-safe MCU-rail switch in place of the J24 shunt (D39), plus a spare | 4-8 | QOD tied to VOUT, CT 1 nF, 1 µF at VIN. ON pulled up with 100 k to P3V3. |
| 4 | BSS138P (Nexperia) SOT-23 on adapters; 2N7002 acceptable | 6 | Q1 PWR_CTL inverter, Q2 NRST open-drain driver, Q3 RESET_B gate sense (D50), 3 spares | 5 | VGS(th) 0.9-1.5 V (2N7002: 1-2.5 V) |
| 5 | Passive kit | 1 set | Series resistors (D40), analog nodes, dividers, gate networks, the switch, the Q3 sense (1 kΩ gate, 10 kΩ drain pull-up); the 22 Ω is the W1 back-feed shunt | 10-15 | Resistors: 2.2 kΩ ×40 (or 8-way SIP networks), 10 kΩ ×20, 100 kΩ ×20, 4.7 kΩ ×10, 1 kΩ ×10, 100 Ω ×5, 22 Ω ×2. Capacitors: 100 nF ×20, 1 nF ×4, 1 µF ×4. |
| 6 | Component-free Arduino-R3 proto shields with stacking headers | 2 | DUT shield and stimulus shield (pin tables §6) | 10-16 | No power LED, reset button or ICSP header. Clip the 5V (J3-10) and VIN (J3-16) stacking pins on both. Outer rows only, plus pigtails. |
| 7 | 2×20 IDC ribbon (20-30 cm) with 2 box headers; DuPont F-F/F-M wires ×40; single-row male pins; one keyed 2-pin housing | 1 set | Shield-to-shield ribbon; pigtails to J3-13/J3-15, J3-3, J3-7, J2-1..7, J1-5..15; keyed flying leads to J24 | 8-12 | |
| 8 | Powered USB hub (7+ ports) from the uhubctl compatibility list | 1 | `usb_port` power cuts of the DUT (J17), stimulus recovery; It2 probe, FTDI and peers | 40-60 | Port P1: the DUT MCU-Link. J11 is never connected. |
| 9 | USB data cables to match the hub (A-to-C or C-to-C), plus the FX2's cable | 3 (+1) | DUT J17, stimulus MCU-Link, spare, FX2 | 8-15 | Data cables, not charge-only. FRDM MCU-Link ports are USB-C. |
| 10 | Cat6 patch cables | 2-3 | DUT to host NIC, uplink | 6 | |
| 11 | USB3 GbE NIC, RTL8153 class | 0-1 | Dedicated DUT NIC, only if the host has one NIC | 0-15 | |
| 12 | HIL host: x86 N100 mini-PC, 16 GB, 2× i226, Ubuntu 24.04 | 0-1 | pytest/Twister, runner, builds, netns | 180-250 | 0 if an existing Linux PC with 2 NICs is reused |
| – | Assumed owned (confirm on day 0): DMM, soldering station, bench PSU with current limit, oscilloscope (the FX2 can stand in during W1) | – | W1/W2 measurements: J24 continuity and back-feed, RESET_B, V_ON, VDD_ANA | 0 | |

**Total without the host: about €105-180. With the host: about €285-430.**

**Dropped from the v2.1 It1 list:**
- 2× NUCLEO-F767ZI: P1-F767 is an optional purchase.
- W25Q128JV breakout: the MCXN947 has an on-board W25Q64.
- Pololu 2810, and the E5V keyed housing and morpho pins.
- 5 V PSU: moves to It2, for the RS-485 bias only.

## IT2 changes

- **MS/TP parts, no longer gated for rig work (D47), about €65-90 in total:**
  - 3.3 V RS-485 transceivers (THVD1450 on SOIC-8 adapters, or modules with their pull-ups, 120 Ω and bias removed), ×6: U1 (DUT), U2 (injector), U3 (sniffer), 2 peers, 1 spare. €15-25.
  - Bus kit: 3-6 m CAT5e, 120 Ω ×2, 510 Ω ×2, WAGO, 100k 0.1 % dividers ×4 (with 100 nF at the midpoints). €15-20.
  - 5 V ≥ 1 A PSU for the bias (+5VA). €10-15.
  - FTDI USB-RS485-WE, 1 (host node MAC 7, mstpcap, R-08). €25-30. Insulate its red VCC (5 V) wire and the terminator wires.
- **USB cables** for the two MCXA peers (USB-C) and the nRF DK, ×3. €8-12.
- **Unchanged items:** DSLogic Plus (only as PID 0x0020, else a Saleae); managed switch with mirroring, plus a second NIC.
- **Changed purpose:** INA226 now measures the MCU-rail current between the TPS22918 VOUT and J24 pin B. ADS1115 becomes an optional reference.
- **Removed from It2:**
  - FRDM-MCXN947 (owned);
  - the second MCP4728 (one covers ai0-ai2);
  - the TPS22918 and the R3 shield (bought in It1).

## IT3 changes

- **Spare FRDM-MCXN947** (≈ €30-40): replaces the spare F767, for soak and destructive OTA/PERS tests.
- Optional stimulus-timed VBUS switch for PERS-02 (a TPS22918 in a USB-C VBUS pass-through) [proposed; D51].
- The Pico 2 is dropped; the MCXA153/156 are the functional peers.
- **Only if P1-F767 is needed:** NUCLEO-F767ZI, W25Q128JV breakout, an E5V switch (Pololu 2810 or TPS22918), and a 5 V PSU.
- NUCLEO-H563ZI (P3): optional, unchanged.