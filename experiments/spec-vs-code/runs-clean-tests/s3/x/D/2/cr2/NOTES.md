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

## CR-202

RAM budget: `alarm_t` is 32 bytes and `alarm_init` keeps a pointer to the configuration.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. `alarm.h`
  was already updated. `point_t` in `alarm.c` now holds a `const alarm_cfg_t *`
  instead of a copy of the config. Its members are ordered largest first, so there is no
  internal padding. It is 28 bytes with 4-byte pointers (Cortex-M0; I checked this with
  `clang --target=thumbv6m-none-eabi`) and exactly 32 bytes on the 64-bit host build.
  `_Static_assert` checks the size, and a new `_Static_assert` checks the alignment.
- **`alarm_init` keeps a pointer, not a copy; the caller keeps the config alive and may
  share it between points**: implemented. The module only reads the config. It never
  writes to the config and keeps no per-point data in it. I checked this with a
  throwaway test (two points on one `const` config, one alarming and one not); the test
  is not kept.
- **External behavior must not change**: holds, if the configuration is not modified
  while points use it. All 49 vectors pass unchanged
  (`make && python3 run_vectors.py acceptance.vec tests/unit.vec`). No vector changes a
  config after `init`. No new vectors were added: the driver has one point and one
  config, so it cannot show sharing.

No conflict with SPEC or with ASHRAE 135 was found: neither says how the implementation
stores its configuration. `SPEC.md` is now v1.2, with a paragraph that documents the
memory/configuration contract. No rule changed.

Things I am unsure about:
- **Changing a config at run time is now a behavior change.** Before, a change to a
  config after `alarm_init` had no effect until re-init. Now it affects every point that
  shares the config, on its next call. That includes limits and delays of a transition
  that is already pending (the delay is read when the timer is evaluated). SPEC v1.2
  says to re-initialize points after changing their config. This keeps the old
  behavior, but it also resets their alarm state and acknowledgement. BACnet
  High_Limit/Low_Limit/Deadband/Time_Delay are normally writable. If the object layer
  must apply such writes without re-init, how live changes behave (in particular a
  pending timer) needs its own CR, and possibly review against ASHRAE 135 and [POL]
  A8/A10. If the config is written from another context (ISR/task) while an alarm call
  runs, the call can read a half-updated config. The caller must serialize this.
- **The RAM arithmetic does not close.** 512 points × 32 bytes = 16,384 bytes = the
  whole 16 KiB of RAM. Nothing is left for the shared configs, the stack or the rest of
  the firmware. On the M0 `point_t` needs only 28 bytes, so 4 bytes per point
  (2 KiB in total) are lost to the fixed 32-byte `alarm_t`. It could shrink to 24 bytes
  (e.g. `uint32_t storage[6]`) by packing state/last_notified/pending and the four
  flags into 2 bytes. On the 64-bit host build the pointer is 8 bytes, so that size
  would need a host-specific definition or a config index instead of a pointer. I did
  not change `alarm.h`. The firmware lead should confirm the budget.
- **`alarm_init` still does not validate `cfg`** (NULL is not checked, as before).
  A dangling pointer is now the caller's responsibility.
