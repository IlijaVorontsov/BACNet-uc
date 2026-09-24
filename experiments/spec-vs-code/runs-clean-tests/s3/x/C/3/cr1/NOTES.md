# Notes

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Implemented. The field was already in `alarm.h`; `alarm.c` copies it with the rest of the config in `alarm_init` (`point_t` still fits in `alarm_t`, checked by the `_Static_assert`). |
| HIGH -> NORMAL and LOW -> NORMAL use `time_delay_normal_s` | Implemented (`pending_delay()` in `alarm.c`). This matches BACnet OUT_OF_RANGE transitions (c)/(d), which use pTimeDelayNormal. |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s` | Implemented. This matches BACnet transitions (a)/(b)/(e)/(f), which use pTimeDelay. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented. This is how the C API says the BACnet property is absent; the standard then uses Time_Delay. |

No item conflicts with the BACnet standard, so every item was implemented.

Tests:
- `tests/unit.vec` has new blocks x064 to x071 (tag `CR201`). They cover:
  - a normal delay shorter than, longer than, and equal to (explicit sentinel) `time_delay_s`
  - `delay_normal=0`
  - HIGH <-> LOW still using `time_delay_s`
  - the pending target switching from NORMAL to LOW
  - maintenance mode
  - FAULT -> NORMAL
- `acceptance.vec` has a new scenario, `high-alarm-returns-after-normal-delay`.
- All existing vectors are unchanged and still pass, because the driver's default is `delay_normal=4294967295`, which gives the same behaviour as before. 53/53 blocks pass.

Open questions:
- FAULT -> NORMAL (the sensor recovering) is still immediate and does not wait for `time_delay_normal_s`. It was never a delayed transition, and BACnet fault transitions have no time delay. My reading of "all other delayed transitions" is that the CR does not cover it.
- Existing behaviour, not changed: in HIGH, if the value moves between the normal band and below `low_limit`, the pending timer restarts each time, and the delay then used is `time_delay_normal_s` or `time_delay_s` depending on the new target. Read literally, BACnet condition (c) ("below high_limit - deadband for pTimeDelayNormal") would keep one timer running across both regions. The same applies in the other direction from LOW. The CR did not ask for a change here, so I left it alone.
- Because of the sentinel, a delay of exactly 4294967295 s (about 136 years) cannot be set for the return to normal. It always means "same as `time_delay_s`". I assume this is acceptable.
- The requirement IDs used as vector tags (A2 to A13) are not documented in this directory. I could only check the CR against the BACnet standard and the existing tests, not against the product requirements text.
