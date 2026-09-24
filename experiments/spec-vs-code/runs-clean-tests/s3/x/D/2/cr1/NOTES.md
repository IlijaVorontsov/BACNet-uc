# Notes

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: done. The field was
  already in `alarm.h`. `alarm_init` copies the whole config, so nothing else was
  needed there. `point_t` still fits in `alarm_t` (checked by the existing
  `_Static_assert`).
- **HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s`**: implemented.
  `alarm.c` now picks the delay from the pending target (`delay_for()`, used by
  `eval_timer()`).
- **All other delayed transitions (NORMAL→HIGH/LOW, HIGH↔LOW) keep `time_delay_s`**:
  implemented.
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented
  (`ALARM_DELAY_NORMAL_SAME` in `alarm.c`).

No conflict found. This matches ASHRAE 135 clause 13.3.6 (OUT_OF_RANGE): the
transitions to NORMAL use pTimeDelayNormal, the transitions to HIGH_LIMIT and LOW_LIMIT
(including HIGH↔LOW) use pTimeDelay, and if Time_Delay_Normal is absent, Time_Delay
is used. No [POL] rule is affected.

Other changes to keep the artifacts consistent:
- `SPEC.md` is now v1.1. A5 [STD] defines the delay per transition. A5a's timer
  evaluation now refers to "the delay of the pending target" instead of `time_delay_s`.
- `tests/unit.vec` has new vectors x042–x046 (tag `CR-201`):
  - different normal and alarm delays in both directions (x042, x043);
  - explicit `0xFFFFFFFF` (x044);
  - a pending return-to-normal replaced by a HIGH→LOW target, which restarts on
    `time_delay_s` (x045);
  - FAULT recovery staying immediate (x046).
  `acceptance.vec` is unchanged and still passes with the default.
- All 49 vectors pass (`make && python3 run_vectors.py acceptance.vec tests/unit.vec`).

Things I am unsure about:
- **FAULT→NORMAL recovery (A6a [POL]) stays immediate** and does not use
  `time_delay_normal_s`. The CR covers only HIGH/LOW→NORMAL, and changing A6a would need
  product-owner sign-off. Vector x046 covers this.
- **A delay is chosen by the pending target, at evaluation time.** When a pending
  target changes (for example, return-to-normal pending, then the value drops below
  `low_limit`), the new pending transition starts at `now` as before (A5a). It uses the
  new target's delay.
- **A real delay of exactly 4294967295 s cannot be set for return-to-normal**, because
  that value is the sentinel. BACnet has no such sentinel: an absent property means
  "use Time_Delay". If the value is ever exposed as a writable BACnet property, the
  object layer must map "absent" to 0xFFFFFFFF and reject or clamp a written value of
  4294967295.
