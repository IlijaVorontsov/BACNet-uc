# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). I found no conflict with BACnet or with the existing behaviour and tests, so every item below is implemented. There is no requirements document in this directory; the unit vectors cite requirement IDs A2–A13, but the document itself is missing, so I checked the change only against the existing vectors. All 44 original vectors still pass, and so do the 8 new ones (52 of 52 in total).

| Item | Status |
|------|--------|
| Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. The field was already in the supplied `alarm.h`, which I left unchanged. `alarm_init` copies it together with the rest of the configuration, and `point_t` still fits in `alarm_t` (the `_Static_assert` still holds). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented through `pending_delay()` in `alarm.c`, which `eval_timer` now calls. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | Implemented. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME` in `alarm.c`). This value is the driver's default, so existing configurations behave exactly as before. |
| `driver.c` `delay_normal=N` / updated `alarm.h` | I checked that both are present and did not modify either one. |

Tests: I added vectors `y001` to `y008` (tag `CR201`) to `tests/unit.vec`. They cover:
- a longer and a shorter normal delay
- HIGH↔LOW transitions still using `time_delay_s`
- the explicit `0xFFFFFFFF` value
- the timer restarting when the value leaves the band
- the return to normal during maintenance
- FAULT→NORMAL

I left `acceptance.vec` unchanged; its scenarios still pass.

BACnet consistency: this change matches Time_Delay_Normal semantics. The OUT_OF_RANGE algorithm uses pTimeDelayNormal for transitions to NORMAL and pTimeDelay for everything else. When Time_Delay_Normal is absent, Time_Delay is used, which is what the `0xFFFFFFFF` value represents here.

Points I am unsure about:
- **FAULT→NORMAL.** This stays immediate, with no `time_delay_normal_s` delay (vector `y007`). BACnet treats leaving FAULT as a reliability change, not as an event-algorithm transition, and existing vectors x029 and x030 already rely on it being immediate. I read the CR's "HIGH or LOW back to NORMAL" as not covering FAULT.
- **Value falling from above high to below low while in HIGH.** The code keeps its existing behaviour: LOW is the pending target and is timed with `time_delay_s`. This follows the CR's "HIGH↔LOW keep using time_delay_s". However, the BACnet algorithm also treats "below high−deadband for Time_Delay_Normal" as satisfied at the same time. So if `time_delay_normal_s` < `time_delay_s`, a strict reading of BACnet could give TO_NORMAL first, where this code goes straight to LOW. The pre-existing restart-on-target-change rule (A5a, x013) is related and is also unchanged. If product wants the strict BACnet behaviour, that needs its own CR.
- **The `0xFFFFFFFF` value.** Because it means "same as `time_delay_s`", a literal delay of 4294967295 s (about 136 years) cannot be configured. I consider this harmless.

## CR-202

This CR asks for a smaller `alarm_t` and for `alarm_init` to keep a pointer to the configuration. I found no conflict with BACnet or with the existing behaviour and tests, so every item is implemented. There is a problem with the RAM arithmetic, though (see the first point under "Points I am unsure about"). All 52 unit vectors and the 3 acceptance scenarios pass unchanged.

| Item | Status |
|------|--------|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Implemented. The updated `alarm.h` was already in place and I left it unchanged. In `alarm.c`, `point_t` no longer contains an `alarm_cfg_t`. Its fields are reordered largest first, so it takes 28 bytes with 32-bit pointers (Cortex-M0; checked with `clang --target=thumbv6m-none-eabi`) and 32 bytes with 64-bit pointers (host test build). `_Static_assert`s check both its size and its alignment against `alarm_t`. |
| `alarm_init` keeps a pointer instead of copying the configuration | Implemented. `point_t.cfg` is now `const alarm_cfg_t *`, and every read of a limit, delay or escalation time goes through it. |
| The caller keeps the `alarm_cfg_t` alive, and configurations are shared between points | Relied on. The module cannot check this. The driver's configuration is `static`, so it meets the rule. |
| External behavior must not change | Implemented. Besides the vectors, I compared the new code with the previous copying implementation, rebuilt temporarily with a larger `alarm_t`. I ran 15,000 random scenarios (about 280,000 commands, covering every command type, `delay_normal`, time wrap and time going backwards) and the outputs were byte-for-byte identical. |

I added no new vectors. For a caller that keeps its configuration unchanged, nothing is externally different. The only new behaviour is the one described under "Changing a configuration after `alarm_init`", and the CR does not specify it, so I did not fix it in a test.

Points I am unsure about:
- **The RAM budget does not add up.** 512 points × 32 bytes = 16,384 bytes, which is all of a 16 KB part. That leaves nothing for the shared `alarm_cfg_t`s (24 bytes each), the stack, the notification buffers or the BACnet stack. The firmware lead should confirm the figures. Options that do not touch the alarm logic:
  - On the target, `point_t` needs only 28 bytes, so a 28-byte `alarm_t` (14 KB for 512 points) would work. The host build with 64-bit pointers still needs 32 bytes.
  - Packing the state, pending target and flags into bit-fields would bring it to about 24 bytes on the target (12 KB).
  - Storing a 16-bit configuration index instead of a pointer would save more.

  I did not change `alarm.h`, because the CR says it is already in place.
- **Changing a configuration after `alarm_init`.** Before, changes had no effect until the next `alarm_init`, and that call also reset the point's state. Now a change takes effect at the next call on every point that shares that `alarm_cfg_t`. The CR does not say whether in-place changes are allowed. If they are, the writes are not atomic: a change made from another task or an ISR during an `alarm_*` call can be seen half-applied, for example a new `high_limit` with the old `deadband`. A configuration on the stack or freed early now leaves a dangling pointer. This is the caller's responsibility under the new contract.
- **BACnet integration.** High_Limit, Low_Limit, Deadband, Time_Delay and Time_Delay_Normal are per-object properties. A WriteProperty to one object must not change other objects that share its `alarm_cfg_t`, so the application layer has to give that object its own configuration first (copy-on-write). There is no API to move a point to a different `alarm_cfg_t` without `alarm_init`, and `alarm_init` resets the point's alarm state. This is not a conflict inside this module, but the integrator needs to know it.
- **Earlier notes.** The CR-201 section above says that `alarm_init` "copies" the configuration. CR-202 supersedes that. The unused `has_value` field (written, never read) is still there. It fits in the budget, and removing it would not reduce `sizeof(alarm_t)`.
