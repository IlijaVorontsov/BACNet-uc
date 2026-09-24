## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: implemented. The field was
  already in `alarm.h` as delivered; `alarm_init` copies it with the rest of the
  configuration. `point_t` still fits in `alarm_t` (checked by the existing `_Static_assert`).
- **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented in `alarm.c`
  (`pending_delay()`, used by `eval_timer()`). This matches the BACnet OUT_OF_RANGE event
  algorithm, where HIGH_LIMIT->NORMAL and LOW_LIMIT->NORMAL use pTimeDelayNormal.
- **NORMAL->HIGH/LOW and HIGH<->LOW keep using `time_delay_s`**: implemented. This also
  matches the BACnet algorithm (pTimeDelay for those transitions). If a pending return to
  NORMAL is replaced by a pending HIGH/LOW (or the other way round), the timer restarts
  and the new transition's delay applies (as it did before this change).
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. The fallback is resolved
  when the timer is checked, so the stored configuration is an unmodified copy. This
  matches BACnet's rule that Time_Delay applies to transitions to NORMAL when
  Time_Delay_Normal is absent. `0` is a real value that means an immediate return to NORMAL.
- Unchanged: FAULT -> NORMAL (sensor fault cleared) still happens at once and does not use
  either delay, because BACnet fault handling has no time delay. Maintenance mode still
  runs the timers silently and reports the resulting state when maintenance ends.
- Tests: added unit vectors `cr201a`..`cr201g` (tag `CR201`) to `tests/unit.vec`. They cover
  a shorter and a longer normal delay, `delay_normal=0`, the explicit sentinel, HIGH<->LOW
  and NORMAL->HIGH still using `time_delay_s`, a pending return to NORMAL replaced by a
  LOW, FAULT->NORMAL not being delayed, and the delay running during maintenance.
  `acceptance.vec` is unchanged. All 51 vectors pass.

Open questions:
- I found no conflict with the BACnet standard, and no requirements document in this
  directory to check against. The `A*` tags in the vectors refer to requirements I could
  not see.
- In BACnet, Time_Delay_Normal is an Unsigned, so 4294967295 s is a legal value there.
  Here it is taken to mean "not configured". A BMS front-end that maps a BACnet write of
  exactly 4294967295 would get `time_delay_s` behaviour instead of a delay of about 136
  years. The front-end should use the sentinel only when the property is absent.

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by pointer.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. `alarm.h` was
  already updated. In `alarm.c`, `point_t` now holds a `const alarm_cfg_t *` in place of the
  24-byte copy, and its fields are ordered by alignment so there is no internal padding. It is
  32 bytes on the 64-bit host build and 28 bytes on the Cortex-M0. Two `_Static_assert`s check
  it at build time: one for size (the existing check) and a new one for alignment. I also
  cross-compiled `alarm.c` for `thumbv6m-none-eabi` (clang, `-mcpu=cortex-m0`) to check the
  asserts on the target ABI.
- **`alarm_init` keeps a pointer and does not copy the configuration**: implemented. Every
  function reads the limits, delays and escalation time through the pointer.
  I added a note to the `alarm.h` comment: a change to a shared configuration applies to every
  point that uses it, from that point's next call. The point's state is not reset.
  The CR-201 notes above say that `alarm_init` copies the configuration and that "the stored
  configuration is an unmodified copy". CR-202 replaces those statements. The
  `time_delay_normal_s` sentinel is still resolved when the timer is checked, and it now has to
  be, because the configuration is `const` and shared.
- **External behavior must not change**: done and checked. All 51 vectors pass
  (`acceptance.vec`, `tests/unit.vec`). I also compared the new driver's output with the
  pre-change code, built with a larger `alarm_t`, on about 600,000 random commands (random
  configurations including the `delay_normal` sentinel, NaN/inf, faults, maintenance, ack,
  time going backwards and 32-bit time wrap). The output was identical. The new code runs
  clean under ASan/UBSan. I added no vectors, because the driver cannot tell the two versions
  apart for a caller that follows the contract.

Open questions / things I am unsure about:
- **The RAM budget does not add up.** 512 points x 32 B = 16,384 B, which is all of the 16 KB
  of RAM. That leaves nothing for the stack, the shared `alarm_cfg_t`s (24 B each), the note
  buffers or the rest of the firmware. On the M0 a point needs only 28 B. The last 4 B of every
  `alarm_t` are padding caused by `uint64_t` storage. Packing the flags into bits would bring a
  point down to 22-24 B on the M0. A 16-bit configuration index instead of a pointer would save
  more. However, a 24-byte `alarm_t` cannot hold an 8-byte pointer on the 64-bit host build. I
  did not change the size set in `alarm.h`. The firmware lead needs to decide this.
- **The API contract changed for callers.** A caller that passes a stack or temporary
  `alarm_cfg_t` now leaves a dangling pointer. So does a caller that reuses one buffer to
  initialise several points with different settings. The only caller in this directory is
  `driver.c`, which uses a static `cfg`, so it is safe. Note that the driver's `cfg` command
  rewrites that struct in place. A scenario that sent `cfg` without a following `init` would
  now change the live point's configuration, where before it had no effect until `init`. All
  existing vectors send `init` after `cfg`. Other callers in the firmware must be audited.
- **BACnet: each object has its own limit properties.** High_Limit, Low_Limit, Deadband,
  Time_Delay and Time_Delay_Normal belong to each object. If a client writes one of them on an
  object whose configuration is shared, the BMS front-end must not modify the shared struct in
  place, because that would change every object that shares it. The front-end should switch
  that object to its own configuration (copy-on-write, from a static pool because there is no
  heap). This does not conflict with the CR, because the module does not decide how
  configurations are shared. Also note that before this change, a new configuration always
  meant calling `alarm_init`, which resets state. Now an in-place edit takes effect without a
  reset, and a timer that is already running uses the new delay.
- **Concurrency:** the fields of the configuration are read one by one. If another context
  (an ISR or another task) edits a shared configuration while a point is evaluated, the point
  may see a mix of old and new values. The header now says not to do this.
- I found no conflict with the BACnet standard or with a documented requirement. As for
  CR-201, there is no requirements document in this directory. The `(point_t *)` cast of
  `alarm_t` (strict aliasing) is unchanged from before.
