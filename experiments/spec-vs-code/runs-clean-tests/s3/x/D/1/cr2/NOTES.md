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

## CR-202

RAM budget: `alarm_t` is 32 bytes and `alarm_init` keeps a pointer to the configuration
instead of copying it.

Conflict check: none found. The change is internal to the module. It does not touch any
[STD] or [POL] rule in SPEC.md, and the alarm algorithm (ASHRAE 135 clause 13.3.6) is
unchanged.

Items:

1. **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`):** implemented. `alarm.h`
   already had the new size and I left it unchanged. With the old code the build failed
   on the `point_t` size check. `point_t` now holds a pointer instead of a copy of the
   configuration. It uses 32 bytes on the 64-bit host and 28 bytes on Cortex-M0. I
   checked the M0 size by cross-compiling `alarm.c` with
   `clang --target=thumbv6m-none-eabi`. I also added a static assert that `point_t`'s
   alignment is no stricter than `alarm_t`'s.
2. **`alarm_init` keeps a pointer instead of copying the configuration:** implemented.
   `point_t.cfg` is now `const alarm_cfg_t *`, and every call reads the configuration
   through it. The module never writes to the configuration.
3. **The caller keeps the configuration alive, and points may share it:** implemented
   as a caller contract. SPEC.md (now v1.2) states it in the module preamble. I tested
   this once, outside the build: 512 points sharing one `static const` configuration in
   read-only memory each had their own state, acknowledgement and escalation. I then
   deleted that test. `driver.c` drives only one point, so a test vector cannot cover
   sharing.
4. **External behavior must not change:** checked. All 53 vectors pass (acceptance +
   unit). I added no vectors because nothing observable changed.

Other artifacts: SPEC.md is now v1.2, with the configuration and lifetime paragraph at
the top. `alarm.h`, `driver.c`, `run_vectors.py` and the `.vec` files are not changed.

Unsure / for the firmware lead:
- **The RAM budget does not add up.** 512 × `sizeof(alarm_t)` = 512 × 32 = 16384 bytes,
  which is all of the 16 KB RAM. Nothing is left for the stack, the shared
  configurations (24 bytes each), the BACnet stack or buffers. On Cortex-M0, `point_t`
  needs only 28 bytes. `alarm_t` is 32 bytes only because the `uint64_t` storage forces
  8-byte alignment. `uint32_t storage[7]` would make it 28 bytes (14 KiB in total).
  Packing the flags would get it to 24 bytes (12 KiB in total). `has_value` is written
  but never read, so it can go. I did not change `alarm.h` because the CR fixes the
  size at 32 bytes. Please confirm the budget.
- **Changing a configuration in use now has an effect.** Before, the point kept a
  private copy, so later changes to the caller's struct did nothing. Now a change takes
  effect at the next call, for every point that shares that configuration, and it can
  happen in the middle of a pending delay. SPEC v1.2 says the configuration must not
  change while a point uses it, and that the way to reconfigure a point is still
  `alarm_init`. If live reconfiguration is wanted, its semantics need to be specified.
  The same applies if another task or ISR writes the configuration during a call: the
  point could read a mix of old and new values.
- **Existing call sites must be audited.** A caller that builds the configuration on
  the stack, or reuses one buffer to initialize several points, now leaves points
  pointing at a dead or changed configuration. The compiler does not catch this.
- **BACnet object layer.** High_Limit, Low_Limit, Deadband, Time_Delay and
  Time_Delay_Normal are properties of each object. A WriteProperty to one Analog Input
  must not be written into a configuration that other points share, because that would
  change other objects' properties. The object layer has to give that object its own
  configuration and call `alarm_init` again. That resets the point's alarm state, which
  was also true before this CR. This is outside the module.
