# Notes

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: already done in the delivered
  `alarm.h`. I left `alarm.h` as it was.
- **HIGH/LOW → NORMAL use `time_delay_normal_s`**: implemented (`pending_delay()` in
  `alarm.c`, used by `eval_timer()`). This matches ASHRAE 135 clause 13.3.6
  (OUT_OF_RANGE uses pTimeDelayNormal for transitions to NORMAL), so it does not conflict
  with the standard or with any [POL] rule. SPEC A5/A5a updated, spec bumped to v1.1.
- **NORMAL→HIGH/LOW and HIGH↔LOW keep `time_delay_s`**: implemented. This also matches
  13.3.6 (pTimeDelay).
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. It works the same way as
  an absent Time_Delay_Normal property in BACnet.
- Not changed: FAULT→NORMAL recovery (A6a) and TO_FAULT (A6) stay immediate. The CR
  only covers HIGH/LOW→NORMAL, and these are [POL] rules. Returning to NORMAL still does
  not clear the unacknowledged status (A8/SP-3), whatever the delay.
- Tests: added 4 CR-201 scenarios to `acceptance.vec` (separate return delay, direct
  LOW→HIGH keeps `time_delay_s`, `delay_normal=0` plus fault recovery, and the sentinel).
  All 7 pass. `driver.c` and `run_vectors.py` are unchanged.

Open points:
- Integration risk: a caller that builds `alarm_cfg_t` with `memset(0)` or with a
  designated initializer that leaves out the new field gets `time_delay_normal_s = 0`.
  For that caller, HIGH/LOW→NORMAL becomes immediate instead of "same as
  `time_delay_s`". Every existing configuration site outside this module must set the
  field explicitly (`0xFFFFFFFF` keeps the old behaviour). I could not check those sites
  from here.
- Because `0xFFFFFFFF` is the sentinel, a literal return delay of 4294967295 s cannot be
  configured. It is roughly 136 years, so it does not matter in practice.
- Exposing Time_Delay_Normal as a BACnet object property (read/write, and presence for
  the BTL listing) is outside this module and was not done here.
