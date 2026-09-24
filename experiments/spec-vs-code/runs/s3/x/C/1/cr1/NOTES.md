## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Conflict check: I found no conflict with the BACnet standard. In the OUT_OF_RANGE
event algorithm (ASHRAE 135, clause 13.3.6), HIGH_LIMIT/LOW_LIMIT -> NORMAL waits
pTimeDelayNormal. NORMAL -> HIGH_LIMIT/LOW_LIMIT and HIGH_LIMIT <-> LOW_LIMIT wait
pTimeDelay. When Time_Delay_Normal is absent, Time_Delay is used. The CR asks for
the same thing. This directory has no written requirements document other than the
comments in `alarm.h` and the requirement tags in the vectors, and nothing there
conflicts with the CR.

Items:

1. Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`: **done**. It was already in
   the supplied `alarm.h`. I did not change the header. `point_t` in `alarm.c` still
   fits in `alarm_t`; the `_Static_assert` still passes.
2. HIGH/LOW -> NORMAL uses `time_delay_normal_s`: **implemented**. The new
   `pending_delay()` in `alarm.c` picks the delay from the pending target.
   `eval_timer()` uses it for both `alarm_sample()` and `alarm_tick()`/`ack`/`maint`.
3. NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s`:
   **implemented**. A pending return to NORMAL that is replaced by a pending
   HIGH/LOW restarts the timer with `time_delay_s`, and the reverse case restarts
   it with the normal delay. This matches the existing restart-on-new-target rule.
4. `0xFFFFFFFF` means "same as `time_delay_s`": **implemented**
   (`TIME_DELAY_NORMAL_UNSET`).

Tests: I added `tests/unit.vec` x064-x074 (tag `CR201`) and the acceptance
scenario `high-alarm-returns-after-normal-delay`. I did not modify
`driver.c` or `run_vectors.py`. All 56 blocks pass. 9 of the new
blocks fail against the old delay logic. x069, x071 and x073 are regression guards
that pass either way.

Unsure / worth reviewing:
- FAULT -> NORMAL stays immediate. `time_delay_normal_s` does not apply to it. The
  CR names only HIGH/LOW -> NORMAL, and in BACnet a fault clear is not delayed by
  Time_Delay_Normal. x073 pins this.
- Zero-initialised configs: an integrator who builds `alarm_cfg_t` with `= {0}`,
  `memset` or designated initializers and does not set the new field gets
  `time_delay_normal_s = 0`. That makes the return to normal immediate, not
  "same as `time_delay_s`". This is a silent behaviour change for existing callers,
  and they should set the field to `0xFFFFFFFF` explicitly. The test driver already
  does this.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s cannot be
  configured. The largest usable value is 4294967294 (x072).
- The existing rule is unchanged: the config is copied at `alarm_init`, so a
  change to either delay needs a re-init.
- The requirement tags (A3, A4, A5, A5a, A6, A10) on the new vectors are my best
  guess from the existing vectors. No requirements list was available to confirm
  them.
