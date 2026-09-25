# Stimulus protocol v0 (with the It2 capability set)

Implemented by `apps/hil_stimulus` (Zephyr 4.4.2, NUCLEO-F767ZI) and by `hilrig.stim`. The protocol is board-agnostic: channel names come from the stimulus devicetree (`hil,stim-channels`). The version stays **0** for every addition in this document. New features are announced in `caps=`.

## 1. Transport

- Stimulus USART3 through its ST-LINK V2-1 VCP, 115200 8N1. `bench.yml stim.baud` may raise the rate after R-01 has measured it; 1 000 000 and 2 000 000 are exact divisors on both UARTs [unverified]. Raising it needs `&usart3 { current-speed = <…>; }`.
- udev link `/dev/hil/stim0`, keyed on the ST-LINK serial. The same rule sets `ID_MM_DEVICE_IGNORE=1`, so ModemManager never probes the port.
- The host opens the port with `exclusive=True`; no other process (pre-flash script, second pytest) may write to it.
- Prompt `stim:~$ `. VT100 colours off, echo on, history off.
- Lines are ASCII, at most 511 characters. One command outstanding at a time.
- Shell buffers (set explicitly): `SHELL_CMD_BUFF_SIZE=512`, `SHELL_ARGC_MAX=24`, RX ring 1024, TX ring 256.
- **Resync:** the host sends 0x03 (Ctrl-C clears a partial line; this needs echo on) and then CR, and waits for a quiet prompt.
- **Retries:** read-only commands (`info`, `chan`, `din`, `edges`, `adc`, `rstmon` without `clear`) are retried once after a resync. State-changing commands are never retried.

## 2. Framing

- **Banner** after every stimulus reset: `STIM READY proto=0 fw=<semver> board=<board> rc=0x<hwinfo reset cause> init=<rc>`. It is printed with `shell_print` after `shell_ready()`. A banner seen in the middle of a session raises `StimRebooted`, and the test fails as a rig fault (except in tests that power the stimulus off on purpose, such as PWR-02, which reopen the link).
- **Reply:** exactly one line per command.
  - Success: `OK[ k=v]…`. Values contain no spaces; lists are comma-separated.
  - Failure: `ERR -<errno> <text>`.
- The host ignores echo, log lines (the stimulus logs at WRN and above), blank lines and ANSI sequences. It must ignore unknown keys.
- **Units:**
  - times are `_ns` since stimulus boot, on the 64-bit SysTick cycle counter (216 MHz, 4.63 ns quantum; the crystal ppm is unknown, so fit it with `sync`);
  - voltages are `mv`, duty is `_ppm`.
- **Levels:**
  - On DUT channels (`di*`, `do*`, `ai*`, `ao*`), levels are **physical wire levels**, because the stimulus DT flags are ACTIVE_HIGH. hilrig applies the DUT catalog polarity (`GPIO_ACTIVE_LOW`).
  - Rig channels have fixed meanings: `nrst` 1 = NRST asserted; `nrst_sense` 1 = DUT in reset; `pwr` logical 1 = DUT powered, and it is only driven through `stim power`.

## 3. Error codes (Zephyr/newlib numbering; a BUILD_ASSERT checks them under picolibc)

| Code | Name | Meaning |
|---|---|---|
| -1 | EPERM | wrong channel kind for the command (including `edges`/`lat` on an ao channel), or an analog source > 0 mV while the DUT is unpowered |
| -2 | ENOENT | unknown channel |
| -5 | EIO | driver error |
| -11 | EAGAIN | no signal |
| -16 | EBUSY | edge engine or capture busy |
| -19 | ENODEV | hardware absent (the host turns this into a pytest skip) |
| -22 | EINVAL | usage, number format or range |
| -34 | ERANGE | value outside the usable range, or timer overflow |
| -71 | EPROTO | host side only: reply garbled |
| -116 | ETIMEDOUT | timeout |
| -120 | EALREADY | input already at the target level |
| -122 | EMSGSIZE | RS-485 slot overflow |
| -134 | ENOTSUP | unknown command, or the channel lacks that function |

A wrong argument count returns `ERR -22 usage: …`. Handlers use `SHELL_OPT_ARG_CHECK_SKIP`, so the shell's own "wrong parameter count" text never appears. An unknown subcommand returns `ERR -134 unknown command`.

## 4. v0 commands

| Command | Arguments and ranges | Reply | Errors | Timing and accuracy |
|---|---|---|---|---|
| `stim info` | – | `OK proto=0 fw= board= profile= uptime_ms= vdda_mv= pwr=0\|1 rst_n= caps=<list> chans=<list>` | – | The host checks proto == 0, profile == bench profile, and that chans contains bench.yml `stim.chans`. |
| `stim chan <name>` | name | `OK name= kind= src=0\|1 sense=0\|1 cap=0\|1 pin=<stim pin> dut=<dut pin>` | -2 | – |
| `stim safe` | – | `OK pwr=1` | -5 | Every di line Hi-Z; DAC channels disabled (EN=0, Hi-Z); MCP4728s at 0 V; NRST released; SYNC low; edge engine disarmed; **DUT power on**. ao pins are not touched (they stay in TIM AF mode with the pinctrl pull-down). Idempotent, < 1 ms. |
| `stim dout <chan> <0\|1\|z>` | kind di (0/1/z) or sync (0/1) | `OK t_ns=` | -2, -1, -22 | The edge happens within about 0.5 µs before `t_ns`. |
| `stim din <chan>` | any channel with gpios, including ao (AF pins are read through IDR without reconfiguring them) | `OK v=0\|1` | -2, -5 | instantaneous sample |
| `stim pulse <chan> <width_us> [count] [period_us] [active]` | width 1..10 000 000 µs; count 1..100 000; period > width (default 2 × width), ≤ 20 s; total ≤ 600 s. `active` (0/1) is required when idle is z; idle = the current state. | `OK n= t0_ns= mode=busy\|sleep` | -1, -22, -34 | Total ≤ 50 ms: IRQ-locked busy wait (`k_busy_wait` polls the cycle counter continuously), edges ±1 µs (estimate). Otherwise `k_usleep` on a 10 kHz tick: +0..200 µs per phase. |
| `stim edges <chan> <window_ms> [max]` | input kinds do, marker (m0-m3, lb) and nrst_sense; window 1..60 000 ms; max 1..1024 (default 64). **ao is refused (ERR -1)**: read it with `din` or `pwmcap`. | `OK n=<total> t0_ns= trunc=0\|1 t_ns=<list> lv=<list>` | -1, -2, -16, -22 | Each timestamp lags the true edge by the EXTI ISR latency (about 1-3 µs [unverified]; calibrated by R-09). Edges closer than about 5 µs may merge. |
| `stim lat <out> <0\|1\|z> <in> <0\|1> <timeout_ms>` | out: di or sync; in: do, marker (incl. lb) or nrst_sense (not ao); timeout 1..60 000 | `OK lat_ns= t0_ns=` | -116, -120, -16, -1 | 4.63 ns resolution. The bias is the ISR latency, which is subtracted using `lat sync 1 lb 1 10`. |
| `stim dac <chan> <mV\|z>` | ai channel with a src. The on-chip DAC range is 200..3100 mV (buffered: 0.2 V to VDDA − 0.2 V). `z` sets EN=0. | `OK mv= code= vdda_mv=` or `OK mv=z` | -134 (fixed divider), -34, -1 (DUT off), -19 | The code is scaled by VDDA measured from VREFINT. The reply comes after the node settles (1 ms = 10τ). Absolute accuracy ±(10 mV + 0.5 %). Tests compare the DUT against `adc` of the same node, not against `mv`. |
| `stim adc <chan> [n]` | ai or sense channel (including v3v3, v5, and in It2 rs485_a/rs485_b); n 1..1024 (default 16) | `OK mv= raw= n= vdda_mv=` (`mv` includes the `scale`) | -134, -22, -5 | about 30-40 µs per sample (480 cycles at 27 MHz); ±(5 mV + 0.5 %) (estimate). Differences of two channels on the same ADC cancel most of the gain error. |
| `stim reset [hold_ms]` | 1..10 000 (default 10) | `OK held_ms= t_ns=<release>` | -22, -19 | Hold time +0..0.2 ms. The DUT leaves reset 2.3-3.9 ms after `t_ns` (the NRST RC). |
| `stim power <on\|off\|cycle> [off_ms]` | off_ms 10..60 000 (default 2000) | `OK pwr=0\|1 t_ns=` | -22, -5 | `off` and `cycle` run lines-safe first. `off` persists until `on`, `safe` or a stimulus reset. The rail decays over milliseconds; tests check with `adc v3v3`. With the It2 TPS22918 the rail rises about 2.5 ms after `on` (tON ≈ 2 ms). |
| `stim rstmon [clear]` | – | `OK n= last_ns= in_reset=0\|1` | -19 | Counts every DUT reset (ISR on `nrst_sense`, EXTI15, armed permanently) since boot or the last clear. |

## 5. It2 capabilities (announced in `caps=`; proto stays 0)

| Capability | Commands | Reply | Guarantee | State |
|---|---|---|---|---|
| `pwmcap` | `stim pwmcap <ao> [timeout_ms 5..60000, default 200]` | `OK period_ns= pulse_ns= duty_ppm= static=0`, or `period_ns=0 pulse_ns=0 duty_ppm=0\|1000000 static=1 lv=` | TIM2/TIM5: ±2 ticks ≈ ±20 ns + crystal ppm. TIM4: ±200 ns, ≤ 6.67 ms (-34 beyond). The first 2 captures are skipped. The ao pins keep their TIM AF configuration for the whole run (`safe` never touches them), so a capture after `safe` is valid. | implemented (ao fix in v2.1) |
| `rs485` | `stim rs485 baud <1200..1000000> [skew_ppm ±50000]` | `OK baud=` | Keeps DEM (flow_ctrl RS485). BRR resolution about 0.07 % at 38400. | implemented |
| | `stim rs485 clear <slot 0..3>`, `stim rs485 load <slot> <off 0..2047> <hex ≤ 200 B>` | `OK slot= len= crc32=` | The host compares len and zlib.crc32. A 511-octet frame takes 3 lines. | implemented |
| | `stim rs485 tx <@slot\|hex ≤ 200 B> [gap=<idx>:<us>]×4 [rep=1..10000] [per_ms=]` | `OK n= rep= t0_ns= t_end_ns=` | Gaps are measured from TC. Inter-octet jitter comes only from ISR preemption. | implemented (polled) |
| | `stim rs485 rx <timeout_ms> [idle_us]` | `OK n= t_first_ns= t_last_ns= hex=` (It2 adds `fe=`, `ore=`) | – | polled today; interrupt ring in It2 |
| | `stim rs485 break <us>`, `fe <hex>`, `jam <ms> <mark\|space\|random>`, `de <0\|1\|auto>` | `OK` | GPIO takeover of PD5/PD4, restored with `pinctrl_apply_state` | to build |
| `ref` | `stim ref <chan> [n]` | `OK uv= n= fs_mv=` | ADS1115 via io-channel `ref`; 10 kΩ series on every input tied to a DUT net, gain error calibrated per bench | to build |
| `ina` | `stim ina [n]` | `OK mv= ma= mw=` | INA226 via the sensor API | to build |
| `sync` | `stim sync <n 1..1000> [period_ms 1..1000, default 50]` | `OK n= t_ns=<list>` | about ±50 ns per toggle timestamp | to build |

The MCP4728s have no command of their own. `stim dac ai1 <mV>` works as soon as ai1's `src` io-channel is `<&mcp4728_1 0>`. MCP4728 #1 sits on I2C1 (PB8/PB9) and #2 on I2C2 (PF1 SCL, PF0 SDA), both at 0x60. The driver's power-down mode is static, so `safe` sets code 0.

## 6. Safety invariants

1. **Hardware fail-safe without firmware.** While the stimulus is in reset, unpowered or hung (IWDG 4 s plus the command-deadline watchdog):
   - all DUT-facing lines are Hi-Z (F7 pins are floating inputs during and just after reset). **Exception:** the JTAG pins keep their debug AF with internal pulls [likely: RM0410 not reachable]; the only one in use is PA15 (JTDI, ao1), which then pulls DUT PE11 weakly high (about 40 kΩ + 1 kΩ). No test reads ao1 during a stimulus reset. PB4 (NJTRST) is not used;
   - the NRST FET is off (100 k gate pull-down), so NRST is released;
   - the power FET is off, so the switch ON pin is pulled up and **the DUT stays powered**;
   - the RS-485 DE pull-down keeps the driver off;
   - the DAC is disabled.
2. The host calls `stim safe` at session start, before and after every test (autouse fixture) and at session end.
3. `power off` and `cycle` run lines-safe first. While the DUT is unpowered, analog sources refuse non-zero values (ERR -1). Digital outputs stay allowed, because DUT FT pins accept VDD + 4 V and positive injection is not possible.
4. A 1 kΩ resistor (P1) or 2.2 kΩ (the It2 shield) at the stimulus end of every DUT line limits contention and injection to ≤ 3.3 mA or ≤ 1.5 mA.
5. No stimulus pin ever sees more than 3.3 V. The 5 V net exists only on the switch side and on the FET drains. U2 and U3 run from the stimulus 3V3, and the RS-485 taps are divided by 2.
6. NRST is driven open-drain only, through the FET. There is no external pull-up, and the sense input has no pull.
7. **Cycle counter rule.** In tickless mode the SysTick LOAD follows the next kernel timeout, and each counter read can detect only one wrap (COUNTFLAG, cortex_m_systick.c). The 77.7 ms figure is only the maximum reload. So inside an IRQ-locked section the counter must be read at least once per the minimum possible LOAD: the port's busy waits (`pulse` ≤ 50 ms, `rs485 tx` TC polls) poll the counter continuously through `k_busy_wait` and `k_cycle_get_64`, and nothing ever blocks with IRQs locked.
8. The stimulus never drives a do, marker, nrst_sense or ao line (ERR -1).
9. The stimulus never reconfigures an ao pin: pinctrl sets TIM AF and the pull-down once at pwm init, and neither `safe` nor `edges`/`lat` touch it. The ao pins share EXTI lines 0, 15 and 12 with m0, nrst_sense and do4, so they are never armed.

## 7. Host driver contract (`hilrig.stim`)

- `Stim(port, baud=115200, timeout=…)` provides `info() chan() safe() dout() din() pulse() edges() lat() dac() adc() reset() power() rstmon()`, returning typed values.
- Exceptions: `StimError(errno, text)` with the subclasses `StimTimeout`, `StimNoDevice`, `StimNotSupported` and `StimBusy`; `StimLinkError` with the subclass `StimRebooted`.
- The per-command timeout is the command's own duration plus 2 s.
- Traffic is logged with `time.monotonic()` to `artifacts/stim.log`.
- `rs485_tx()` sends up to 200 B inline and uses the chunked upload beyond that, with a crc32 check.
- MS/TP frames always come from `hilrig.mstp.frame()`, never from hand-written hex.

## 8. Example

```
stim:~$ stim info
OK proto=0 fw=0.2.0 board=nucleo_f767zi profile=P1 uptime_ms=5123 vdda_mv=3301 pwr=1 rst_n=0 caps=rstmon chans=di0,di1,di2,do0,...,v5
stim:~$ stim dout di1 0
OK t_ns=5123456789
stim:~$ stim lat di1 z do3 1 500
OK lat_ns=41234000 t0_ns=5200000123
stim:~$ stim edges ao0 100
ERR -1 not an input channel
stim:~$ stim dac ai0 1650
OK mv=1650 code=2047 vdda_mv=3301
stim:~$ stim adc ai0 64
OK mv=1648 raw=2045 n=64 vdda_mv=3301
stim:~$ stim power cycle 2000
OK pwr=1 t_ns=7300000000
stim:~$ stim frob
ERR -134 unknown command
```

## 9. Round-trip estimates at 115200 (R-01 measures them)

| Command | Estimate |
|---|---|
| `din` | 4.7 ms |
| `dout` | 6.2 ms |
| `adc 16` | 8.3 ms |
| `dac` | 8.7 ms |
| `info` (28 channels) | 24 ms |
| `edges` with 64 edges | 192 ms |
| `rs485 load` of 200 B | 42 ms |

Short commands are dominated by USB latency. It1 stays at 115200. R-01 times `din`, because `info` alone would exceed its 20 ms p99 budget.
