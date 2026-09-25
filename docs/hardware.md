# Hardware

BACnet-uc runs on two evaluation boards and on the `native_sim` host target.
This document lists the hardware facts the firmware depends on, the IO channel
catalogs, the storage options, the planned RS-485 extension and a bill of
materials for a bench set-up.

Facts are taken from the Zephyr v4.4.2 board and SoC devicetree files
(`boards/st/nucleo_f767zi`, `boards/nxp/frdm_mcxn947`, `dts/arm/st/f7`,
`dts/arm/nxp/mcx`) and from the overlays in
[`firmware/boards/`](../firmware/boards/). Values that come from vendor
documentation and are not encoded in Zephyr are marked as such.

## 1. Board overview

| | NUCLEO-F767ZI | FRDM-MCXN947 | native_sim |
|---|---|---|---|
| Zephyr board target | `nucleo_f767zi` | `frdm_mcxn947/mcxn947/cpu0` | `native_sim/native/64` |
| MCU | STM32F767ZIT6 | MCXN947VDF | Linux process (x86-64) |
| CPU used | Cortex-M7, 216 MHz (PLL from the 8 MHz ST-LINK clock: /4 ×216 /2) | Cortex-M33 core 0, 150 MHz (PLL0); core 1 unused | host CPU |
| FPU | double precision (FPv5); firmware built with `CONFIG_FP_HARDABI=y` | single precision; `CONFIG_FP_HARDABI=y` (double arithmetic in software) | host |
| Internal flash | 2 MiB, single bank: 4 × 32 KiB, 1 × 128 KiB, 7 × 256 KiB sectors | 2 MiB dual bank, 8 KiB sectors, 16-byte write unit | simulated 2 MiB, 4 KiB erase blocks, file `flash.bin` |
| RAM | 512 KiB: DTCM 128 KiB @ 0x2000_0000 + SRAM1/2 384 KiB @ 0x2002_0000 (`zephyr,sram`) | 512 KiB: SRAMX 96 KiB @ 0x0400_0000 + SRAM A-H 416 KiB @ 0x2000_0000. Zephyr's default split gives cpu0 320 KiB; the firmware overlay gives cpu0 SRAM A-G, 384 KiB (cpu1 is not used, SRAM H stays unused) | host memory |
| WAMR pool (chosen `uc,app-pool`) | 112 KiB in DTCM, next to the Ethernet DMA buffers | 96 KiB, all of SRAMX | 256 KiB of process memory |
| External flash | none on the board; SPI NOR added for `/lfs` (section 4) | Winbond W25Q64JV, 8 MiB QSPI NOR on FlexSPI, 4 KiB sectors, memory mapped at 0x9000_0000 | - |
| Ethernet | MAC with RMII, on-board PHY LAN8742A (per ST UM1974; Zephyr uses the generic MDIO PHY driver at address 0) | ENET QoS MAC with RMII, on-board PHY at MDIO address 0 (generic `ethernet-phy` driver) | host sockets (NSOS) |
| MAC address | derived from the MCU unique ID by `eth_stm32_hal` | derived from the unique ID (`nxp,unique-mac` in the overlay; the board default is random) | - |
| Console | USART3 (PD8 TX, PD9 RX) via ST-LINK/V2-1 virtual COM port, 115200 8N1 | FLEXCOMM4 LPUART4 (P1_9 TX, P1_8 RX) via MCU-Link VCOM, 115200 8N1 | pseudo-terminal (`/dev/pts/N`), printed at start |
| Debug probe | on-board ST-LINK/V2-1 | on-board MCU-Link (CMSIS-DAP) | gdb |
| `west flash` runners | `stm32cubeprogrammer` (default), `openocd`, `jlink` | `linkserver` (default), `jlink`, `pyocd` | - |
| Memory protection | ARMv7-M MPU, MPU stack guards | ARMv8-M MPU, stack limit registers, TrustZone (firmware runs secure) | - |
| WAMR build target | `THUMBV7EM_VFP` | `THUMBV8M.MAIN_VFP` | `X86_64` |
| IO voltage | 3.3 V (most pins 5 V tolerant, not the ADC inputs) | 3.3 V; ADC reference 1.8 V (section 3.2) | - |

## 2. NUCLEO-F767ZI

### 2.1 Why the internal flash does not hold `/lfs`

The STM32F767 flash in single-bank mode (the Zephyr default) has 32 KiB
sectors at the start, a 128 KiB sector and 256 KiB sectors after that. That
makes an on-chip LittleFS impractical:

- **Block size.** LittleFS erases whole blocks. With 32 KiB blocks, the
  board's 64 KiB `storage_partition` (sectors 2-3 at 0x1_0000) holds two
  blocks, which is not enough for a superblock pair, a directory pair and
  files. Larger partitions would use 128/256 KiB blocks: every small file
  or append would erase 256 KiB.
- **Erase time and stalls.** A 256 KiB sector erase takes up to 4 s
  (`max-erase-time` in `stm32f7.dtsi`). The CPU executes from the same
  single-bank flash, so code fetches stall while the flash is busy: the
  network and the BACnet thread would stop for seconds on every erase.
- **Endurance.** The internal flash is specified for 10 000 erase cycles per
  sector (ST datasheet), an order of magnitude less than serial NOR.
- **Layout.** Without MCUboot, the firmware is linked at flash offset 0 and
  its image (larger than 64 KiB) overlaps the board's `storage_partition`.

The firmware therefore puts `/lfs` on an external SPI NOR flash (default
overlay) or, without one, on a RAM disk (snippet `uc-ramfs`, volatile).

### 2.2 Pins used by the board and the firmware

| Function | Pins | Notes |
|----------|------|-------|
| Ethernet RMII | PA1 REF_CLK, PA2 MDIO, PA7 CRS_DV, PB13 TXD1, PC1 MDC, PC4 RXD0, PC5 RXD1, PG11 TX_EN, PG13 TXD0 | PA7 is also Arduino D11 (SPI1 MOSI): the SPI NOR uses PB5 instead |
| Console | PD8, PD9 | ST-LINK VCP |
| SPI NOR (`/lfs`) | PA5 SCK (D13), PA6 MISO (D12), PB5 MOSI (CN7-13), PD14 /CS (D10) | `nucleo_f767zi.overlay` |
| IO catalog | section 3.1 | |

Silicon revision: Zephyr's board documentation reports Ethernet instability
on cut-A devices (device marking "A", ST errata sheet DM00257543) and
recommends cut-Z.

## 3. IO channel catalogs

Each board declares its channels in a devicetree node with compatible
`uc,io-channels` ([io.md](io.md#2-the-channel-catalog)). Values: `di`/`do`
0 or 1, `ai` millivolts, `ao` duty cycle 0..100 %.

### 3.1 NUCLEO-F767ZI (`firmware/boards/io/nucleo_f767zi.dtsi`)

| Channel | Kind | Hardware | MCU pin | Connector | Notes |
|---------|------|----------|---------|-----------|-------|
| `di0` | di | GPIO | PC13 | B1 USER button | pressed = 1 (`GPIO_ACTIVE_HIGH`; the board DTS declares B1 active low, the Nucleo-144 button pulls PC13 high) |
| `di1` | di | GPIO | PF15 | Arduino D2 | internal pull-up, contact to GND = 1 |
| `di2` | di | GPIO | PF14 | Arduino D4 | internal pull-up, contact to GND = 1 |
| `do0` | do | GPIO | PB0 | LD1 green LED | |
| `do1` | do | GPIO | PB7 | LD2 blue LED | |
| `do2` | do | GPIO | PB14 | LD3 red LED | |
| `do3` | do | GPIO | PF13 | Arduino D7 | push-pull, 1 = high |
| `do4` | do | GPIO | PF12 | Arduino D8 | push-pull, 1 = high |
| `ai0` | ai | ADC1_IN3 | PA3 | Arduino A0 | 0..3300 mV, 12 bit |
| `ai1` | ai | ADC1_IN10 | PC0 | Arduino A1 | 0..3300 mV |
| `ai2` | ai | ADC1_IN13 | PC3 | Arduino A2 | 0..3300 mV |
| `ai3` | ai | ADC3_IN9 | PF3 | Arduino A3 | 0..3300 mV |
| `ai4` | ai | ADC3_IN15 | PF5 | Arduino A4 | 0..3300 mV |
| `ai5` | ai | ADC3_IN8 | PF10 | Arduino A5 | 0..3300 mV |
| `ao0` | ao | TIM1_CH1 | PE9 | Arduino D6 | PWM 1 kHz, 0..100 % |
| `ao1` | ao | TIM1_CH2 | PE11 | Arduino D5 | PWM 1 kHz |
| `ao2` | ao | TIM1_CH3 | PE13 | Arduino D3 | PWM 1 kHz |

ADC: reference VREF+ = VDDA = 3.3 V, 144 cycles sampling time, ADC clock
PCLK2/4 = 27 MHz (the board default PCLK2/2 = 54 MHz exceeds the 36 MHz
limit of the F7 ADC). PWM: TIM1 with prescaler 10 (21.6 MHz), so a 1 ms
period has 21 600 steps. Pins deliberately not used: RMII, SPI NOR, console,
USART6 (D0/D1, reserved for RS-485), I2C1 (D14/D15).

### 3.2 FRDM-MCXN947 (`firmware/boards/io/frdm_mcxn947_mcxn947_cpu0.dtsi`)

| Channel | Kind | Hardware | MCU pin | Connector | Notes |
|---------|------|----------|---------|-----------|-------|
| `di0` | di | GPIO | P0_23 | SW2 user button | pressed = 1 (also Arduino A5) |
| `di1` | di | GPIO | P0_6 | SW3 ISP button | pressed = 1 |
| `di2` | di | GPIO | P0_29 | Arduino D2 | pull-up, contact to GND = 1 |
| `di3` | di | GPIO | P0_30 | Arduino D4 | pull-up, contact to GND = 1 |
| `do0` | do | GPIO | P0_10 | RGB LED red | 1 = on (also Arduino D9) |
| `do1` | do | GPIO | P0_27 | RGB LED green | 1 = on (also Arduino D10) |
| `do2` | do | GPIO | P1_2 | RGB LED blue | 1 = on (also Arduino D6) |
| `do3` | do | GPIO | P0_31 | Arduino D7 | push-pull |
| `do4` | do | GPIO | P0_28 | Arduino D8 | push-pull |
| `ai0` | ai | LPADC0 CH14B | P0_14 | Arduino A2 | 0..1800 mV, 12 bit |
| `ai1` | ai | LPADC0 CH14A | P0_22 | Arduino A3 | 0..1800 mV |
| `ai2` | ai | LPADC0 CH15B | P0_15 | Arduino A4 | 0..1800 mV |
| `ao0` | ao | FlexPWM1 SM0 A | P2_6 | J3-15 | PWM 1 kHz |
| `ao1` | ao | FlexPWM1 SM0 B | P2_7 | J3-13 | PWM 1 kHz |
| `ao2` | ao | SCTimer0 OUT5 | P1_23 | Arduino D3 | PWM 1 kHz |

- LPADC0 uses the on-chip VREF at **1.8 V** as reference: inputs above 1.8 V
  read full scale. Use a divider for 0..3.3 V or 0..10 V signals and set
  `scale` in `io.json` accordingly.
- Arduino A0/A1 are not in the board's Arduino GPIO map and A5 is SW2, so the
  catalog uses A2..A4.
- The RGB LED shares pins with Arduino D6/D9/D10; configuring the GPIO
  switches the pin mux.
- Not in the catalog: DAC0/DAC2 (the binding has no DAC property), Arduino D5
  (P1_21 is ENET MDIO).

### 3.3 native_sim (`firmware/boards/io/native_sim_native_64.dtsi`)

`di0`, `di1`, `do0`, `do1`, `ai0` (initial 2000 mV), `ai1`, `ao0`, `ao1`, all
`simulated`. Inputs are set with `uc_io force`; outputs follow their objects
and can be read back.

## 4. Storage options

| Board | Default `/lfs` backing | Size | Erase block | Survives reset |
|-------|------------------------|------|-------------|----------------|
| NUCLEO-F767ZI | external SPI NOR W25Q128JV on SPI1 | 16 MiB | 4 KiB | yes |
| FRDM-MCXN947 | on-board W25Q64JV (8 MiB) via FlexSPI, `storage_partition` 0x000000-0x7EFFFF | 8128 KiB (2032 blocks) | 4 KiB | yes |
| native_sim | flash simulator, partition @ 0x75000 | 1580 KiB | 4 KiB | yes (`flash.bin`, `--flash=<file>`) |
| any, snippet `uc-ramfs` | RAM flash simulator | 64 KiB | 1 KiB | no |

On the FRDM-MCXN947 the overlay
([`frdm_mcxn947_mcxn947_cpu0.overlay`](../firmware/boards/frdm_mcxn947_mcxn947_cpu0.overlay))
shrinks the board's `storage_partition` to the first 8128 KiB of the
W25Q64JV. The top 64 KiB (0x7F0000-0x7FFFFF) are reserved for the settings
partition of the MQTT firmware in this repository, so that both firmwares
can be flashed alternately on one board without wiping each other's data
([storage-and-logging.md](storage-and-logging.md#12-frdm-mcxn947)).

### 4.1 SPI NOR on the NUCLEO-F767ZI

Any 3.3 V `jedec,spi-nor` compatible part works; the overlay is written for a
Winbond W25Q128JV (JEDEC ID `ef 40 18`, 16 MiB, 4 KiB sectors). W25Q breakout
modules usually tie /WP and /HOLD high already.

| Flash pin | MCU | Nucleo connector | Arduino name |
|-----------|-----|------------------|--------------|
| CLK | PA5 (SPI1_SCK) | CN7-10 | D13 |
| DO (MISO) | PA6 (SPI1_MISO) | CN7-12 | D12 |
| DI (MOSI) | PB5 (SPI1_MOSI) | CN7-13 | (D22 on the Zio connector) |
| /CS | PD14 | CN7-16 | D10 |
| VCC | 3V3 | CN8-7 | |
| GND | GND | CN8-11 | |
| /WP, /HOLD | 3V3 | | |

The Arduino MOSI pin D11 (PA7) cannot be used: PA7 is the RMII CRS_DV
signal of the Ethernet PHY. SPI clock: 24 MHz (`spi-max-frequency`); keep
the wires short (< 15 cm) or lower the frequency. For a different part, change
`jedec-id` and `size` (in bits) of the `uc_nor` node and the partition size.

Block size: Zephyr's LittleFS uses the page size of the flash driver's page
layout as its block size. The `spi-nor` driver reports pages of
`CONFIG_SPI_NOR_FLASH_LAYOUT_PAGE_SIZE` bytes; Zephyr's default is 65536,
which would make every file that does not fit inline in a directory entry
occupy at least 64 KiB and every append after a sync erase a 64 KiB block.
The firmware Kconfig ([`firmware/Kconfig`](../firmware/Kconfig)) sets the
default to **4096**, the W25Q sector size, whenever the SPI NOR driver is
built: `/lfs` has 4096 blocks of 4 KiB (see
[storage-and-logging.md](storage-and-logging.md#2-littlefs-parameters)).

### 4.2 RAM file system (snippet `uc-ramfs`)

```sh
west build -b nucleo_f767zi BACNet-uc/firmware -S uc-ramfs
```

The snippet adds a 64 KiB RAM "flash" (1 KiB erase blocks) and mounts it at
`/lfs` instead of the board's entry; on the F767 it also disables the SPI NOR
node (`snippets/uc-ramfs/boards/nucleo_f767zi.overlay`), so the SPI NOR
driver is not built and no missing chip is probed. `/lfs` is formatted at every boot, so configuration, applications and
logs are lost on reset. The snippet reduces the log files to 2 × 4 KiB and
`CONFIG_UC_APP_MAX_FILE_SIZE` to 32 KiB. Intended for bring-up without the
flash module and for tests driven by the harness, which re-deploys after a
reset.

RAM: the 64 KiB RAM disk is paid for by a smaller kernel heap (64 → 48 KiB)
and `malloc` arena (64 → 32 KiB), set by the snippet's `boards/ram.conf` for
both boards. Both boards link with it: RAM 378 044 B (F767) and 376 456 B
(MCXN947) of 384 KiB, about 15 KiB of margin
([architecture.md](architecture.md#62-measured-usage)).

## 5. RS-485 add-on for BACnet MS/TP (Planned)

BACnet MS/TP is not implemented (see [bacnet.md](bacnet.md#9-datalinks-roadmap)).
The hardware plan reserves the Arduino UART of both boards:

| Signal | NUCLEO-F767ZI | FRDM-MCXN947 |
|--------|---------------|--------------|
| UART | USART6 (`arduino_serial`) | FLEXCOMM2 LPUART2 (`arduino_serial`) |
| TX → transceiver DI | PG14 (D1) | P4_2 (D1) |
| RX ← transceiver RO | PG9 (D0) | P4_3 (D0) |
| DE and /RE (tied) | PD15 (D9), GPIO | P0_24 (D11), GPIO |

Transceiver: a 3.3 V half-duplex RS-485 transceiver (e.g. TI THVD1450,
MAX3485, SP3485 class) with fail-safe biasing; 120 Ω termination at both ends
of the trunk; galvanic isolation (e.g. ADM2587E class) for installations
outside the lab. Baud rates 9600 to 115 200 bit/s. The DE line is driven
by the datalink in software; the F767's USART2 (PD5 TX, PD6 RX, PD4 RTS as
driver enable; pinctrl already in the board DTS, pins on the ST Zio
connector per ST UM1974) is an alternative with hardware driver-enable.

## 6. Flashing and debugging

| Task | NUCLEO-F767ZI | FRDM-MCXN947 |
|------|---------------|--------------|
| Host tool for `west flash` | STM32CubeProgrammer (default runner), or OpenOCD (`-r openocd`), or J-Link (`-r jlink`) | NXP LinkServer (default runner), or J-Link (`-r jlink`, after flashing J-Link firmware onto the MCU-Link or with an external probe), or pyOCD (`-r pyocd`) |
| USB connector | CN1 (ST-LINK, Micro-B): power, SWD and the virtual COM port | MCU-Link USB-C connector: power, SWD and VCOM (see the NXP board user manual for the connector designator) |
| Serial console | `/dev/ttyACM0` (Linux), 115200 8N1 | `/dev/ttyACM0`, 115200 8N1 |
| Debug | `west debug` (GDB via the runner) | `west debug` |
| Firmware update in the field | SMP image group with MCUboot (sysbuild, [storage-and-logging.md](storage-and-logging.md#1-flash-layouts)) | same |

## 7. Bill of materials (bench set-up for a two-node system)

| Qty | Item | Purpose |
|----:|------|---------|
| 1 | ST NUCLEO-F767ZI | node 1 |
| 1 | NXP FRDM-MCXN947 | node 2 |
| 1 | W25Q128JV (or W25Q64/W25Q32) SPI NOR breakout, 3.3 V | `/lfs` on the F767 |
| 6 | female-female jumper wires | SPI NOR wiring |
| 2 | USB cable: Micro-B (Nucleo CN1), USB-C (FRDM MCU-Link) | power, flashing, console |
| 1 | unmanaged Ethernet switch, 3+ Cat5e cables | BACnet/IP network (with the development host) |
| 1 | DHCP server on that network, or static addresses in `device.json` | IPv4 |
| 2 | 10 kΩ potentiometers | analog input stimuli (divide to ≤ 1.8 V on the MCXN947) |
| 2 | push buttons or DIP switches | digital inputs on D2/D4 (to GND, internal pull-ups) |
| 2 | LED with 330 Ω resistor, or a 3.3 V logic-level relay module | digital outputs D7/D8 |
| 1 | RC low-pass (e.g. 10 kΩ / 10 µF) per PWM output | 0..3.3 V analog from `ao` channels |
| 2 | 3.3 V RS-485 transceiver module + 120 Ω resistors (Planned MS/TP) | MS/TP trunk |
| 1 | development host (Linux x86-64) with Zephyr SDK, Python 3.12 | build, harness, `native_sim` nodes |

Actuators and field wiring beyond LEDs need proper driver stages
(transistor/relay drivers, 0-10 V converters); the MCU pins are logic level
only.
