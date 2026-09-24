## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|------|--------|
| Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. The field was already in `alarm.h`. `alarm_init` copies the whole configuration, so the new field is included. The private `point_t` grew by 4 bytes and still fits in `alarm_t`; the `_Static_assert` still passes. |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented. The new helper `delay_for()` in `alarm.c` chooses the delay from the pending target, and `eval_timer()` uses it. The A5a timer rules are unchanged: the transition happens when `now - pending_start >= delay`, a delay of 0 means the transition happens on the sample itself, and the transition only happens when a call observes it. |
| All other delayed transitions (NORMAL→HIGH/LOW, HIGH↔LOW) keep `time_delay_s` | Implemented. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented (`TIME_DELAY_NORMAL_SAME` in `alarm.c`). |

No conflict found. This matches the BACnet OUT_OF_RANGE algorithm (ASHRAE 135
clause 13.3.6). In that algorithm, transitions to NORMAL use pTimeDelayNormal and
the other transitions use pTimeDelay. When Time_Delay_Normal is absent,
Time_Delay is used, which the sentinel models. The CR was sent by the product
owner, so it counts as their sign-off for the A5a wording change: "time_delay_s"
becomes "the transition's delay".

Other changes:
- The comments in `alarm.c` (the header contract summary, `eval_timer`, and
  `alarm_sample`) now describe the new delay selection.
- `acceptance.vec` has 6 new `cr201-*` blocks. They cover: the longer and
  shorter normal delay, HIGH↔LOW and NORMAL→offnormal still using
  `time_delay_s` while `delay_normal` is set, `delay_normal=0`, the explicit
  `4294967295` sentinel, and the fact that FAULT→NORMAL recovery is not delayed.
  All 9 blocks pass (`make && python3 run_vectors.py acceptance.vec`).

Things I am unsure about:
- **SPEC.md is not in this directory.** `alarm.c` says it implements SPEC.md,
  but I could not update the A5/A5a text there. The spec owner should update it
  to match: return to NORMAL uses `time_delay_normal_s`, and `0xFFFFFFFF` means
  "same as `time_delay_s`".
- **Behaviour changes for existing integrators.** Code that builds
  `alarm_cfg_t` with a zero-initialized struct or a designated initializer and
  does not set the new field now gets `time_delay_normal_s = 0`. That means an
  immediate return to NORMAL, not the previous "same as `time_delay_s`"
  behaviour. All `alarm_cfg_t` initializers in the product should be reviewed
  and set the field explicitly, usually to `0xFFFFFFFF`.
- **The sentinel takes up one real value.** Because of the sentinel, a
  Time_Delay_Normal of exactly 4294967295 s cannot be configured. If a BACnet
  client writes that value to the property, the BACnet object layer (outside
  this module) should reject it or map it before it reaches `alarm_cfg_t`.
- **FAULT→NORMAL recovery (A6a) is still immediate.** I read the CR's "delayed
  transitions from HIGH or LOW" as not covering it. I left the [POL] rule
  unchanged.
