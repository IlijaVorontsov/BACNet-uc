# HIL pin tables (v2.1)

Conventions:
- "stim end" means the series resistor sits at the stimulus board.
- Levels on DUT channels are physical wire levels. hilrig applies the catalog polarity (flag `GPIO_ACTIVE_LOW`).
- Connector positions come from UM1974 Rev 8 Table 19 (Zio) and Table 21 (morpho) for MB1137, and from UM12018 Tables 17-20 for FRDM-MCXN947.
- Everything in these tables was verified this session unless it is tagged [likely] or [proposed].

## 1. P1 DUT: NUCLEO-F767ZI (MB1137), catalog `firmware/boards/io/nucleo_f767zi.dtsi` + snippets `hil`, `hil-io`

### 1.1 Catalog channels (release images)

The ids are the devicetree order. Every DUT pin listed is FT.

| Id | Chan | Kind / flags | MCU | Connector | Stimulus side (1 kΩ at stim end) | Notes |
|---|---|---|---|---|---|---|
| 0 | di0 | di, ACTIVE_HIGH (B1) | PC13 | **CN11-23** (morpho; solder a single pin) | push-pull, active = 1, inactive = z | B1 net: R59 330 Ω, R58 220 k pull-down |
| 1 | di1 | di, ACTIVE_LOW, PULL_UP | PF15 | CN10-12 (D2) | active = 0, inactive = z (dry contact) | 0 V drive → pin ≈ 0.1 V against the 30-50 k RPU |
| 2 | di2 | di, ACTIVE_LOW, PULL_UP | PF14 | CN10-8 (D4) | same as di1 | |
| 3 | do0 | do (LD1 green; also `led0` of mqtt_tls) | PB0 | CN10-31 (D33) / CN11-34 | input, pull-down | LD1 load via SB120 |
| 4 | do1 | do (LD2 blue) | PB7 | **CN11-21** | input | 680 Ω + LED |
| 5 | do2 | do (LD3 red) | PB14 | **CN12-28** | input | 1 kΩ + LED |
| 6 | do3 | do | PF13 | CN10-2 (D7) | input | push-pull |
| 7 | do4 | do | PF12 | CN7-20 (D8) | input | |
| 8 | ai0 | ai ADC1_IN3 | PA3 | CN9-1 (A0) | DAC src + ADC sense; 100 nF at the DUT pin | 0-3.3 V (VREF+ = VDDA); AVDD sense at CN10-1 |
| 9 | ai1 | ai ADC1_IN10 | PC0 | CN9-3 (A1) | It1 divider 10k/10k → 1650 mV; It2 MCP4728 #1 A | |
| 10 | ai2 | ai ADC1_IN13 | PC3 | CN9-5 (A2) | It1 20k/10k → 1100 mV; It2 MCP4728 #1 B | |
| 11 | ai3 | ai ADC3_IN9 | PF3 | CN9-7 (A3) | DAC2 src + sense | one swept channel on each ADC |
| 12 | ai4 | ai ADC3_IN15 | PF5 | CN9-9 (A4) | It1 10k/20k → 2200 mV; It2 MCP4728 #1 C | SB147 ON / SB138 OFF |
| 13 | ai5 | ai ADC3_IN8 | PF10 | CN9-11 (A5) | It1 33k/10k → 767 mV; It2 MCP4728 #1 D | SB157 ON / SB143 OFF |
| 14 | ao0 | ao TIM1_CH1 1 kHz | PE9 | CN10-4 (D6) | timer capture | direct to D6 (no solder bridge on MB1137) |
| 15 | ao1 | ao TIM1_CH2 | PE11 | CN10-6 (D5) | capture | |
| 16 | ao2 | ao TIM1_CH3 | PE13 | CN10-10 (D3) | capture | also the board pwm1 pin |

The dividers have distinct values so that swapped wiring shows up in R-02a.

### 1.2 HIL extras (instrumented images)

| Function | MCU | Connector | Name | Wired to | Iteration |
|---|---|---|---|---|---|
| Marker m0..m3 | PG0 / PG1 / PG2 / PG3 | CN9-29 / CN9-30 / CN8-14 / CN8-16 | `hil-marker-gpios` | stim m0..m3, LA | It1 (m0/m1 used) |
| di3 / di4 (hil-io) | PE10 / PE12 | CN10-24 / CN10-26 | appended catalog ids 17/18, ACTIVE_HIGH + PULL_DOWN | stim di3 / di4 | It2 |
| do5 / do6 (hil-io) | PE14 / PE15 | CN10-28 / CN10-30 | ids 19/20 | stim do5 / do6 | It2 |
| MS/TP TX | PD5 | CN9-6 (D53) | `usart2_tx_pd5` | U1 DI, LA `tx` | It2 (gated) |
| MS/TP RX | PD6 | CN9-4 (D52) | `hil_usart2_rx_pd6_pu` | U1 RO (10 k pull-up to DUT 3V3), LA `rx` | It2 (gated) |
| MS/TP DE | PD4 | CN9-8 (D54) | `usart2_de_pd4` | U1 DE + /RE (10 k pull-down), LA `de` | It2 (gated) |
| MS/TP alternative (BACnet roadmap) | PG14 TX / PG9 RX / PD15 DE | CN10-14 / CN10-16 / CN7-18 | USART6 + GPIO DE | U1 via a 3-jumper selector | if FW-07 is declined |

### 1.3 Rig connections

| Function | Where | Circuit |
|---|---|---|
| NRST | **CN8-5** (Zio RESET). Also CN11-14 and CN6-5. | BSS138P drain via 100 Ω (stim `nrst`); 10 k to stim `nrst_sense` (no pull) |
| Power in | E5V **CN11-6**, GND **CN11-8**: solder only these two pins (same even row, 2.54 mm apart), keyed 2-pin housing. **CN11-5 = VDD and CN11-7 = BOOT0 stay unpopulated.** | load switch VOUT; JP3 1-2, JP1 OFF |
| 3V3 sense | CN8-7 | 100k/100k + 100 nF → stim `v3v3` (PB1) |
| AVDD sense | CN10-1 | ADS1115 (It2) through 10 kΩ |
| VCP TX tap | PD8 at CN12-10 via SB7 | LA only, after the continuity check |
| SPI NOR W25Q128JV (/lfs) | CLK PA5 CN7-10, DO PA6 CN7-12, DI PB5 CN7-13, /CS PD14 CN7-16, VCC CN8-7, GND CN8-11 | per the BACnet overlay; /WP and /HOLD to 3V3 |
| GND | CN8-11/13, CN9-12/23, CN10-5/17/22/27, CN7-8, CN11-8 | star ground, ≥ 2 wires to the stimulus |

### 1.4 Keep-out on the DUT (never wire)

| Group | Pins |
|---|---|
| Supply and boot pins on CN11 | CN11-5 (VDD), CN11-7 (BOOT0) |
| RMII | PA1, PA2, PA7 (also CN7-14 D11 and CN9-15), PB13 (also CN7-5 D18), PC1, PC4, PC5, PG11, PG13 |
| VCP | PD8, PD9 (PD8 only as the LA tap) |
| USB | PA8-PA12, PG6, PG7 |
| SWD/SWO | PA13, PA14, PB3 |
| Clocks | PH0, PH1, PC14, PC15 |
| NOR | PA5, PA6, PB5, PD14 |
| Board nodes | I2C1 PB8/PB9, USART6 PG9/PG14 (except the MS/TP alternative), CAN1 PD0/PD1, DAC PA4 |

### 1.5 Jumper and solder-bridge checklist (UM1974 Table 12 defaults and the MB1137 B-01 schematic)

| Item | Default | Rig | Reason |
|---|---|---|---|
| JP3 | 3-4 U5V | **1-2 E5V** | power cut on E5V; the ST-LINK stays on USB via D5 |
| JP1 | OFF | OFF | required with E5V |
| CN4, JP5, JP6, JP7, JP4 | ON | ON | ST-LINK to the MCU; IDD; RMII |
| SB5 / SB6 | ON | ON | VCP |
| SB7 / SB4 | conflicting in the docs | **continuity check** | PD8 tap |
| SB111, SB110, SB177 | ON | ON | NRST ↔ ST-LINK, SWO, NRST ↔ PHY |
| SB112, SB149 | ON | ON | HSE from the ST-LINK MCO |
| SB119 | OFF | **OFF** | PA5 is the NOR clock |
| SB121 / SB122 | ON / OFF | unchanged | D11 = PA7 = CRS_DV |
| SB147, SB157 | ON | ON | A4/A5 = ADC |
| Morpho CN11/CN12 | not soldered [likely] | solder single pins CN11-6, CN11-8, CN11-21, CN11-23, CN12-10, CN12-28; **never CN11-5 (VDD) or CN11-7 (BOOT0)** | E5V, GND, PB7, PC13, PD8, PB14. A lead one pin off puts 5.1 V on VDD (abs max 4.0 V); a bridge CN11-5/7 sets BOOT0 = 1 |
| W1 DMM check | – | CN11-6 to GND = E5V; CN11-5 and CN11-7 unwired | before the first power-up |

## 2. Stimulus (NUCLEO-F767ZI running `apps/hil_stimulus`, profile P1)

The stimulus avoids:
- RMII, VCP (PD8/PD9), USB, SWD/SWO, clocks, B1 PC13;
- PE2, which appears on two Zio pins;
- morpho-only positions: every stimulus pin below is on a Zio header, so the stimulus needs no morpho soldering.

`gpio_keys` is disabled, which frees EXTI13.

| Chan | Kind | Stim pin (connector) | EXTI | Network | DUT end |
|---|---|---|---|---|---|
| di0 | di out 0/1/z | PE14 (CN10-28) | – | 1 kΩ | PC13 CN11-23 |
| di1 | di | PF15 (CN10-12) | – | 1 kΩ | PF15 CN10-12 |
| di2 | di | PF14 (CN10-8) | – | 1 kΩ | PF14 CN10-8 |
| do0 | do in, pull-down | PE7 (CN10-20) | 7 | 1 kΩ | PB0 CN10-31 |
| do1 | do | PE8 (CN10-18) | 8 | 1 kΩ | PB7 CN11-21 |
| do2 | do | PE10 (CN10-24) | 10 | 1 kΩ | PB14 CN12-28 |
| do3 | do | PF13 (CN10-2) | 13 | 1 kΩ | PF13 CN10-2 |
| do4 | do | PF12 (CN7-20) | 12 | 1 kΩ | PF12 CN7-20 |
| ai0 | src DAC1_OUT1 + sense | src PA4 (CN7-17); sense PA3 (CN9-1, ADC1_IN3) | – | src 1 kΩ to the node, 100 nF node→GND at the DUT pin, sense 1 kΩ | PA3 CN9-1 |
| ai1 | sense (It1 divider; It2 MCP4728 #1 A) | PC0 (CN9-3) | – | DUT 3V3 → 10k/10k → node, 100 nF | PC0 CN9-3 |
| ai2 | sense | PC3 (CN9-5) | – | 20k/10k | PC3 CN9-5 |
| ai3 | src DAC1_OUT2 + sense | src PA5 (CN7-10); sense PF3 (CN9-7, ADC3_IN9) | – | as ai0 | PF3 CN9-7 |
| ai4 | sense | PF5 (CN9-9) | – | 10k/20k | PF5 CN9-9 |
| ai5 | sense | PF10 (CN9-11) | – | 33k/10k | PF10 CN9-11 |
| ao0 | capture TIM5_CH1 AF2 (32-bit); TIM AF + pull-down for the whole run | PA0 (**CN10-29** via SB179; also CN11-28) | never armed (line 0 = m0) | 1 kΩ, pull-down | PE9 CN10-4 |
| ao1 | capture TIM2_CH1 AF1 (32-bit); PA15 = JTDI (see §2.1) | PA15 (CN7-9) | never armed (line 15 = nrst_sense) | 1 kΩ, pull-down | PE11 CN10-6 |
| ao2 | capture TIM4_CH1 AF2 (16-bit, prescaler 10: 101.9 ns, ≤ 6.67 ms) | PD12 (**CN10-21**, D29) | never armed (line 12 = do4) | 1 kΩ, pull-down | PE13 CN10-10 |
| nrst | FET gate (1 = NRST low) | PE4 (CN9-16) | – | 100 Ω to the gate, 100 k gate→GND | DUT NRST CN8-5 via 100 Ω drain |
| nrst_sense | input, **no pull**, ACTIVE_LOW | PE15 (CN10-30) | 15 (rstmon, permanent) | 10 kΩ | DUT NRST |
| pwr | FET gate, ACTIVE_LOW (logical 1 = DUT on) | PE5 (CN9-18) | – | 100 Ω, 100 k gate→GND; drain on switch ON with 10 k to +5VA | load switch |
| sync | out | PE6 (CN9-20) | – | 100 Ω | LA `sync` |
| lb | marker (self-test) | PB11 (CN10-34) | 11 | jumper from SYNC | – |
| m0..m3 | marker in, pull-down | PG0 (CN9-29), PG1 (CN9-30), PG2 (CN8-14), PG3 (CN8-16) | 0-3 | 1 kΩ | DUT PG0..PG3 (pin-to-pin) |
| v3v3 | sense ×2 | PB1 (CN10-7, A6, ADC1_IN9) | – | 100k/100k + 100 nF, 480 cycles | DUT CN8-7 |
| v5 | sense ×2 | PF4 (**CN10-11**, A8, ADC3_IN14) | – | 100k/100k + 100 nF | switch output (E5V) |
| rs485_a (It2, gated) [proposed] | sense ×2 | PC2 (CN10-9, A7, ADC1_IN12) | – | 100k/100k **0.1 %** + 100 nF (mandatory: analog pin, not FT) | bus D+ |
| rs485_b (It2, gated) [proposed] | sense ×2 | PA6 (CN7-12, D12, ADC1_IN6; spi1 disabled on the stimulus) | – | as rs485_a | bus D- |
| RS-485 injector (It2, gated) | USART2 HW DE (8/16 bit) | TX PD5 (CN9-6), RX PD6 (CN9-4), DE PD4 (CN9-8) | – | U2 (VCC = stim 3V3): DE 10 k pull-down, /RE to GND, RO 10 k pull-up to stim 3V3 | bench bus |
| I2C1 (It2) | controller | SCL PB8 (CN7-2), SDA PB9 (CN7-4) | – | 4.7 k to stim 3V3 | MCP4728 #1 0x60, ADS1115 0x48/0x49, INA226 0x40 |
| I2C2 (It2) [proposed] | controller | SCL PF1 (CN9-19), SDA PF0 (CN9-21) | – | 4.7 k to stim 3V3 | MCP4728 #2 0x60 (P2 analog sources via the shield) |
| di3 / di4 (It2, hil-io) [proposed] | di out | PF7 (CN9-26) / PF8 (CN9-24) | – | 1 kΩ | PE10 / PE12 |
| do5 / do6 (It2, hil-io) [proposed] | do in | PF9 (CN9-28) / PB6 (CN10-13, D26) | 9 / 6 | 1 kΩ | PE14 / PE15 |
| Relays K1-K7 (It3) | out → ULN2803A | 7 of PE3 (CN9-22), PC6 (CN7-1), PC7 (CN7-11), PB15 (CN7-3), PB12 (CN7-7), PB10 (CN10-32), PE12 (CN10-26), PD7 (CN9-2), PD3 (CN9-10) | – | – | relay box (K1-K3 NC, K4-K7 NO) |

EXTI lines armed by `edges`, `lat` and `rstmon`: 0-3 (m0-m3), 7, 8, 10 (do0-do2), 11 (lb), 12 (do4), 13 (do3), 15 (nrst_sense; owned permanently by rstmon). The ao pins sit on lines 0 (PA0), 15 (PA15) and 12 (PD12), which other ports already own, so ao is never armed: read it with `din` or `pwmcap`. With the proposed do5/do6 add 9 and 6. Free: 4, 5, 14.

The proposed rows need an edtlib, EXTI and build check when they are added to the stimulus overlay.

### 2.1 Reset-state exceptions on the stimulus

F7 pins are floating inputs during and just after reset, except the JTAG pins (ST HAL GPIO header). PA15 (JTDI, ao1) comes out of reset in AF mode with an internal pull-up [likely: RM0410 not reachable], so while the stimulus is in reset DUT PE11 sees about 40 kΩ to 3.3 V through 1 kΩ. This biases only boot-state readings of ao1; no test reads ao1 during a stimulus reset. PB4 (NJTRST) is not used (do6 moved to PB6).

## 3. Logic analyzer

**It1: FX2** (`bench.yml la: {driver: fx2lafw, channels: {nrst: 0, m0: 1, m1: 2, di1: 3, do3: 4, do0: 5, vcp_tx: 6, sync: 7}}`, 24 MS/s, host root port):

| CH | Signal | Probe point |
|---|---|---|
| 0 | nrst | DUT CN8-5 |
| 1 | m0 | DUT PG0 CN9-29 |
| 2 | m1 | DUT PG1 CN9-30 |
| 3 | di1 | DUT PF15 CN10-12 (DUT side of 1 kΩ) |
| 4 | do3 | DUT PF13 CN10-2 |
| 5 | do0 | DUT PB0 CN10-31 |
| 6 | vcp_tx (or m2) | DUT PD8 CN12-10 after the SB7 check (else PG2 CN8-14) |
| 7 | sync | stim PE6 |

**It2: DSLogic Plus 16 ch** (USB 2a0e:0020 only; otherwise the Saleae Logic Pro 16 with the same names).

| CH | Signal |
|---|---|
| 0 | de (PD4) |
| 1 | tx (PD5) |
| 2 | bus_ro (U3 sniffer RO, 3.3 V) |
| 3 | nrst |
| 4 | m0 |
| 5 | m1 |
| 6 | di1 |
| 7 | do3 |
| 8 | rx (PD6) |
| 9 | stim_de (stim PD4) |
| 10 | m2 (PG2) |
| 11 | m3 (PG3) |
| 12 | ao0 (PE9) |
| 13 | sync |
| 14 | vcp_tx |
| 15 | do0 |

Rates and capture lengths:
- MS/TP: stream mode at 20 MHz × 16.
- Latency, boot and ISR timings (R-09, TIM-01/02/04): 100 MHz × 16 ch (10 ns) in buffer windows of ≤ 0.16 s. 400 MHz is not used: it samples only CH0-3 (de, tx, bus_ro, nrst).
- Every timing test declares its rate and length.

## 4. P2 DUT: FRDM-MCXN947 (`frdm_mcxn947/mcxn947/cpu0`, It2)

Arduino outer rows only. I/O is not 5 V tolerant (VDIO max = VDD_Px + 0.3 V, injection ±3 mA per pin), so it uses **2.2 kΩ** at the stim end.

The catalog is from the BACnet branch `frdm_mcxn947_mcxn947_cpu0.dtsi`; the polarity details follow the profiles research [likely].

| Chan | Kind | MCU | FRDM header (Arduino) | Stim role | Note |
|---|---|---|---|---|---|
| di0 | di, act-low, pull-up (SW2) | P0_23 | J4-12 (A5) | out 0/z (shield GPIO path, §6) | SW2 0.1 µF cap (τ ≈ 0.2 ms through 2.2 k) |
| di1 | di (SW3 = ISPMODE_N) | P0_6 | not on a header | **never driven** | ISP pin |
| di2 | di, act-low, pull-up | P0_29 | J1-6 (D2) | out 0/z | |
| di3 | di, act-low, pull-up | P0_30 | J1-10 (D4) | out 0/z | SJ2 1-2 |
| do0 | do, act-low (red LED, led0) | P0_10 | J2-4 (D9) | in | mqtt identify visible here |
| do1 | do, act-low (green) | P0_27 | J2-6 (D10) | in | |
| do2 | do, act-low (blue) | P1_2 | J1-14 (D6) | in | |
| do3 | do | P0_31 | J1-16 (D7) | in | |
| do4 | do | P0_28 | J2-2 (D8) | in | |
| ai0 | ai LPADC0 CH14B, VREFO 1.8 V full scale | P0_14 | J4-6 (A2) | src MCP4728 #2 A + sense | MCP4728 internal 2.048 V, gain 1 (0.05 V is below the on-chip DAC minimum of 0.2 V) |
| ai1 | CH14A | P0_22 | J4-8 (A3) | src MCP4728 #2 B + sense | |
| ai2 | CH15B | P0_15 | J4-10 (A4) | src MCP4728 #2 C + sense | SJ8 1-2 |
| ao0 | FlexPWM1 | P2_6 | J3-15 (inner row, pigtail x0) | capture | shares SDHC/SAI pins; assert `CONFIG_I2S=n` and SDHC off |
| ao1 | FlexPWM1 | P2_7 | J3-13 (pigtail x1) | capture | |
| ao2 | SCT0_OUT5 | P1_23 | J1-8 (D3) | capture | SJ1 1-2 |
| RESET | RESET_B | – | J3-6 | NRST FET + sense | SW1 0.1 µF gives a slow release; filter ≥ 330 ns |
| Console | LPUART4 P1_8 RX / P1_9 TX | – | MCU-Link VCOM on J17; LA tap J9-30 (TX) | – | 115200 |
| Ethernet | ENET-QoS RMII, LAN8741A, MDIO P1_21 | – | RJ45 | – | MAC AE:9A:22 + CRC-24 of the UID (`nxp,unique-mac`) |
| Power | J24 feeds P3V3_MCU | – | It2: uhubctl on J17; J24 load switch only after the back-power measurement | – | |
| **Forbidden** | D5 = P1_21 ENET MDIO (J1-12) | | | | |
| Caution | D0/D1 | | | | 'MCU-Link UART' conflict noted in UM12018: continuity check |

## 5. P3 DUT: NUCLEO-H563ZI (optional, MQTT only)

| Function | Where | Note |
|---|---|---|
| Ethernet | RJ45 | MAC 02:80:E1 + crc32 (UID) per D29 (no MAC set) |
| Console | STLINK-V3EC VCP | mqtt_tls logs only |
| NRST | Arduino RESET position [likely CN8-5; re-check] | FET + sense |
| Power | uhubctl on CN1 | drops the probe as well |
| Runner | stm32cubeprogrammer (default) / pyocd / jlink | no openocd runner in 4.4.2 |

## 6. It2 Arduino-R3 HIL shield (outline) [proposed]

Outer-row single pins with stacking headers and 2.2 kΩ per line. Digital positions keep the same stimulus pin across profiles. The analog-source paths and P2 A5 change role, so they go through per-profile shunts; the sense path of each A position is shared.

| Position | P1 | P2 | Shunt rule |
|---|---|---|---|
| A0 | ai0: on-chip DAC1 (PA4) + sense PA3 | unused | source shunt closed on P1 only |
| A1 | ai1: divider (It1) / MCP4728 #1 A | unused | source shunt closed on P1 only |
| A2 | ai2: MCP4728 #1 B | ai0: MCP4728 #2 A | select P1 = #1 B, P2 = #2 A |
| A3 | ai3: on-chip DAC2 (PA5) | ai1: MCP4728 #2 B | select P1 = DAC2, P2 = #2 B (P2 needs 0.05 V) |
| A4 | ai4: MCP4728 #1 C | ai2: MCP4728 #2 C | select P1 = #1 C, P2 = #2 C |
| A5 | ai5: MCP4728 #1 D | di0 (SW2): stim di0 GPIO path, drives 0/z | select P1 = #1 D, P2 = GPIO; **never both** (MCP4728 at code 0 would hold di0 active) |
| D0, D1 | OPEN (USART6) | OPEN (FC2 / MCU-Link UART) | always open |
| D5 | closed | **OPEN** (P1_21 ENET MDIO) | |
| D9, D10 | OPEN (PD15 MS/TP alt DE; PD14 NOR CS) | closed (do0, do1: DUT outputs → stim inputs) | |
| D11-D13 | **OPEN** (NOR, RMII) | open unless used | |
| D14, D15 | OPEN (I2C1) | OPEN (FC2 I2C) | always open |
| 3V3, 5V, VIN | never connected | never connected | |

Only the stimulus profile overlay renames the channels (D22).
