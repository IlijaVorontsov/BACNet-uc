## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: already present in the
  supplied `alarm.h`; no header change needed. `alarm_init` copies it with the rest
  of the configuration.
- **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented. `alarm.c` now
  picks the delay for the pending transition in `pending_delay()`: a pending
  NORMAL transition (which can only arise from HIGH or LOW) uses
  `time_delay_normal_s`.
- **Other delayed transitions (NORMAL -> HIGH/LOW, HIGH <-> LOW) keep `time_delay_s`**:
  implemented. This is also what the BACnet OUT_OF_RANGE event algorithm specifies
  (pTimeDelay for offnormal and HIGH<->LOW transitions, pTimeDelayNormal only
  for transitions to NORMAL).
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. It corresponds to
  BACnet's "Time_Delay_Normal absent -> use Time_Delay".

No item conflicts with the documented requirements or with the BACnet standard, so
every item was implemented.

Other changes:
- `acceptance.vec`: six new blocks cover a longer and a shorter return-to-normal
  delay, `delay_normal=0`, HIGH<->LOW still using `time_delay_s`, the explicit
  sentinel, and FAULT -> NORMAL. The existing blocks leave `delay_normal` at its
  default (the sentinel), so their behavior is unchanged. All 9 blocks pass.

Open points:
- FAULT -> NORMAL (sensor fault clears) is still immediate and ignores both delays.
  The CR only covers HIGH/LOW -> NORMAL, and in BACnet the fault-clear transition
  is not governed by Time_Delay_Normal either. If the product owner meant
  "every transition to NORMAL", that needs a separate CR.
- The sentinel means 4294967295 s (about 136 years) cannot be configured as a real
  Time_Delay_Normal. BACnet allows any Unsigned value, so a BACnet front-end should
  reject that value on write, or map it to the sentinel knowingly.
- As before, the delay is chosen when the timer is checked, based on the pending
  target. If the pending target changes (e.g. HIGH, value first drops into the
  normal band and then below the low limit), the timer restarts and the new
  target's delay applies. That matches the existing restart-on-new-target behavior.

## CR-202

RAM budget: 512 points on a Cortex-M0 with 16 KB of RAM.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. The
  supplied `alarm.h` already had the new size. In `alarm.c`, `point_t` now holds a
  pointer to the configuration instead of a copy. The fields are ordered pointer
  first, then the 32-bit fields, then the byte fields, so there is no internal
  padding. `point_t` is 28 bytes on ILP32 (checked with clang for
  `thumbv6m-none-eabi`/Cortex-M0) and 32 bytes on the LP64 host test build.
  `_Static_assert`s check both the size and the alignment against `alarm_t`. I
  also removed the `has_value` flag: the code set it and never read it.
- **`alarm_init` keeps a pointer instead of copying; the caller keeps the
  `alarm_cfg_t` alive, and configurations may be shared**: implemented.
  `alarm_init` stores `cfg`, and every limit, deadband, delay and escalation lookup
  reads through that pointer. The configuration is only read, never written, so
  sharing it between points is safe. This replaces the CR-201 note that "`alarm_init`
  copies it with the rest of the configuration": `time_delay_normal_s` is now read
  through the pointer like every other field.
- **External behavior must not change**: holds when the configuration is not
  modified after `alarm_init`. `acceptance.vec` passes 9/9. I also ran 6000 random
  scenarios (samples, faults, NaN/inf, ticks, acks, maintenance, re-init, `now`
  wraparound and time going backwards) against the pre-change module, built with a
  larger `alarm_t`. The outputs were identical. `acceptance.vec` did not need
  changes.

No item conflicts with the documented requirements or with the BACnet standard, so
every item was implemented.

Open points / things I am unsure about:
- **Changing a configuration after init now has a visible effect.** Before, each
  point kept the values it had at `alarm_init`. Now a write to an `alarm_cfg_t`
  takes effect right away, on the next call, for *every* point that shares it. That
  includes any pending delay and escalation timer that is running. This follows
  from "keeps a pointer", so it is part of the requested change, but it is the one
  way the external behavior differs. The random scenarios above do show different
  output once the configuration is rewritten after init. Consequences:
  - A BACnet front-end that allows writes to High_Limit/Low_Limit/Deadband/
    Time_Delay/Time_Delay_Normal must not write into a configuration that other
    objects share. In BACnet these are per-object properties. The front-end needs
    its own `alarm_cfg_t` for that object, and the only way to attach a point to a
    different configuration is `alarm_init`, which resets the point's state. That
    was already the case before this CR.
  - Writes to a configuration are not atomic with respect to the alarm calls. If an
    ISR or another task updates a configuration while `alarm_sample`/`alarm_tick`
    runs, it can see some old and some new values. The caller has to serialize
    this.
  - Test infrastructure: `driver.c` passes its single static `cfg` to `alarm_init`.
    A `cfg` line issued after `init` therefore now reconfigures the running point.
    That includes a `cfg` line rejected with BADCMD, which has already zeroed the
    struct. All current vectors re-`init` after `cfg`, so none are affected.
- **The RAM arithmetic does not close.** 512 points x 32 bytes = 16384 bytes, which
  is all 16 KB of RAM. That leaves nothing for the shared configurations (24 bytes
  each), stack, notification buffers or the rest of the firmware (BACnet stack,
  I/O). On the M0 the state actually needs only 28 bytes. An `alarm_t` of 28 bytes
  with 4-byte alignment (e.g. `uint32_t storage[7]` on 32-bit targets) would save
  2 KB, but 14 KB for the points alone is still very tight. I left the supplied
  `alarm.h` as it is. Please confirm whether "16 KB" is the whole RAM or a budget
  just for the points.
