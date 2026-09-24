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

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by pointer.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. The delivered
  `alarm.h` was already updated and I left it as it was. In `alarm.c` the internal
  `point_t` no longer holds a copy of `alarm_cfg_t` (24 bytes). It holds a
  `const alarm_cfg_t *`, and the members are reordered so there is no padding. It is
  28 bytes on Cortex-M0 (4-byte pointers, checked with clang `thumbv6m`) and exactly
  32 bytes on the 64-bit test host. `_Static_assert`s check both size and alignment.
- **`alarm_init` keeps a pointer, caller guarantees lifetime, configurations shared**:
  implemented. The module only reads the configuration (at every call) and never
  writes it. Two points sharing one `const` configuration stay independent (checked
  with a throwaway test program).
- **External behavior must not change**: met for every caller that does not modify
  the configuration after `alarm_init`. All 7 `acceptance.vec` scenarios pass. I also
  ran a throwaway differential test against the pre-CR implementation (about 4000
  random scenarios, 94k commands, covering faults, NaN/inf, maintenance, ack,
  escalation, time going backwards, and every `delay_normal` variant): the output was
  identical. I did not add any vectors because behavior is unchanged and the driver
  holds only one point. `driver.c`, `run_vectors.py` and `Makefile` are unchanged.
- SPEC bumped to v1.2. The new section 6 records the configuration-lifetime contract;
  rules A1–A13 are unchanged. I found no conflict with the SPEC's [STD]/[POL] rules or
  with ASHRAE 135.

Open points / things I am unsure about:
- **RAM arithmetic does not add up.** 512 × `sizeof(alarm_t)` = 512 × 32 B = 16384 B,
  which is all of the 16 KB of RAM, before stack, the configurations (if they are in
  RAM), notification buffers and the rest of the BACnet stack. The point state needs
  only 28 B on M0, but `alarm.h` fixes 32 B and 8-byte alignment. The firmware lead
  should recheck the budget. Options, not done here because they change the delivered
  header: `uint32_t storage[7]` (28 B, saves 2 KB), or packing state, pending and flags
  into bitfields (about 24 B, saves 4 KB).
- **One behavior does change, and it follows from the CR itself.** Up to v1.1, changing
  the caller's `alarm_cfg_t` after `alarm_init` had no effect on the point. Now the new
  values apply from the next call, to every point that shares that configuration.
  State, pending start time and alarm episode are kept. I documented this in SPEC
  section 6. The product owner should confirm it is acceptable.
- **Integration risk:** any existing call site that builds the configuration in a
  local or temporary variable and then calls `alarm_init` now leaves a dangling
  pointer (undefined behavior, no compile error). Every configuration site outside
  this module must be audited. I could not check them from here.
- **Concurrency:** the copy used to isolate a point from the caller's configuration.
  Now a configuration written from another context (ISR or task) while an `alarm_*`
  call is running can be read torn. Such writes must be serialized with the calls.
- **BACnet note for the caller, not this module:** High_Limit, Low_Limit, Deadband,
  Time_Delay and Time_Delay_Normal are properties of each object in ASHRAE 135. If
  they are writable, a WriteProperty to one object must not change other objects that
  share its `alarm_cfg_t`. The caller has to give that object its own configuration
  first (copy-on-write). Configurations placed in flash as `const` cannot be written
  at all.
- On 64-bit host builds `point_t` fills `alarm_t` exactly, so no field can be added
  later without growing the header again. The field `has_value` is set but never read
  (it was like that before this CR too); I left it in place.
