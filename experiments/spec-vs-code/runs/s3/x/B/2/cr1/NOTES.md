## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). None of the items conflict
with SPEC.md or with the BACnet standard. The CR matches ASHRAE 135 clause 13.3.6
(OUT_OF_RANGE), where HIGH_LIMIT/LOW_LIMIT → NORMAL uses pTimeDelayNormal and every other
transition, including HIGH_LIMIT ↔ LOW_LIMIT, uses pTimeDelay. A5 is a [STD] rule, so
changing it brings the module closer to the standard and does not touch any [POL] rule.

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. It was already in the delivered `alarm.h`, which I did not modify. `alarm_init` copies the whole config, so the field is stored. `point_t` still fits in `alarm_t` (the `_Static_assert` passes). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented. `delay_for()` in `alarm.c` picks the delay for the pending transition, and `eval_timer` uses it. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep `time_delay_s` | Implemented. This behaviour did not change. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented. The constant `DELAY_NORMAL_SAME_AS_DELAY` is local to `alarm.c`. |

Other changes to keep the artifacts consistent:
- `SPEC.md` is now v1.1. A5 now states which delay applies to each transition and the
  0xFFFFFFFF sentinel. A5a now says "its delay" instead of `time_delay_s`.
- `acceptance.vec` has 7 new `cr201-*` blocks. They cover both returns to normal, a
  return delay of 0, a return delay longer than `time_delay_s`, a direct HIGH→LOW with a
  short return delay, a pending return that is replaced by LOW (the timer restarts with
  `time_delay_s`), the explicit 0xFFFFFFFF value, and fault recovery. All 10 blocks pass.
  With the old code, 4 of the new blocks fail.

Things I am unsure about:
- **Fault recovery.** FAULT→NORMAL (A6a) and the maintenance catch-up (A10) are still
  immediate. They are not delayed by `time_delay_normal_s`. I read the CR as covering only
  HIGH/LOW→NORMAL, and BACnet does not apply Time_Delay_Normal to fault recovery either.
- **Sentinel value.** Because 0xFFFFFFFF is the sentinel, a real return delay of
  4294967295 s (about 136 years) cannot be configured. In BACnet that value is a legal
  Unsigned value for Time_Delay_Normal. An object layer that maps the property to this
  field needs to know this. For an absent property it should write 0xFFFFFFFF.
- **Public constant.** I did not add a named constant for the sentinel to `alarm.h`,
  because the CR says the updated header is already in place. Callers must write the
  literal `0xFFFFFFFF` or `UINT32_MAX`. A `#define` in the header may be worth adding.
- **Existing callers.** Callers that build `alarm_cfg_t` with zero-initialisation (for
  example `= {0}` or `memset`) now get `time_delay_normal_s = 0`. That means an
  **immediate** return to normal, not "same as `time_delay_s`". Every such caller
  outside this directory must set the field to 0xFFFFFFFF explicitly to keep the old
  behaviour. The test driver already does this.
