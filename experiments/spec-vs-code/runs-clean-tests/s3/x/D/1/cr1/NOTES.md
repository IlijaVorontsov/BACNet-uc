# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Conflict check: none found. This matches ASHRAE 135 clause 13.3.6 (OUT_OF_RANGE). There,
the returns HIGH_LIMIT→NORMAL and LOW_LIMIT→NORMAL use pTimeDelayNormal. All other
transitions, including the direct HIGH↔LOW ones, use pTimeDelay. pTimeDelayNormal is
Time_Delay_Normal when that property is present, and Time_Delay otherwise. The CR comes
from the product owner, which covers the change to the [POL] rule A5a.

Items:

1. **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`:** implemented. `alarm.h`
   already had the field and I left it unchanged. `alarm_init` copies it with the rest
   of the configuration. `point_t` still fits in `alarm_t` (the static assert passes).
2. **HIGH/LOW → NORMAL use `time_delay_normal_s`:** implemented. The new
   `pending_delay()` in `alarm.c` picks the delay for the pending transition, and
   `eval_timer` uses it. A pending NORMAL target only happens in HIGH or LOW, so
   "pending == NORMAL" means "HIGH/LOW back to NORMAL".
3. **All other delayed transitions (NORMAL→HIGH/LOW, HIGH↔LOW) keep `time_delay_s`:**
   implemented, unchanged. FAULT entry and FAULT→NORMAL recovery (A6/A6a) are still
   immediate. The CR does not cover them.
4. **`0xFFFFFFFF` means "same as `time_delay_s`":** implemented
   (`DELAY_NORMAL_SAME` in `alarm.c`). It models an absent Time_Delay_Normal property.

Other artifacts:
- `SPEC.md` is now v1.1. A5 describes both delays and the sentinel. A5a's timer rule
  now refers to "the time delay of that transition".
- `tests/unit.vec` has new vectors x070–x078 (tag `CR201`): longer and shorter
  return-to-normal delay, HIGH↔LOW still on `time_delay_s`, an explicit sentinel,
  tick-driven return, a pending return replaced by a direct HIGH→LOW, FAULT recovery
  staying immediate, delay 0xFFFFFFFE, and a return during maintenance.
  All 53 vectors pass (acceptance + unit). Six of the new vectors fail against the old
  code, as expected.
- `acceptance.vec`, `driver.c`, `run_vectors.py`: not changed. The driver defaults to
  0xFFFFFFFF, so earlier behavior is unchanged.

Unsure / for the product owner:
- **Compatibility risk for existing integrations:** a caller that builds `alarm_cfg_t`
  with zero-initialization or designated initializers, and does not set the new field,
  gets `time_delay_normal_s = 0`. That point then returns to NORMAL immediately instead
  of after `time_delay_s`. To keep the old behavior, every existing configuration site
  must set 0xFFFFFFFF. A named constant in `alarm.h` (for example
  `ALARM_DELAY_NORMAL_SAME`) would help, but I left the provided header unchanged.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s cannot be
  configured. BACnet Unsigned allows it, but it does not matter in practice.
- When BACnet Time_Delay_Normal is written through the object model, the BACnet layer
  must map "property absent" to 0xFFFFFFFF. That mapping is outside this module.
