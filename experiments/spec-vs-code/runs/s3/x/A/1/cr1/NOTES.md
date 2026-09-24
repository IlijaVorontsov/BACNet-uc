## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Items:

1. **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: done. It was already in the
   supplied `alarm.h`, and I left that file unchanged. `alarm_init` copies the whole config, so
   the new field is stored per point. `point_t` still fits in `alarm_t`, and the
   `_Static_assert` still holds.
2. **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented. The new
   `delay_for()` in `alarm.c` picks the delay for the pending transition, and `eval_timer()`
   uses it.
3. **All other delayed transitions (NORMAL -> HIGH/LOW, HIGH <-> LOW) keep `time_delay_s`**:
   implemented.
4. **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented as the private constant
   `DELAY_NORMAL_SAME` in `alarm.c`. The driver's default is also 4294967295, so
   configurations that do not set `delay_normal` behave exactly as before.

No conflict with the BACnet standard. This matches the OUT_OF_RANGE event algorithm
(135, clause 13.3.6). pTimeDelayNormal governs HIGH_LIMIT/LOW_LIMIT -> NORMAL, pTimeDelay
governs the transitions into HIGH_LIMIT or LOW_LIMIT (including HIGH <-> LOW), and when
Time_Delay_Normal is absent Time_Delay is used. The sentinel stands for the "absent" case.

Tests: I added five blocks tagged `cr201` to `acceptance.vec`. They cover a shorter normal
delay, a longer normal delay (from LOW), HIGH <-> LOW still using `time_delay_s` when
`delay_normal=0`, FAULT -> NORMAL staying immediate, and the explicit sentinel value. All 8
blocks pass (`make && python3 run_vectors.py acceptance.vec`).

Open questions:

- FAULT -> NORMAL stays immediate and does not use either delay. It is not a HIGH/LOW ->
  NORMAL transition, and BACnet clears faults without a time delay. Please confirm this is
  what the product owner wants.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s (about 136 years) cannot
  be configured. I think this does not matter in practice.
- This behavior already existed and I did not change it. When the point is in HIGH and the
  value drops below `low_limit`, the module times only the transition to LOW. If the value
  later comes back into the normal band, the NORMAL timer starts at that moment. A strict
  reading of 13.3.6(e)/(h) says the "below high_limit - deadband" condition was already true
  the whole time. With a `time_delay_normal_s` shorter than `time_delay_s`, the standard
  could therefore allow a return to NORMAL earlier than this module does. Separate delays
  make this edge case easier to notice. If exact conformance is required, it should be
  handled in its own change request.
