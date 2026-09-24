## CR-201

Separate return-to-normal delay (Time_Delay_Normal).

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Already in `alarm.h` as delivered; used unchanged. `point_t` grows by 4 bytes and still fits in `alarm_t` (the static assert still holds). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | **Implemented** (`delay_for()` in `alarm.c`, used by `eval_timer`). |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | **Implemented.** |
| `0xFFFFFFFF` = "same as `time_delay_s`" | **Implemented** (`DELAY_NORMAL_SAME` in `alarm.c`). |

No conflicts found. The CR matches BACnet OUT_OF_RANGE (135 clause 13.3.6). pTimeDelayNormal
applies to the HIGH_LIMIT/LOW_LIMIT→NORMAL transitions. pTimeDelay applies to the others,
including the direct HIGH↔LOW ones. When Time_Delay_Normal is absent, Time_Delay is used,
and the sentinel stands for that case. No [POL] rule changes meaning. The CR comes from the
product owner.

Other artifacts updated:
- `SPEC.md` is now v1.1. A5 [STD] now defines the delay for each transition. A5a now
  refers to "the applicable delay (A5)" instead of `time_delay_s`, and the timer semantics
  are otherwise unchanged. A5 also says that FAULT→NORMAL stays immediate (A6a,
  unchanged). With `time_delay_s = 0` the A6a example of a second transition still holds.
- `tests/unit.vec` has new blocks x070–x078 (tag `CR-201`). They cover a longer delay,
  a shorter delay and a zero delay on return. They check that HIGH↔LOW and NORMAL→offnormal
  still use `time_delay_s`, and that the explicit sentinel works. They also cover
  cancel/restart of a pending return, immediate FAULT→NORMAL, and `0xFFFFFFFE` as a real
  delay. The last block covers maintenance catch-up and escalation with a delayed return.
- `acceptance.vec` is unchanged. The driver defaults `delay_normal` to the sentinel, so the
  existing scenarios keep their meaning. All 53 blocks pass.

Unsure / to flag:
- **Zero-initialised configs change behaviour.** Existing integration code might build
  `alarm_cfg_t` with `memset(…, 0, …)`, `= {0}`, or designated initialisers that omit the
  new field. In that code `time_delay_normal_s` is 0, not the sentinel. Returns to NORMAL
  then become immediate instead of following `time_delay_s`. Every caller must set the
  field explicitly, using `0xFFFFFFFF` for the old behaviour. A named constant in
  `alarm.h` might help. I did not add one because the header was delivered as final.
- The sentinel takes one value from BACnet's Unsigned range. If the object layer writes a
  Time_Delay_Normal of exactly 4294967295, it is read as "absent / same as Time_Delay".
  That is harmless in practice (about 136 years), but the BACnet property mapping must
  write the sentinel when the property is absent and must not pass 4294967295 through
  unchanged.
- The delay is chosen from the *pending target*. A pending NORMAL target only exists in
  HIGH or LOW. If the configuration changed while a transition was pending, the new delay
  would apply to the running timer. That cannot happen today because `alarm_init` is the
  only way to set the configuration.
