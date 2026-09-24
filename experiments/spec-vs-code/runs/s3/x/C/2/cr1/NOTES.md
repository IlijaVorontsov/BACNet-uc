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
