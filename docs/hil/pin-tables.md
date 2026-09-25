# HIL pin tables (v2.2)

Conventions:
- **Series resistors (D40).** On P1, every DUT line has one 2.2 kΩ series resistor, mounted on the DUT shield. The "DUT side" of that resistor is the probe point for:
  - the LA;
  - the nRF probe, through 4.7 kΩ;
  - the analog nodes (100 nF at the DUT pin). The stimulus sense inputs add 10 kΩ.
- Levels on DUT channels are physical wire levels. hilrig applies the catalog polarity (`GPIO_ACTIVE_LOW`). Stimulus di lines are 0/z only (D49).
- Shields are component-free Arduino-R3 proto shields. Their 5V and VIN stacking pins (J3-10, J3-16 on both FRDM boards) are clipped.
- Connector positions come from:
  - UM12018 Rev 2.0 Tables 17-20 (FRDM-MCXN947);
  - UM12041 Rev 3.0 Tables 19-26 (FRDM-MCXN236);
  - UM12121 Rev 1 (FRDM-MCXA156) and UM12012 Rev 2.0 (FRDM-MCXA153);
  - UM1974 Rev 8 (Appendix A, NUCLEO-F767ZI).
- MCX I/O electrical limits (MCXNP184M150F70 Table 9):
  - 3.3 V I/O with VIH ≥ 2.31 V and VIL ≤ 0.99 V;
  - absolute maximum VDD_Px + 0.3 V; **not 5 V tolerant**;
  - injection ±3 mA per pin and ±25 mA per 16 contiguous pins;
  - internal pull-ups and pull-downs 33-75 kΩ.
- Everything here was verified this session unless it is tagged [likely], [unverified: W-step] or [proposed].

## 1. P1 DUT: FRDM-MCXN947 (`frdm_mcxn947/mcxn947/cpu0`)

Catalog: `firmware/boards/io/frdm_mcxn947_mcxn947_cpu0.dtsi` (BACnet e62a095 = main, unchanged at 4ee098e), plus the snippet `hil`.

### 1.1 Catalog channels (release images)

The ids follow devicetree order.

| Id | Chan | Kind / flags | MCU | FRDM pin | Arduino | Shared with | Stimulus side (FRDM-MCXN236) | Notes |
|---|---|---|---|---|---|---|---|---|
| 0 | di0 | di, ACTIVE_LOW + PULL_UP (pressed = 1) | P0_23 | J4-12 | A5 | SW2 / WAKEUP_B with 0.1 µF (SJ9 1-2) | `di0` out 0/z, P4_13 A5 | Release reaches VIH after 4-9 ms (RPU 33-75 k × 0.1 µF). Pull-down takes about 0.26 ms through 2.2 k. Never used for debounce timing. |
| 1 | di1 | di, ACTIVE_LOW + PU | P0_6 | not on a header (SW3) | – | ISPMODE_N | **never wired** | Low at reset = ROM ISP. Manual only. |
| 2 | di2 | di, ACTIVE_LOW + PU (contact to GND = 1) | P0_29 | J1-6 | D2 | – | `di2` out 0/z, P2_0 D2 | IO-04 channel on P1; replaces di1 in IO-05/15/16 and ROB-05 |
| 3 | di3 | di, ACTIVE_LOW + PU | P0_30 | J1-10 | D4 | SJ2 1-2 (2-3 = P4_13) | `di3` out 0/z, **P4_21 J3-3** (reroute) | |
| 4 | do0 | do, ACTIVE_LOW (1 = LED on = pin low) | P0_10 | J2-4 | D9 | red LED = mqtt_tls `led0` (identify), SJ5 | `do0` in, pull-down, P3_14 D9 | While the DUT is unpowered, the pad floats to about 3.3 V − Vf through the LED: mask it. |
| 5 | do1 | do, ACTIVE_LOW | P0_27 | J2-6 | D10 | green LED, FC1_P3 (lpspi1 disabled by the snippet), SJ6 | P1_3 D10 | as do0 |
| 6 | do2 | do, ACTIVE_LOW | P1_2 | J1-14 | D6 | blue LED, MCU-Link SPI bridge (DNP), SJ4 | P3_17 D6 | as do0 |
| 7 | do3 | do, ACTIVE_HIGH push-pull | P0_31 | J1-16 | D7 | – | P0_22 D7 | IO-01 channel |
| 8 | do4 | do, ACTIVE_HIGH push-pull | P0_28 | J2-2 | D8 | – | P0_23 D8 | |
| 9 | ai0 | LPADC0 CH14B, 12 bit, VREFO 1.8 V full scale | P0_14 | J4-6 | A2 | J2-3 only if SJ19 is 2-3 | src MCP4728 A; sense **P0_26 J2-7** (CH18B) through 10 k | 2.2 kΩ + 100 nF node at the DUT pin; inputs above 1.8 V read full scale |
| 10 | ai1 | LPADC0 CH14A | P0_22 | J4-8 | A3 | J2-5 only if SJ18 is 2-3 | src B; sense **P0_28 J2-3** (CH20B) | same as ai0 |
| 11 | ai2 | LPADC0 CH15B | P0_15 | J4-10 | A4 | SJ8 1-2 | src C; sense P4_12 A4 (CH5A) | same as ai0 |
| 12 | ao0 | FlexPWM1 SM0 A, 1 kHz | P2_6 | J3-15 (inner, pigtail) | – | SDHC0 D3, SAI0 BCLK (drivers not built) | **P3_16 J3-7** (PWM1_A2, SM2 A capture) | |
| 13 | ao1 | FlexPWM1 SM0 B | P2_7 | J3-13 (inner, pigtail) | – | SDHC0 D2, SAI0 FS | P2_7 J3-13 (PWM1_B0, SM0 B capture) | inner row, straight across |
| 14 | ao2 | SCT0_OUT5 | P1_23 | J1-8 | D3 | SJ1 1-2, camera J9-23 | P3_12 D3 (PWM1_A0, SM0 A capture) | stimulus P3_12 is shared with its U12 IO3 (W2 check, §2) |

### 1.2 HIL extras (snippet `hil`) and MS/TP (planned UART)

| Function | MCU | FRDM pin | Arduino | Devicetree | Rig side | Iteration |
|---|---|---|---|---|---|---|
| Marker m0 | P0_26 (MED pad) | J2-10 | D12 | `hil-marker-gpios[0]`; FC1_P2 (lpspi1 disabled) | stim `m0` P1_2 D12; 100 k pull-down; LA | It1 |
| Marker m1 | P0_25 | J2-12 | D13 | `[1]`; FC1_P1 | stim `m1` P1_1 D13; LA | It1 |
| Marker m2 | P4_0 (SLOW pad) | J2-18 | SDA | `[2]`; FC2_P0 (lpi2c2 okay, driver not built) | stim `m2` P1_16 SDA; LA It2 | It1 wired |
| Marker m3 | P4_1 (SLOW pad) | J2-20 | SCL | `[3]`; FC2_P1 | stim `m3` P1_17 SCL; LA It2 | It1 wired |
| MS/TP TX | P4_2 | J1-4 | D1 | lpuart2 TXD = FC2_P2, alias `mstp-uart0`, 38400. Driven idle-high from boot in both apps. `dac0` (same pin) is disabled. | U1 DI (It2); stim `mstp_tx` P4_2 D1 (read only, It1 wired); LA `tx` | It1 wired / It2 bus |
| MS/TP RX | P4_3 | J1-2 | D0 | lpuart2 RXD = FC2_P3, pad pull-up | U1 RO → 2.2 kΩ → D0, with a 10 k pull-up to U1 VCC at RO; stim `mstp_rx` P4_3 D0 (no pull); LA `rx` | It1 wired / It2 bus |
| MS/TP DE | P0_24 (DIS after reset) | J2-8 | D11 | `/zephyr,user mstp-de-gpios`, push-pull, 1 = drive; FC1_P0 (lpspi1 disabled) | U1 DE + /RE (tied), 10 k pull-down on the net; stim `mstp_de` P1_0 D11; LA `de` | It1 wired / It2 bus |

- No `hil-io` on P1: no free outer-row GPIO is left. If one is ever needed, candidate inner-row pigtails are J3-1 P2_0, J3-3 P1_22, J2-7 P5_4 and J2-11 P1_12 [unverified: conflict check].
- Build checks (D38): an instrumented P1 image fails if any of these holds:
  - CONFIG_I2C, SPI, DAC, I2S, LED or INPUT is set, or an SDHC/SDMMC disk is enabled;
  - lpi2c2 or lpuart2 is not okay;
  - dac0 or lpspi1 is okay.

### 1.3 Rig connections

| Function | Where | Circuit |
|---|---|---|
| RESET_B | **J3-6** (Arduino RESET). The same net has SW1 with 0.1 µF and the MCU-Link reset; J23 is the external SWD port (unused). | **Drive:** BSS138P Q2 drain through 100 Ω; gate from stim `nrst` P2_8 through 1 k, 100 k gate→GND.<br>**Sense (D50):** BSS138P Q3 on the DUT shield. Gate from J3-6 through 1 kΩ, source GND, drain over IDC line 33 to stim `nrst_sense` P3_6, which has a 10 k pull-up to the stimulus VDD_BOARD on the stimulus shield. RESET_B sees only Q3's gate. |
| MCU-rail switch | **J24** (1×2, shunt removed). Pin A = P3V3, pin B = P3V3_MCU. They are identified by continuity with the board unpowered: pin A 0 Ω to J3-8, pin B 0 Ω to AREF J2-16 (VDD_ANA via R152) [unverified: W1]. The leads use a keyed or labelled 2-pin housing. | TPS22918 VIN = A, VOUT = B, QOD tied to VOUT, CT 1 nF, 1 µF at VIN. ON: 100 k to P3V3, pulled low by BSS138P Q1, whose gate comes from stim `pwr` P3_18 through 1 k, with 100 k to GND. It2: INA226 between VOUT and pin B. |
| MCU-rail sense | AREF **J2-16** = VDD_ANA (switched, through R152 0 Ω) | 100k/100k + 100 nF on the DUT shield → 10 k → stim `v3v3` P0_29 J2-1 |
| Board-rail sense | **J3-8** (or J3-4 IOREF) = P3V3 (always on) | 100k/100k + 100 nF → 10 k → stim `v3v3_brd` P0_27 J2-5. Sense only, never a supply. It2 `pers` profile: a separate 100k/100k divider from J3-8 to LA CH15. |
| U1 VCC (It2) | P3V3_MCU at J24 pin B: the TPS22918 VOUT when the switch is fitted, the shunt output otherwise | 100 nF at U1 |
| Console | LPUART4 P1_8 RX / P1_9 TX through the MCU-Link VCOM (J17, J18 open) | LA `vcp_tx` tap at camera header **J9-30** (P1_9); no solder bridge |
| Ethernet | ENET-QoS RMII, LAN8741A at MDIO 0, on P3V3; Y3 50 MHz on P1_4 | RJ45 to the host DUT NIC |
| GND | J2-14, J3-12, J3-14 | at least 2 wires to the stimulus; 1 GND per 4 LA probes |

### 1.4 Keep-out on the DUT (never wire)

| Group | Pins |
|---|---|
| Supply inputs | J3-10 (5V = P5V0 input) and J3-16 (VIN): their stacking pins are clipped on the DUT shield. **J11** (HS USB: it would feed P5V0 during a `usb_port` cut), J15. |
| Probe / ISP | J21 (forces MCU-Link ISP), SW3 / P0_6 (ISPMODE_N) |
| ENET | **D5 = P1_21 MDIO (J1-12)**, P1_4-P1_7, P1_13-P1_15, P1_20 |
| SWD / SWO | P0_0, P0_1, P0_2, P0_3 (inner row J2-9) |
| FlexSPI NOR | P3_0, P3_6-P3_11 |
| CAN | P1_10 / P1_11 (TJA1057 via SJ16/SJ26, default) |
| Console | P1_8 / P1_9, except the J9-30 LA tap |
| Analog pads | A0 / A1 (J4-2 / J4-4, ADC0_A0 / B0): unused (a possible future catalog ai) |
| Inner rows | everything except the pigtails J3-13 and J3-15 |
| Board nodes | `dac0`, `flexcomm1_lpspi1` (disabled by the snippet); `flexcomm2_lpi2c2` stays okay but its driver is never built |

### 1.5 Jumpers and board settings (UM12018)

| Item | Default | Rig | Reason |
|---|---|---|---|
| J24 | shorted | shunt removed; TPS22918 fitted in W2, on keyed leads; used only after the W1 back-feed check passes | only feed of P3V3_MCU |
| J17 | MCU-Link USB-C | on the uhubctl hub; the only USB path | power, SWD, VCOM, `usb_port` |
| J11 | – | never connected | would feed P5V0 during a USB cut |
| J18 | open (VCOM on) | open | console |
| J19 | open | open (shorted = onboard SWD off) | |
| J21 | open | **never fitted** | MCU-Link ISP |
| J22 | shorted | unchanged | SWD clock |
| SJ1, SJ2, SJ4-SJ9, SJ14, SJ15, SJ18, SJ19 | defaults (1-2) | unchanged | Arduino routing of the catalog, markers and MS/TP |
| R154 | DNP | DNP; **never populated** | Populating it disables Y3 permanently (UM12018 §2.4): no PHY clock, no Ethernet |

### 1.6 W1 bench checks (in order; results into bench.yml)

1. **D0/D1 contention.** Hold the DUT in reset (SW1). With 10 kΩ from D0 to GND and from D1 to GND, each must read < 0.1 V.
2. **J24 pin identity and back-feed.**
   - Board unpowered: pin A reads 0 Ω to J3-8 (P3V3), pin B reads 0 Ω to AREF J2-16. Label the leads and use the keyed housing. A reversed switch would put its QOD pull-down (25-35 Ω) across P3V3.
   - With the shunt removed and the board on J17: measure the open-circuit voltage on pin B, and the current into a 22 Ω shunt from pin B to GND.
   - **Pass only if the current is < 3 mA (< 66 mV across 22 Ω) and pin B stays < 0.3 V.** Otherwise `mcu_rail` is not used, and PWR-01, PERS-01 and OTA-04 use `usb_port`. R154 is never populated.
3. **RESET_B.**
   - Idle level ≥ 3.0 V.
   - Release time with SW1's 0.1 µF.
   - MCU-Link reset driver type.
   - Whether an internal (WWDT) reset pulls RESET_B low.
   - Whether RESET_B drops the Ethernet link.
4. **UART loopback D1→D0** at 38400, 76800 and 115200 baud. This also confirms the combined-mode pinout.
5. **CMPA** read-only address (0x01004000 or 0x11004000) and tool (pyocd `savemem` or LinkServer gdbserver); record the SHA-256. `--erase` stays forbidden whatever the answer.
6. *(It2)* ai0 at 0.90 V reads about 2048 (VREF mode, ADC clock: FW-14).
7. di0 release timing on the LA.
8. *(W2, after the shields)* RESET_B ≥ 3.0 V (DMM) with the stimulus idle and with its hub port off; the offset between the Q3-sensed release edge, the LA `nrst` edge and the boot marker.

## 2. Stimulus: FRDM-MCXN236 (`frdm_mcxn236`), `apps/hil_stimulus` profile P1

The stimulus avoids these positions (never wire them on the stimulus shield):

| Position | Pin | Why |
|---|---|---|
| D4 | P0_21 | FXLS8974 INT1 |
| D5 | P2_7 | same pin as J3-13 (ao1) |
| A1 | P4_15 | CAN1 RXD, driven by the TJA1057 |
| A2 | P4_16 | CAN1 TXD |
| A3 | P4_17 | blue LED |
| J3-15 | P3_12 | same pin as D3 |
| J1-1 | P3_16 | same pin as J3-7 |
| J1-3 | P2_0 | same pin as D2 |
| J1-11, J3-5 | P3_17 | same pin as D6 (do2) |
| J2-9 | P0_22 | same pin as D7 (do3) |
| J3-1 | P4_13 | same pin as A5 (di0) |
| J3-11 | P3_14 | same pin as D9 (do0) |
| J5-7 | P5V0 | mikroBUS 5 V; the MCP4728 takes VDD from J6-7 |
| 5V (J3-10), VIN (J3-16) | – | stacking pins clipped on the shield |
| RESET, AREF, IOREF, 3V3 | – | supplies and reset. The shield's 3V3 (J3-8, VDD_BOARD) feeds only the `nrst_sense` pull-up and, optionally, the MCP4728; it never crosses the ribbon. |

Further notes:
- D10-D13 (P1_3..P1_0) also go to the MCU-Link USB-SPI bridge through R153-R156, which are populated by default (UM12041 §3.9). The bridge's SCK, host data and PCS0 are MCU-Link outputs on m1, m0 and do1. **Remove R153-R156 before the W2 wiring** and record it in bench.yml.
- D3 (P3_12, ao2) is shared with IO3 of the on-board QSPI flash U12 through a zero-ohm selection (UM12041 §2.6). W2: with the DUT driving D3 low, P3_12 must read < 0.8 V; otherwise isolate U12 with its zero-ohm resistor.
- The board DTS enables only gpio0, gpio1 and gpio4; the overlay enables gpio2 and gpio3.
- Interrupts use a per-pin GPIO ICR with no line sharing. As a policy, ao channels are never armed.
- The reset states of P2_8, P3_18, P3_6 and P1_7 are [unverified: W2]. The 100 k gate pull-downs make NRST and PWR fail-safe either way.

| Chan | Kind | Stim pin (connector) | Network | DUT end |
|---|---|---|---|---|
| di0 | di out 0/z | P4_13 (A5, J4-12) | 2.2 kΩ on the DUT shield | P0_23 A5 |
| di2 | di out 0/z | P2_0 (D2, J1-6) | same | P0_29 D2 |
| di3 | di out 0/z | **P4_21 (J3-3)**, reroute | same | P0_30 D4 |
| do0 | do in, pull-down | P3_14 (D9, J2-4) | same | P0_10 D9 |
| do1 | do in, pull-down | P1_3 (D10, J2-6); R153-R156 removed | same | P0_27 D10 |
| do2 | do in, pull-down | P3_17 (D6, J1-14) | same | P1_2 D6 |
| do3 | do in, pull-down | P0_22 (D7, J1-16) | same | P0_31 D7 |
| do4 | do in, pull-down | P0_23 (D8, J2-2) | same | P0_28 D8 |
| ai0 | src MCP4728 A + sense | src VOUTA; sense **P0_26 (J2-7, ADC0 CH18B)** | src: 2.2 kΩ into the node, 100 nF at the DUT pin; sense: 10 kΩ at the stimulus | P0_14 A2 |
| ai1 | src B + sense | src VOUTB; sense **P0_28 (J2-3, CH20B)** | same | P0_22 A3 |
| ai2 | src C + sense | src VOUTC; sense P4_12 (A4, CH5A) | same | P0_15 A4 |
| ao0 | FlexPWM1 SM2 A capture, pull-down | **P3_16 (J3-7)**, pigtail | 2.2 kΩ | P2_6 J3-15 |
| ao1 | FlexPWM1 SM0 B capture | P2_7 (J3-13) | 2.2 kΩ | P2_7 J3-13 |
| ao2 | FlexPWM1 SM0 A capture | P3_12 (D3, J1-8); U12 IO3 check in W2 | 2.2 kΩ | P1_23 D3 |
| mstp_rx | marker, **no pull** | P4_3 (D0, J1-2) | 2.2 kΩ | P4_3 D0 |
| mstp_tx | marker, pull-down | P4_2 (D1, J1-4) | 2.2 kΩ | P4_2 D1 |
| mstp_de | marker, pull-down | P1_0 (D11, J2-8) | 2.2 kΩ | P0_24 D11 |
| m0 | marker, pull-down | P1_2 (D12, J2-10) | 2.2 kΩ | P0_26 D12 |
| m1 | marker, pull-down | P1_1 (D13, J2-12) | 2.2 kΩ | P0_25 D13 |
| m2 | marker, pull-down | P1_16 (SDA, J2-18); lpi2c5 and i3c1 disabled | 2.2 kΩ | P4_0 SDA |
| m3 | marker, pull-down | P1_17 (SCL, J2-20) | 2.2 kΩ | P4_1 SCL |
| nrst | FET gate (1 = RESET_B low) | P2_8 (J1-5) | 1 kΩ to Q2's gate, 100 k gate→GND | RESET_B J3-6 through the 100 Ω drain |
| nrst_sense | input, **no internal pull**, ACTIVE_HIGH (pin high = DUT in reset) | P3_6 (J1-7) | 10 kΩ pull-up to VDD_BOARD on the stimulus shield; Q3's drain via IDC 33 | RESET_B J3-6 through 1 kΩ to Q3's gate (D50) |
| pwr | FET gate, ACTIVE_LOW (logical 1 = DUT on) | P3_18 (J1-9) | 1 kΩ to Q1's gate, 100 k gate→GND | TPS22918 ON at J24 |
| sync | out | P1_7 (J1-13) | 100 Ω | LA `sync`, nRF probe |
| lb | marker (self-test) | P2_9 (J1-15) | jumper from J1-13 | – |
| v3v3 | sense ×2 | P0_29 (J2-1, CH21B) | 10 kΩ after the 100k/100k + 100 nF divider | VDD_ANA at AREF J2-16 |
| v3v3_brd | sense ×2 | P0_27 (J2-5, CH19B) | same | P3V3 at J3-8 |
| AFE I2C | LPI2C2 at 400 kHz (FC2 in LPI2C-only mode; lpuart2 disabled) | SDA P4_0 (mikroBUS J5-6), SCL P4_1 (J5-5), GND J5-8 | board pull-ups; the module's pull-ups go to 3.3 V or are removed | MCP4728 at 0x60 (LDAC → GND). **VDD = 3.3 V from J6-7 (VDD_BOARD), never J5-7 (P5V0).** The FXLS8974 at 0x18 shares the bus. |
| Injector (It2) | FC3 in LPUART-only mode, RTS = DE (`nxp,rs485-mode`) | RX P1_12 (J9-28), TX P1_13 (J9-27), DE P1_14 (J9-2) | U2, VCC = stim 3V3: DE 10 k pull-down, /RE → GND, RO 10 k pull-up | bench bus |
| rs485_a / rs485_b (It2) [proposed] | sense ×2 | two free ADC0 inputs chosen in It2 (candidate J2-11 P0_25, behind R38) | bus wire → 100 k 0.1 % → midpoint (100 k 0.1 % + 100 nF to GND) → 10 k → ADC | bus D+ / D- |
| Free | – | J4-11 P5_7 | – | It3 relays (more pins are chosen with the relay box) |

The full devicetree is `apps/hil_stimulus/boards/frdm_mcxn236.overlay` (rebuilt this session with the review patch: 100,244 B / 42,976 B, 0 warnings).

### 2.1 Fallback stimulus: FRDM-MCXA156 (not wired unless the MCXN236 fails)

What differs from the MCXN236:
- **Capture.** It needs the upstream `pwm_mcux.c` capture guards and can capture only on X channels: D3 P3_12 (PWM0_X0), D5 P3_14 (PWM0_X2), D10 P3_13 (PWM0_X1). ao1 needs an X pin or a CTIMER input.
- **Counter.** Set `CONFIG_MCUX_OS_TIMER=n` for the 96 MHz 64-bit counter.
- **Injector.** LPUART1 on P3_20/P3_21, with RTS on P3_22.
- **DAC.** The on-chip DAC on J2-9 (P2_2) can source one ai.
- **Conflicts.** A4 = P1_12 (CAN RXD) and A5 = P1_13 (CAN TXD): never wired. D3 and D10 carry the red and green LEDs.

## 3. Logic analyzer

**It1: FX2.** `bench.yml la: {driver: fx2lafw, channels: {nrst: 0, m0: 1, m1: 2, di2: 3, do3: 4, do0: 5, vcp_tx: 6, sync: 7}}`, 24 MS/s, on a host root port.

| CH | Signal | Probe point (DUT side of 2.2 kΩ) |
|---|---|---|
| 0 | nrst | RESET_B J3-6 |
| 1 | m0 | P0_26 D12 |
| 2 | m1 | P0_25 D13 |
| 3 | di2 | P0_29 D2 |
| 4 | do3 | P0_31 D7 |
| 5 | do0 | P0_10 D9 (red LED, mqtt `led0`) |
| 6 | vcp_tx | P1_9 at J9-30 |
| 7 | sync | stim P1_7 J1-13 |

**It2: DSLogic Plus, 16 ch.** USB 2a0e:0020 only; otherwise a Saleae Logic Pro 16 with the same names.

| CH | Signal | CH | Signal |
|---|---|---|---|
| 0 | de (P0_24 D11) | 8 | rx (P4_3 D0) |
| 1 | tx (P4_2 D1) | 9 | stim_de (stim P1_14 J9-2) |
| 2 | bus_ro (U3 RO, 3.3 V) | 10 | m2 (P4_0 SDA) |
| 3 | nrst | 11 | m3 (P4_1 SCL) |
| 4 | m0 | 12 | ao0 (P2_6 J3-15) |
| 5 | m1 | 13 | sync |
| 6 | di2 | 14 | vcp_tx |
| 7 | do3 | 15 | do0; in the `pers` profile `p3v3_brd` (separate 100k/100k divider on J3-8; D51) |

Rates:
- MS/TP: stream mode at 20 MHz × 16.
- Latency, boot and ISR timing: 100 MHz × 16 in windows of at most 0.16 s.
- 400 MHz is never used.

## 4. nRF54L15 DK edge probe (It2, optional; D48)

Preparation, before any cable is attached:
- In nRF Connect Board Configurator:
  - set VDD:nRF to 3.3 V;
  - disconnect the UART0 group (P0.00-P0.03) from the debugger;
  - **disconnect the UART1 RTS/CTS group (P1.06/P1.07) from the debugger** (analog switches U4/U5). Turning HWFC off in firmware is not enough: on DTR the debugger runs HWFC detection by driving CTS, and if it detects HWFC it keeps driving P1.07 until a power-on reset (DK UG v1.0.0 §3.1.3).
- Measure VDD at P6 with a DMM: it must be ≥ 3.2 V. The setting persists in the board controller.
- With the VCOM1 terminal open (DTR asserted), check that P1.07 is Hi-Z.

| Probe pin | GPIOTE | Signal | Source (DUT side of 2.2 kΩ) | Series |
|---|---|---|---|---|
| P1.11 | GPIOTE20 | de | P0_24 D11 | 4.7 kΩ |
| P1.12 | GPIOTE20 | tx | P4_2 D1 | 4.7 kΩ |
| P0.00-P0.03 | GPIOTE30 | m0-m3 | D12, D13, SDA, SCL | 4.7 kΩ each |
| P1.06 | GPIOTE20 | sync | stim P1_7 | 4.7 kΩ |
| P1.07 | GPIOTE20 | bus_ro | U3 RO | 4.7 kΩ |

- Timestamps: GPIOTE IN → nrfx_gppi (DPPI/PPIB) → TIMER00 CAPTURE at 128 MHz (32-bit; extended by an overflow count).
- The firmware refuses to arm unless VDD ≥ 3.0 V.
- Console: VCOM1 (uart20 P1.04/P1.05), which needs DTR. udev `/dev/hil/probe0`.
- The **nRF54LM20 DK** (`nrf54lm20dk/nrf54lm20b/cpuapp`) is the alternative: about 34 free GPIOTE pins on P0/P1/P3, no Arduino header, and it also defaults to 1.8 V.

## 5. MS/TP peers (It2; D46)

| Board | UART | TX | RX | DE (RTS, hardware, `nxp,rs485-mode`) | MAC | Notes |
|---|---|---|---|---|---|---|
| FRDM-MCXA153 | LPUART2 | P1_5 D1 | P1_4 D0 | P1_6 (J1-3) | 3 (`boards/frdm_mcxa153.conf`: `CONFIG_MSTP_NODE_MAC=3`) | The board DTS routes lpuart2 to mikroBUS P3_14/P3_15; the overlay moves it to D0/D1. 24 KiB RAM: MAX_APDU 480. Device instance 260203. udev `/dev/hil/peer3`. |
| FRDM-MCXA156 | LPUART2 (overlay node + clock hook) | P2_10 D1 | P2_11 D0 | P1_6 (J1-3) | 4 (`boards/frdm_mcxa156.conf`: `CONFIG_MSTP_NODE_MAC=4`, `CONFIG_BACNET_MAX_APDU_SIZE=480`) | 4.4.2's SoC dtsi declares only lpuart0/1. A4/A5 = CAN RXD/TXD (never wired). Device instance 260204. udev `/dev/hil/peer4`. |

Each peer has its own THVD1450, powered from the peer's 3V3, and follows the §4.1 module rules. The peers are never a timing reference. Both are flashed with `hil-flash --probe <full serial>` (D42).

## 6. Shields and ribbon (P1) [proposed, built in W2]

Both shields are component-free Arduino-R3 proto shields: no power LED, reset button or ICSP header. The 5V (J3-10) and VIN (J3-16) stacking pins are clipped on both.

**DUT shield** (stacking outer-row headers):
- 2.2 kΩ per line;
- 100 nF at A2/A3/A4;
- 100k/100k + 100 nF dividers for AREF and J3-8 (plus, in It2, a separate 100k/100k divider from J3-8 for LA CH15 `p3v3_brd`);
- BSS138P Q2 NRST (100 Ω drain), Q1 PWR (1 k + 100 k gate networks), and Q3 RESET_B sense (1 kΩ gate from J3-6, source GND, drain to IDC 33);
- a 2×5 LA header and an nRF probe header;
- pigtails to J3-13, J3-15 and J3-6 (RESET is on the outer row);
- a keyed flying lead pair to the TPS22918 module at J24;
- It2: a U1 socket, with VCC from J24 pin B.

**Stimulus shield:**
- straight outer-row positions;
- pigtails to J3-3, J3-7, J3-13, J2-1/3/5/7, J1-5/7/9/13/15;
- the 10 kΩ pull-up from IDC 33 to the shield's 3V3 (VDD_BOARD) at P3_6;
- the MCP4728 on flying leads to mikroBUS J5-5/J5-6 (SCL/SDA) and J5-8 (GND), with VDD from J6-7 (never J5-7);
- 10 kΩ in series on each sense input;
- It2: the injector U2 on J9.

40-way IDC map (odd pins carry signals where possible; about one GND per 4 signals):

| IDC | Signal | DUT shield | Stimulus shield |
|---|---|---|---|
| 1 | GND | GND J2-14 | GND |
| 2 | di0 | A5 | A5 P4_13 |
| 3 | di2 | D2 | D2 P2_0 |
| 4 | di3 | D4 | J3-3 P4_21 |
| 5 | GND | | |
| 6 | do0 | D9 | D9 P3_14 |
| 7 | do1 | D10 | D10 P1_3 |
| 8 | do2 | D6 | D6 P3_17 |
| 9 | do3 | D7 | D7 P0_22 |
| 10 | do4 | D8 | D8 P0_23 |
| 11 | GND | | |
| 12 | ao0 | J3-15 pigtail | J3-7 P3_16 |
| 13 | ao1 | J3-13 pigtail | J3-13 P2_7 |
| 14 | ao2 | D3 | D3 P3_12 |
| 15 | GND | | |
| 16 | mstp_rx | D0 | D0 P4_3 |
| 17 | mstp_tx | D1 | D1 P4_2 |
| 18 | mstp_de | D11 | D11 P1_0 |
| 19 | GND | | |
| 20 | m0 | D12 | D12 P1_2 |
| 21 | m1 | D13 | D13 P1_1 |
| 22 | m2 | SDA | SDA P1_16 |
| 23 | m3 | SCL | SCL P1_17 |
| 24 | GND | | |
| 25 | ai0_src | 2.2 kΩ → A2 node | MCP4728 VOUTA |
| 26 | ai0_sense | A2 node | 10 kΩ → J2-7 P0_26 |
| 27 | ai1_src | 2.2 kΩ → A3 node | VOUTB |
| 28 | ai1_sense | A3 node | 10 kΩ → J2-3 P0_28 |
| 29 | ai2_src | 2.2 kΩ → A4 node | VOUTC |
| 30 | ai2_sense | A4 node | 10 kΩ → A4 P4_12 |
| 31 | GND | | |
| 32 | nrst_drv | Q2 gate network | J1-5 P2_8 |
| 33 | nrst_sense | Q3 drain (gate 1 kΩ from J3-6) | 10 kΩ pull-up to VDD_BOARD → J1-7 P3_6 |
| 34 | pwr_ctl | Q1 gate network → TPS22918 ON | J1-9 P3_18 |
| 35 | v3v3 | AREF divider midpoint | 10 kΩ → J2-1 P0_29 |
| 36 | v3v3_brd | J3-8 divider midpoint | 10 kΩ → J2-5 P0_27 |
| 37 | sync | LA header | J1-13 P1_7 (100 Ω) |
| 38 | GND | | |
| 39 | spare | – | – |
| 40 | GND | | |

- With the ribbon unplugged, the gate pull-downs keep the DUT powered and out of reset. P3_6 then reads "in reset" through its pull-up, which R-02a catches.
- The shields never connect 3V3, 5V, VIN, IOREF or AREF across the ribbon.

---

## Appendix A: P1-F767, optional profile NUCLEO-F767ZI (MB1137)

This is the v2.1 material, kept for the optional profile. The MS/TP rows follow the planned UART (D37).

With an F767 DUT the stimulus is one of:
- a second NUCLEO-F767ZI (tables A.6 and A.7, unchanged from v2.1; its di0 channel sets `drive-high`);
- the FRDM-MCXN236 with a `p1-f767` profile overlay, to be written only if an F767 is bought.

### A.1 Catalog channels (`firmware/boards/io/nucleo_f767zi.dtsi`; all DUT pins FT)

| Id | Chan | Kind / flags | MCU | Connector | Stimulus side (1 kΩ) | Notes |
|---|---|---|---|---|---|---|
| 0 | di0 | di, ACTIVE_HIGH (B1) | PC13 | **CN11-23** (morpho, single pin) | push-pull, active = 1, inactive = z | R59 330 Ω, R58 220 k pull-down |
| 1 | di1 | di, ACTIVE_LOW, PULL_UP | PF15 | CN10-12 (D2) | 0 / z | |
| 2 | di2 | di, ACTIVE_LOW, PULL_UP | PF14 | CN10-8 (D4) | 0 / z | |
| 3 | do0 | do (LD1 green; mqtt `led0`) | PB0 | CN10-31 / CN11-34 | input, pull-down | |
| 4 | do1 | do (LD2 blue) | PB7 | **CN11-21** | input | |
| 5 | do2 | do (LD3 red) | PB14 | **CN12-28** | input | |
| 6 | do3 | do | PF13 | CN10-2 (D7) | input | |
| 7 | do4 | do | PF12 | CN7-20 (D8) | input | |
| 8 | ai0 | ADC1_IN3 | PA3 | CN9-1 (A0) | DAC src + sense, 100 nF at the DUT pin | 0-3.3 V |
| 9 | ai1 | ADC1_IN10 | PC0 | CN9-3 (A1) | It1 divider 10k/10k; later MCP4728 | |
| 10 | ai2 | ADC1_IN13 | PC3 | CN9-5 (A2) | 20k/10k | |
| 11 | ai3 | ADC3_IN9 | PF3 | CN9-7 (A3) | DAC2 src + sense | |
| 12 | ai4 | ADC3_IN15 | PF5 | CN9-9 (A4) | 10k/20k | SB147 ON |
| 13 | ai5 | ADC3_IN8 | PF10 | CN9-11 (A5) | 33k/10k | SB157 ON |
| 14 | ao0 | TIM1_CH1 1 kHz | PE9 | CN10-4 (D6) | capture | |
| 15 | ao1 | TIM1_CH2 | PE11 | CN10-6 (D5) | capture | |
| 16 | ao2 | TIM1_CH3 | PE13 | CN10-10 (D3) | capture | |

### A.2 HIL extras and MS/TP (planned UART)

| Function | MCU | Connector | Devicetree | Wired to |
|---|---|---|---|---|
| m0..m3 | PG0 / PG1 / PG2 / PG3 | CN9-29 / CN9-30 / CN8-14 / CN8-16 | `hil-marker-gpios` (one BSRR write) | stim m0..m3, LA |
| di3 / di4 (hil-io) | PE10 / PE12 | CN10-24 / CN10-26 | catalog ids 17/18, ACTIVE_HIGH + PULL_DOWN | stim di3 / di4 |
| do5 / do6 (hil-io) | PE14 / PE15 | CN10-28 / CN10-30 | ids 19/20 | stim do5 / do6 |
| MS/TP TX | PG14 | CN10-14 (D1) | USART6 TX AF8 (board pinctrl, pull-up), alias `mstp-uart0` | U1 DI, LA `tx` |
| MS/TP RX | PG9 | CN10-16 (D0) | `hil_usart6_rx_pg9_pu` | U1 RO (10 k pull-up to DUT 3V3), LA `rx` |
| MS/TP DE | PD15 | CN7-18 (D9) | `mstp-de-gpios`, GPIO push-pull | U1 DE + /RE (10 k pull-down), LA `de` |

The v2.1 USART2 hardware-DE rows (PD5/PD6/PD4) and the 3-jumper selector are withdrawn.

### A.3 Rig connections

| Function | Where | Circuit |
|---|---|---|
| NRST | CN8-5 (also CN11-14, CN6-5) | BSS138P drain through 100 Ω; 10 k to `nrst_sense` |
| Power in | E5V CN11-6, GND CN11-8. Solder only these two, with a keyed housing. **CN11-5 (VDD) and CN11-7 (BOOT0) stay unpopulated.** | load switch VOUT; JP3 1-2, JP1 OFF |
| 3V3 sense | CN8-7 | 100k/100k + 100 nF |
| VCP TX tap | PD8 at CN12-10 through SB7 | LA only, after the continuity check |
| SPI NOR W25Q128JV (/lfs) | CLK PA5 CN7-10, DO PA6 CN7-12, DI PB5 CN7-13, /CS PD14 CN7-16, VCC CN8-7, GND CN8-11 | per the BACnet overlay |
| U1 VCC | DUT 3V3 CN8-7 | |

### A.4 Keep-out

| Group | Pins |
|---|---|
| CN11 supply and boot | CN11-5 (VDD), CN11-7 (BOOT0) |
| RMII | PA1, PA2, PA7 (CN7-14 D11, CN9-15), PB13, PC1, PC4, PC5, PG11, PG13 |
| VCP | PD8, PD9 |
| USB | PA8-PA12, PG6, PG7 |
| SWD / SWO | PA13, PA14, PB3 |
| Clocks | PH0, PH1, PC14, PC15 |
| NOR | PA5, PA6, PB5, PD14 |
| Board nodes | I2C1 PB8/PB9, CAN1 PD0/PD1, DAC PA4 |

### A.5 Jumpers

JP3 1-2 (E5V), JP1 OFF, CN4/JP5/JP6/JP7/JP4 ON, SB5/SB6 ON, SB7 continuity check, SB111/SB110/SB177 ON, SB112/SB149 ON, SB119 OFF, SB147/SB157 ON. Solder single morpho pins CN11-6, CN11-8, CN11-21, CN11-23, CN12-10 and CN12-28. W1 DMM check (unchanged from v2.1).

### A.6 Stimulus for P1-F767: second NUCLEO-F767ZI (v2.1, unchanged)

| Chan | Kind | Stim pin (connector) | EXTI | DUT end |
|---|---|---|---|---|
| di0 | di out 0/1/z (`drive-high`) | PE14 (CN10-28) | – | PC13 CN11-23 |
| di1 | di | PF15 (CN10-12) | – | PF15 |
| di2 | di | PF14 (CN10-8) | – | PF14 |
| do0..do4 | do in, pull-down | PE7 (CN10-20), PE8 (CN10-18), PE10 (CN10-24), PF13 (CN10-2), PF12 (CN7-20) | 7, 8, 10, 13, 12 | PB0, PB7, PB14, PF13, PF12 |
| ai0 / ai3 | DAC1_OUT1 / OUT2 + sense | src PA4 / PA5; sense PA3 / PF3 | – | PA3 / PF3 |
| ai1, ai2, ai4, ai5 | sense | PC0, PC3, PF5, PF10 | – | same pins |
| ao0 / ao1 / ao2 | TIM5_CH1 / TIM2_CH1 / TIM4_CH1 capture | PA0 (CN10-29), PA15 (CN7-9), PD12 (CN10-21) | never armed | PE9 / PE11 / PE13 |
| nrst / nrst_sense / pwr / sync / lb | rig | PE4 (CN9-16) / PE15 (CN10-30) / PE5 (CN9-18) / PE6 (CN9-20) / PB11 (CN10-34) | – / 15 / – / – / 11 | |
| m0..m3 | marker in | PG0, PG1, PG2, PG3 | 0-3 | PG0..PG3 |
| v3v3 / v5 | sense ×2 | PB1 (CN10-7) / PF4 (CN10-11) | – | CN8-7 / E5V |
| Injector (stimulus side) | USART2 hardware DE | PD5 / PD6 / PD4 | – | U2 |
| I2C1 / I2C2 | AFE | PB8/PB9, PF1/PF0 | – | MCP4728, ADS1115, INA226 |

The v2.1 §2.1 reset-state exception applies: PA15 (JTDI) has a pull-up during reset.

### A.7 LA map for P1-F767

- **It1:** nrst CN8-5, m0 PG0, m1 PG1, di1 PF15, do3 PF13, do0 PB0, vcp_tx PD8 (after SB7), sync.
- **It2:** de = PD15 CN7-18, tx = PG14 CN10-14, rx = PG9 CN10-16; the other channels as v2.1.

## Appendix B: P3 NUCLEO-H563ZI (optional, MQTT only; unchanged)

| Function | Where / note |
|---|---|
| Ethernet | RJ45; MAC 02:80:E1 + crc32(UID) |
| Console | STLINK-V3EC VCP |
| NRST | Arduino RESET position [likely CN8-5] |
| Power | uhubctl on CN1 |
| Runner | stm32cubeprogrammer / pyocd / jlink (no openocd runner in 4.4.2) |