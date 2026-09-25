# apps/hil_stimulus: HIL stimulus firmware

Firmware for the rig's second NUCLEO-F767ZI, the **stimulus board** (docs/HIL.md §5). It drives
the DUT's digital inputs, reads its outputs with timestamps, sources and senses its analog
inputs, measures its PWM outputs, and owns the DUT's reset and power lines. The host drives it
over the ST-LINK VCP with **stimulus protocol v0**
([docs/hil/stimulus-protocol.md](../../docs/hil/stimulus-protocol.md)) through `hilrig.stim`.

- Zephyr v4.4.2, Zephyr SDK 1.0.1, picolibc, board `nucleo_f767zi`.
- Machine interface: shell group `stim` on USART3 (VCP), 115200 8N1, prompt `stim:~$ `.
  Every command answers with exactly one line, `OK k=v ...` or `ERR -<errno> <text>`.
- Boot banner: `STIM READY proto=0 fw=0.2.0 board=nucleo_f767zi rc=0x<reset cause> init=<rc>`.
- `stim info` reports `profile=P1` (from the channel map), so the host can check that the
  stimulus is wired for the bench's DUT profile (D22).

## Build and flash

```sh
# inside the west workspace, SDK 1.0.1 (ZEPHYR_SDK_INSTALL_DIR), Zephyr venv active
west build -b nucleo_f767zi $HIL/apps/hil_stimulus -d b/stim     # or: hil/host/build.sh stim
west flash -d b/stim -r openocd -- --cmd-pre-init "adapter serial $STIM_SN"
```

Measured: 92,124 B flash and 42,624 B RAM, 0 warnings (2026-09-25, SDK 1.0.1, v4.4.2).

Always select the probe by serial. The DUT and the stimulus are both ST-LINK V2-1
(0483:374b, platform `nucleo_f767zi`). `west flash --serial` does not choose the probe on this
board, because its openocd.cfg ignores `_ZEPHYR_BOARD_SERIAL` (D3). Keep the stimulus out of
the Twister hardware map. udev links it as `/dev/hil/stim0`, with `ID_MM_DEVICE_IGNORE=1` so
that ModemManager never writes into the shell.

A quick check after flashing: open `/dev/hil/stim0` at 115200, reset the board, and wait for
the banner, then send `stim info`, `stim chan di1` and `stim safe`.

## Files

| File | What |
|---|---|
| `prj.conf` | shell as a line protocol (512-byte command buffer, 24 arguments, RX ring 1024, TX ring 256, no colours, no history, echo on for the Ctrl-C resync), drivers, `CONFIG_INPUT=n` (frees EXTI13), picolibc |
| `boards/nucleo_f767zi.overlay` | the P1 channel map (`hil-stim` node, `profile = "P1"`), DAC1, ADC1/ADC3 senses with 480-cycle sampling and prescaler 4 (27 MHz), TIM5/TIM2/TIM4 capture, USART2 injector with hardware DE 8/8 (It2); disables `gpio_keys`, `mac`, `mdio`, `spi1`, `can1`, `usart6`, `timers1` |
| `dts/bindings/hil,stim-channels.yaml` | app-local binding: one child per channel with `kind`, `gpios`, `io-channels` (`src`/`sense`), `pwms`, `range-mv`, `scale`, `stim-pin`, `dut-pin` |
| `dts/bindings/vendor-prefixes.txt` | vendor prefix `hil` |
| `src/main.c` | banner, IWDG (4 s) feeding, heartbeat LED; stops feeding when a command overruns its budget + 5 s (the budget is the command's worst case: a sleeping pulse train includes up to 4 ticks per pulse, RS-485 tx/rx the frame's time on the wire at the current rate) |
| `src/stim_cmds.c` | the `stim` commands; the channel table is built with `DT_FOREACH_CHILD_STATUS_OKAY_SEP` |

## Channel map (profile P1)

Channel names are the **DUT catalog names** (D22). Levels on DUT channels are physical wire
levels; the host applies the catalog polarity. Every DUT line has 1 kΩ in series at the
stimulus end. Full wiring: [docs/hil/pin-tables.md](../../docs/hil/pin-tables.md) §2.

| Channel | Kind | Stimulus pin | DUT pin |
|---|---|---|---|
| di0, di1, di2 | di: out 0/1/z | PE14 CN10-28, PF15 CN10-12, PF14 CN10-8 | PC13, PF15, PF14 |
| do0 … do4 | do: in, pull-down, EXTI | PE7, PE8, PE10, PF13, PF12 | PB0, PB7, PB14, PF13, PF12 |
| ai0, ai3 | ai: DAC1 OUT1/OUT2 source + ADC sense | src PA4/PA5, sense PA3/PF3 | PA3, PF3 |
| ai1, ai2, ai4, ai5 | ai: ADC sense of the It1 dividers | PC0, PC3, PF5, PF10 | same pins |
| ao0, ao1, ao2 | ao: TIM5/TIM2/TIM4 capture (TIM AF + pull-down for the whole run) | PA0 CN10-29, PA15 CN7-9, PD12 CN10-21 | PE9, PE11, PE13 |
| nrst | FET gate, 1 = DUT NRST low | PE4 CN9-16 | NRST CN8-5 |
| nrst_sense | input, no pull, 1 = DUT in reset (`rstmon`, EXTI15) | PE15 CN10-30 | NRST via 10 kΩ |
| pwr | inverter gate, logical 1 = DUT powered | PE5 CN9-18 | load switch ON → E5V |
| sync | out to the LA | PE6 CN9-20 | LA `sync` |
| lb | marker loopback (jumper from SYNC) | PB11 CN10-34 | – |
| m0 … m3 | marker in, pull-down, EXTI 0-3 | PG0, PG1, PG2, PG3 | PG0 … PG3 (snippet `hil`) |
| v3v3, v5 | ADC sense ×2 (100k/100k) | PB1 CN10-7, PF4 CN10-11 | DUT 3V3 CN8-7, E5V |

Capabilities announced in `caps=`: `pwmcap`, `rs485` (baud, clear, load, tx, rx; polled) and
`rstmon`. The It2 instruments (MCP4728, ADS1115, INA226) and the P2 profile overlay are not
part of this tree yet (§5.2).

## Safety invariants (protocol §6)

1. **Fail-safe without firmware.** While the stimulus is in reset, unpowered or hung (IWDG
   4 s plus the command-deadline watchdog), every DUT-facing line is Hi-Z (exception: PA15 =
   JTDI keeps its internal pull-up, which biases ao1 weakly), the NRST FET is off (NRST
   released), the power FET is off (**the DUT stays powered**), the RS-485 DE pull-down keeps
   the driver off, and the DAC is disabled.
2. The host sends `stim safe` at session start, around every test and at session end.
   `safe` releases every di line, disables the DAC outputs (EN=0), releases NRST, sets SYNC low,
   disarms the edge engine and powers the DUT. It never touches the ao pins.
3. `power off` and `power cycle` run lines-safe first. While the DUT is unpowered, analog
   sources refuse any non-zero value (`ERR -1`).
4. The stimulus never drives a do, marker, nrst_sense or ao line (`ERR -1`). The ao pins stay
   in TIM AF mode and are never armed on EXTI (they share lines 0/15/12 with m0, nrst_sense
   and do4), so `edges` and `lat` refuse them; read them with `din` or `pwmcap`.
5. NRST is driven only open-drain through the FET; there is no pull on the sense input.
6. IRQ-locked sections (`pulse` trains of at most 50 ms, `rs485 tx`) poll the cycle counter
   continuously and never block (the SysTick rule of protocol §6 item 7).

## Changes against the scratchpad port

The port (with the v2.1 ao fix) is committed as it was, plus four protocol-conformance fixes:
`stim info` now reports `profile=` (binding property `profile`, required); `stim rstmon` with
an argument other than `clear` answers `ERR -22 usage`; `stim lat` takes `t0` with IRQs
locked right after driving the output, so a fast input edge (the `lb` loopback) can no longer
be timestamped before `t0` (an unsigned underflow gave a huge `lat_ns`); and
`stim rs485 <unknown>` answers `ERR -134 unknown command` instead of the shell's help text.
