# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Conflict check: none found. The change matches ASHRAE 135 OUT_OF_RANGE (13.3.6):
HIGH_LIMIT/LOW_LIMIT to NORMAL is timed by pTimeDelayNormal, while NORMAL to an offnormal
state and HIGH_LIMIT to/from LOW_LIMIT are timed by pTimeDelay. When Time_Delay_Normal
is absent, Time_Delay is used, and the `0xFFFFFFFF` sentinel stands for that. The CR
does not change any [POL] rule; A5a only now points to the per-transition delay. The
product owner raised the CR.

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Already in the supplied `alarm.h`. `alarm_init` copies it with the rest of `cfg`. `point_t` is still well within `sizeof(alarm_t)` (static assert passes). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented. `pending_delay()` in `alarm.c` is used by `eval_timer()`. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep `time_delay_s` | Implemented, and covered by tests (x066, x067). |
| `0xFFFFFFFF` = "same as `time_delay_s`" | Implemented (`USE_TIME_DELAY` in `alarm.c`). This is also the driver's default (x068). |
| Driver `cfg ... delay_normal=N` | Already in the supplied `driver.c`, which I did not modify. |

Other artifacts updated:
- `SPEC.md` is now v1.1. A5 defines which delay applies to which transition, including the sentinel. A5a says "its delay (A5)" instead of `time_delay_s`. A6a now states that FAULT→NORMAL recovery stays immediate and is not timed by either delay.
- `tests/unit.vec` has new vectors x064 to x071 (tag `CR201`). They cover shorter and longer return delays, the unchanged forward and HIGH↔LOW delays, the sentinel (both explicit and default), cancellation of a pending return (A5a), immediate fault recovery, and escalation while a long return delay is pending. x064, x065, x066, x069 and x071 fail against the old logic. `make` builds cleanly, and `run_vectors.py tests/unit.vec acceptance.vec` gives 52/52.

Open points:
- **FAULT→NORMAL:** I assumed the CR does not cover this transition, because it is not a timed transition (A6a [POL], immediate). The spec now states this explicitly. If the product owner also wants Time_Delay_Normal to apply to fault recovery, that would change the [POL] rule A6a and needs a separate decision.
- **Maintenance catch-up:** the catch-up notification (A10) is unchanged, because it is not a timed transition.
- **Sentinel limits:** because of the sentinel, a real Time_Delay_Normal of 4294967295 s (about 136 years) cannot be set. An object/property layer that exposes the optional Time_Delay_Normal property has to map "absent" to `0xFFFFFFFF`. That layer is outside this module.
- **Supplied header:** `alarm.h` does not name the sentinel. I used a private define in `alarm.c` so I would not have to edit the supplied header. It could be moved to `alarm.h` if integrators need it.
