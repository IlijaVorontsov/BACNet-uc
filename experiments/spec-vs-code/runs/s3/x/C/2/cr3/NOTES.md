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

## CR-203

Collected field requests.

- **Item 1 (product owner): `EV_ACKED` notification**: implemented. `alarm_ack` now emits
  ACKED (`time = now`, `value` = most recent sample) when it clears an unacknowledged alarm.
  It does nothing when there is nothing to acknowledge, or when the call is rejected because
  time went backwards. In maintenance mode the ack still takes effect, but ACKED is not
  emitted. It is also not reported later when maintenance ends, so `x056` is unchanged.
  This is the module's equivalent of a BACnet ACK_NOTIFICATION. `alarm.h` already had the
  value; I added a comment to it. Tests: `acceptance.vec` was already updated and now passes.
  In `tests/unit.vec` I added ACKED to the expected output of `x021`, `x033`, `x036`,
  `x039`, `x041`, `x043`, `x060` and `x063`. Each of these acknowledges an open alarm, and
  the ACKED note is the only change. I added `cr203a`..`cr203f` (tag `CR203`). They cover
  the value being the latest sample and not the alarm value, ACKED after an escalation, a
  second ack, ack in maintenance, a NaN value in FAULT, a rejected ack when time goes
  backwards, ack of an alarm that has returned to normal, and ack of an alarm first reported
  when maintenance ends. All 57 vectors pass, including under ASan/UBSan.
- **Item 2 (operators, Nord): return to normal clears unacknowledged status and cancels
  escalation**: NOT implemented, because it conflicts with BACnet and with a documented
  requirement.
  - BACnet: a return to NORMAL is a separate event transition. It does not acknowledge the
    earlier TO_OFFNORMAL transition. That transition's bit in Acked_Transitions stays
    cleared until someone acknowledges it (AcknowledgeAlarm). GetEventInformation keeps
    reporting an object whose Event_State is NORMAL for as long as a transition is
    unacknowledged. Clearing the status when the value returns to normal would lose alarms
    that no operator has seen.
  - Requirements: `x038` (tags A8 A9) requires an alarm that has returned to normal
    without being acknowledged to escalate. `x041` (A8 A9) requires it to still need an
    ack. Cancelling the escalation alone would not conflict with BACnet, because escalation
    is a product feature. It would still contradict A9 as `x038` specifies it, so it needs a
    requirement change by the product owner and is not a firmware fix.
  - What operators can do now: acknowledge the alarm. That cancels the escalation, and with
    item 1 it is reported as ACKED. `x038`/`x041` keep their behaviour. `x041` now also shows
    `ACKED@50:20.000`, because the alarm was still unacknowledged when it was acked.
- **Item 3 (operators, Nord): inclusive limits (`>= high_limit`, `<= low_limit`)**: NOT
  implemented, because it conflicts with BACnet and with a documented requirement.
  - BACnet OUT_OF_RANGE event algorithm (clause 13.3.6 in 135-2016 and later): NORMAL ->
    HIGH_LIMIT when the value is *greater than* pHighLimit. NORMAL -> LOW_LIMIT when it is
    *less than* pLowLimit. A value equal to the limit is in range. The code already does
    this.
  - Requirements: `x001` and `x002` (tag A2) require exactly 30.0 and exactly 10.0 to stay
    NORMAL with limits 30/10.
  - What operators can do now: set High_Limit to the highest value that is still acceptable,
    for example just below 30.0 at the sensor's resolution (and set Low_Limit the same way).
    This gets an alarm at 30.0 without changing the algorithm.

Things I am unsure about:
- There is no requirements document in this directory. I inferred what A2, A8 and A9 mean
  from the tagged vectors. The unit vectors `x001`/`x002` carry the tag `CR3incl` and
  `x038`/`x041` carry `CR3clear`. They link those vectors to items 3 and 2. I kept their
  expectations, except for the ACKED that item 1 adds to `x041`.
- Order inside `alarm_ack`: the timer is checked first. So an alarm that becomes active in
  the ack call itself is acknowledged in that same call, and the output is
  `TO_HIGH ... ACKED ...` (`x021`). This was already how the ack behaved;
  item 1 only makes it visible. Also unchanged: an ack arriving after the escalation time
  but before any tick acknowledges without escalating first (`x043`).
- "Most recent sample value" is taken literally. After a sensor-fault sample, it is the
  faulted reading (`x033`: `ACKED@20:0.000`), and after a NaN sample it is `nan`
  (`cr203c`). The front-end should not show that value as a valid reading.
- An ack made during maintenance is never reported. If the BMS needs to know that an
  ack happened during maintenance, it has to record that itself.
