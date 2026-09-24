# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). I found no conflict with BACnet or with the existing behaviour and tests, so every item below is implemented. There is no requirements document in this directory; the unit vectors cite requirement IDs A2–A13, but the document itself is missing, so I checked the change only against the existing vectors. All 44 original vectors still pass, and so do the 8 new ones (52 of 52 in total).

| Item | Status |
|------|--------|
| Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. The field was already in the supplied `alarm.h`, which I left unchanged. `alarm_init` copies it together with the rest of the configuration, and `point_t` still fits in `alarm_t` (the `_Static_assert` still holds). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented through `pending_delay()` in `alarm.c`, which `eval_timer` now calls. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | Implemented. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME` in `alarm.c`). This value is the driver's default, so existing configurations behave exactly as before. |
| `driver.c` `delay_normal=N` / updated `alarm.h` | I checked that both are present and did not modify either one. |

Tests: I added vectors `y001` to `y008` (tag `CR201`) to `tests/unit.vec`. They cover:
- a longer and a shorter normal delay
- HIGH↔LOW transitions still using `time_delay_s`
- the explicit `0xFFFFFFFF` value
- the timer restarting when the value leaves the band
- the return to normal during maintenance
- FAULT→NORMAL

I left `acceptance.vec` unchanged; its scenarios still pass.

BACnet consistency: this change matches Time_Delay_Normal semantics. The OUT_OF_RANGE algorithm uses pTimeDelayNormal for transitions to NORMAL and pTimeDelay for everything else. When Time_Delay_Normal is absent, Time_Delay is used, which is what the `0xFFFFFFFF` value represents here.

Points I am unsure about:
- **FAULT→NORMAL.** This stays immediate, with no `time_delay_normal_s` delay (vector `y007`). BACnet treats leaving FAULT as a reliability change, not as an event-algorithm transition, and existing vectors x029 and x030 already rely on it being immediate. I read the CR's "HIGH or LOW back to NORMAL" as not covering FAULT.
- **Value falling from above high to below low while in HIGH.** The code keeps its existing behaviour: LOW is the pending target and is timed with `time_delay_s`. This follows the CR's "HIGH↔LOW keep using time_delay_s". However, the BACnet algorithm also treats "below high−deadband for Time_Delay_Normal" as satisfied at the same time. So if `time_delay_normal_s` < `time_delay_s`, a strict reading of BACnet could give TO_NORMAL first, where this code goes straight to LOW. The pre-existing restart-on-target-change rule (A5a, x013) is related and is also unchanged. If product wants the strict BACnet behaviour, that needs its own CR.
- **The `0xFFFFFFFF` value.** Because it means "same as `time_delay_s`", a literal delay of 4294967295 s (about 136 years) cannot be configured. I consider this harmless.
