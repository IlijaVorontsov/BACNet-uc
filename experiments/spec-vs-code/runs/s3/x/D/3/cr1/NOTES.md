## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|---|---|
| `uint32_t time_delay_normal_s` in `alarm_cfg_t` | Already in `alarm.h` (as delivered). `alarm.c` now uses it. |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | **Implemented** (`delay_for()` in `alarm.c`, used by `eval_timer`). |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | **Implemented** (no change to those paths). |
| `0xFFFFFFFF` = "same as `time_delay_s`" | **Implemented**. |

No conflict found. This is how ASHRAE 135 clause 13.3.6 (OUT_OF_RANGE) works:
returns to NORMAL use pTimeDelayNormal, which is Time_Delay_Normal when present and
Time_Delay when absent. The HIGH_LIMIT↔LOW_LIMIT and NORMAL→offnormal transitions use
pTimeDelay. The sentinel stands for "property absent".

Other changes, kept consistent with the above:
- `SPEC.md` is now v1.1. A5 [STD] states which delay applies to which transition.
  In A5a [POL], "`now − start ≥ time_delay_s`" now reads "≥ the delay of the pending
  target". The timer semantics are otherwise unchanged. This edits a [POL] rule; the CR
  comes from the product owner, and I take that as the required sign-off.
- `tests/unit.vec` has new vectors `y201a`–`y201j` (tag `CR201`). They cover a longer
  and a shorter return delay, the explicit sentinel, HIGH→LOW and LOW→HIGH still using
  `time_delay_s`, a pending NORMAL replaced by a pending LOW, fault recovery staying
  immediate, maintenance catch-up, and the value 0xFFFFFFFE (a real delay, not the
  sentinel). All 54 vectors pass. Seven of the new ones fail against the old code.
- `acceptance.vec` is unchanged. Its scenarios leave `delay_normal` at the default, and
  they still pass.

Unsure / for review:
- **Existing configs:** in C, any `alarm_cfg_t` that is zero-initialised, or built with
  a positional or designated initializer that leaves out the new field, gets
  `time_delay_normal_s = 0`. That means an immediate return to normal, not "same as
  `time_delay_s`". Every place that builds a config (outside this directory) must set
  `0xFFFFFFFF` explicitly. Otherwise the return-to-normal delay changes without anyone
  noticing. It might be safer to make 0 the "same" sentinel, but the header is already
  fixed by the CR, so I did not change it.
- The sentinel means an actual Time_Delay_Normal of 4294967295 s cannot be configured.
  That is about 136 years, so it does not matter in practice. The BACnet object layer
  must still map "property absent" to `0xFFFFFFFF`.
- FAULT transitions (A6 and A6a) and the maintenance catch-up (A10) are still not
  delayed. The fault recovery TO_NORMAL does not use `time_delay_normal_s`. I read the
  CR as covering only the HIGH/LOW→NORMAL limit transitions.
- Existing behaviour, now easier to see: in HIGH, suppose a pending NORMAL has a short
  normal delay that expires between two calls. If the next sample meets the low
  condition, A4 (direct transition wins) replaces the pending NORMAL with a pending LOW,
  and the timer restarts with `time_delay_s`. This follows A4/A5a as written. I did not
  change it.
