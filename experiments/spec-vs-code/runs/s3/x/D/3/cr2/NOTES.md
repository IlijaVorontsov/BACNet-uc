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

## CR-202

RAM budget: a smaller `alarm_t`, and the configuration is kept by pointer.

| Item | Status |
|---|---|
| `alarm_t` is 32 bytes (`uint64_t storage[4]`) | **Implemented.** `alarm.h` was already in place (as delivered). `point_t` in `alarm.c` now fits: 32 bytes with 8-byte pointers (host), 28 bytes on Cortex-M0. `_Static_assert`s check its size and alignment against `alarm_t`. I checked the M0 layout with `clang --target=thumbv6m-none-eabi`. |
| `alarm_init` keeps a pointer instead of copying the config | **Implemented.** `point_t.cfg` is now `const alarm_cfg_t *`. The module only reads through it, so a shared config can be `const` (in flash). |
| Caller keeps the `alarm_cfg_t` alive; configs are shared between points | Nothing to do in the module. This is documented in `alarm.h` (as delivered), in the `point_t` comment, and in `SPEC.md`. |
| External behavior must not change | **Met.** All 54 vectors pass (`tests/unit.vec` and `acceptance.vec`), also under ASan/UBSan. A throwaway test ran 512 points sharing one `static const` config, with different histories. The points stayed independent and the config was not written. |

No conflict with `SPEC.md` or with BACnet was found for the items themselves.

Other changes:
- I removed `has_value` from the point state. It was written but never read. A13 still
  holds, because nothing can be pending before the first sample.
- `SPEC.md` is now v1.2. It has a short, informative "Memory" paragraph and no rule changes.
- There are no new vectors, because nothing externally visible changed. The driver has one
  point and one static config, so the vector format cannot express sharing.

Unsure / for review:
- **The RAM budget does not add up.** 512 × 32 bytes = 16,384 bytes. That is all 16 KB,
  and nothing is left for the stack, the BACnet stack or buffers. On M0 the state needs
  only 28 bytes, but `uint64_t storage[4]` forces 32 bytes and 8-byte alignment, which
  wastes 2 KB over 512 points. `uint32_t storage[7]` would be 28 bytes (14 KB). Packing the
  flags into bits would bring it to 24 bytes (12 KB). Either option needs a header change,
  and the CR fixed the header, so I left it as it is. Please confirm the budget.
- **Changing a config while points use it.** With the old copy, a change had no effect
  until `alarm_init` ran again. Now it takes effect at the next call of **every** point
  that shares the config. A pending transition keeps its start time. Its target is
  recomputed at the next sample, but a changed delay applies at once. `SPEC.md` leaves
  this undefined. In BACnet, High_Limit, Low_Limit, Deadband, Time_Delay and
  Time_Delay_Normal belong to each object. If the object layer accepts WriteProperty on
  them, it must not write into a shared config: a write to one object would silently
  change the others (copy-on-write, or one config per distinct set of values). Writes from
  another task or ISR could also be read half-updated in the middle of a call.
- **Dangling pointers.** Any caller that builds the config in a local variable and then
  calls `alarm_init` now fails without any warning. Every `alarm_init` call site outside
  this directory needs to be checked.
- The cast from `alarm_t *` to `point_t *` is the same pattern as before, and I did not
  change it. It is formally a strict-aliasing violation, and `-Wstrict-aliasing=1` warns
  about it. The build (`-O2 -Wall -Wextra`) gives no warnings.
