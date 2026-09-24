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

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by pointer.

| Item | Status |
|------|--------|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`, `alarm.h` already updated) | Implemented. The private `point_t` in `alarm.c` no longer has the 24-byte copy of `alarm_cfg_t`. Its members are now ordered from largest to smallest, so there is no internal padding. It is 28 bytes on the Cortex-M0 (4-byte pointer: 27 bytes plus tail padding) and 32 bytes on a 64-bit host (8-byte pointer: 31 bytes plus padding). The `_Static_assert(sizeof(point_t) <= sizeof(alarm_t))` passes. I added a second `_Static_assert` to check that `alarm_t` is at least as strictly aligned as `point_t`, because `P()` casts one to the other. |
| `alarm_init` keeps a pointer instead of copying; the caller keeps the `alarm_cfg_t` alive; configurations are shared between points | Implemented. `alarm_init` stores `cfg`. `target()`, `delay_for()` and `check_escalate()` read the configuration through that pointer. Several points can share one `alarm_cfg_t`, because the module never writes to it (it is `const`). |
| External behaviour must not change | Implemented, with one condition (see the first point below). No rule changed: A1-A13 and the CR-201 delay selection are the same code, and they now read the same values through a pointer. `make && python3 run_vectors.py acceptance.vec` passes all 9 blocks. I did not add any vectors: the driver has one point and a static `cfg`, and the only new behaviour a vector could show (a `cfg` change after `init`) is not something we want to specify. |

No conflict with the BACnet standard or the documented rules. Everything in the
CR is about how the data is stored, and none of the [STD]/[POL] rules changed.

Other changes: the comments in `alarm.c` (file header, `point_t`, `alarm_init`)
now say that the configuration is kept by pointer. The old line "alarm.h: the
configuration is copied" is gone. `driver.c`, `run_vectors.py` and `alarm.h` are
unchanged. The CR-201 section above says "`alarm_init` copies the whole
configuration"; that was true then, and CR-202 replaces it.

Things I am unsure about:
- **Changing a configuration after `alarm_init` now affects the points.**
  Before, `alarm_init` copied the configuration, so a later change to the
  caller's `alarm_cfg_t` did not affect points that were already initialized.
  Now the change takes effect at the next call of *every* point that shares that
  configuration, even in the middle of a pending time delay or an escalation
  period. For example, a BACnet write to High_Limit or Time_Delay would do this.
  If the change comes from another context (an ISR or a task) while a point is
  being evaluated, a call can also see a partly updated configuration. So
  "external behaviour does not change" only holds if configurations are not
  modified while points use them. `alarm.h` only says the caller keeps the
  configuration *alive*. I suggest the lead adds "and unchanged (re-run
  `alarm_init` after changing it)" to `alarm.h`, or decides that live changes
  are allowed. I did not change the header's contract myself.
- **The RAM budget does not work.** 512 points × `sizeof(alarm_t)` = 512 × 32 B
  = 16 384 B, which is all 16 KB of RAM. That leaves nothing for the shared
  configurations (24 B each), the stack, the notification buffers or the
  BACnet stack. On the M0, `point_t` needs only 28 bytes. The other 4 bytes per
  point come from `uint64_t storage[4]` in the header. Changing that would only
  save 2 KB (`uint32_t storage[7]` = 28 B is enough on the target but not on a
  64-bit host build), which still does not fit. The firmware lead should check
  the point count or the RAM figure. This is outside `alarm.c`.
- **SPEC.md is still not in this directory.** If it says anything about
  `alarm_init` copying the configuration, or about reconfiguring a point, the
  spec owner needs to update it.
- **A null or dangling `cfg`.** A null `cfg` used to crash in `alarm_init`. Now
  the crash happens at the first call that reads the configuration. A dangling
  `cfg` (for example, a configuration on the stack of a function that has
  returned) is now silent memory corruption, not a harmless copy. Both are
  outside the caller's guarantee, but integrators should check where each
  `alarm_cfg_t` lives (static or const storage is best).
- I could not build for the Cortex-M0, and a 32-bit host build fails here
  because the 32-bit libc headers are missing. The 28-byte figure for the M0
  is my own calculation for ILP32 with 4-byte pointer alignment. The static
  assertion will check it on the real target build.
