# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). No item conflicts with the
BACnet standard or with the documented rules A1-A13, so every item was implemented.

| # | Item | Status |
|---|------|--------|
| 1 | Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. The field was already in the delivered `alarm.h`. `alarm.c` now reads it. `alarm_init` already copies the whole config, and `point_t` still fits in `alarm_t` (the `_Static_assert` holds). |
| 2 | HIGH/LOW -> NORMAL uses `time_delay_normal_s`; NORMAL -> HIGH/LOW and HIGH <-> LOW keep `time_delay_s` | Implemented. The new helper `pending_delay()` in `alarm.c` picks the delay and `eval_timer()` uses it. This matches the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6): transitions to NORMAL use pTimeDelayNormal and every other transition uses pTimeDelay. The A5a timer semantics are unchanged: when the countdown starts or restarts, cancellation on "no target", `>=` comparison, and a delay of 0 meaning immediate. |
| 3 | `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME` in `alarm.c`). This matches BACnet, where pTimeDelayNormal = pTimeDelay when Time_Delay_Normal is absent. |

Other changes made to keep things consistent:
- `alarm.c`: I updated the contract summary and the A5/A5a comments at `eval_timer`
  to name both delays.
- `acceptance.vec`: I added 4 scenarios (7/7 pass, also under ASan/UBSan):
  - `return-to-normal-uses-delay-normal`: a return to normal waits the longer `delay_normal`.
  - `offnormal-and-high-low-keep-time-delay`: with `delay_normal=0`, NORMAL->LOW and
    LOW->HIGH still wait `delay`, and the return to NORMAL is immediate.
  - `delay-normal-all-ones-means-same-as-delay`: an explicit 4294967295 acts as `delay`.
  - `fault-recovery-stays-immediate`: see the second point below.
- `driver.c`, `run_vectors.py` and `Makefile`: not modified.

Open points:
- **SPEC.md** is referenced by `alarm.c` but is not in this directory, so I could not
  update it. Its A5 text (and the A5a wording "time_delay_s") should be changed to say
  "`time_delay_normal_s` for returns to NORMAL". I changed the A5a comment wording
  (a [POL] rule) only to name the new delay, not its behaviour. I took the CR, which comes
  from the product owner, as the sign-off for that.
- **FAULT -> NORMAL recovery** stays immediate and ignores `time_delay_normal_s`.
  The CR only covers HIGH/LOW -> NORMAL, and A6a [POL] requires immediate recovery.
  BACnet also does not delay fault transitions. Please confirm this reading.
- **Sentinel vs. a real value**: a configured Time_Delay_Normal of exactly 4294967295 s
  cannot be told apart from "absent". The BACnet object layer should map a missing
  Time_Delay_Normal property to 0xFFFFFFFF. It should also reject a write of 4294967295
  (e.g. VALUE_OUT_OF_RANGE) or treat it as absent. That layer is outside this module.
- The delay is chosen when the timer is evaluated, from the target of the pending
  transition. Maintenance mode (A10), escalation (A9) and acknowledgement (A8) are
  unaffected: a return to normal during maintenance also waits `time_delay_normal_s`,
  it just produces no notification.
