# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). No item conflicts with the
BACnet standard or with the documented rules A1-A13, so every item was implemented.

| # | Item | Status |
|---|------|--------|
| 1 | Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. The field was already in the delivered `alarm.h`. `alarm.c` now reads it. `alarm_init` already copies the whole config, and `point_t` still fits in `alarm_t` (the `_Static_assert` holds). |
| 2 | HIGH/LOW -> NORMAL uses `time_delay_normal_s`; NORMAL -> HIGH/LOW and HIGH <-> LOW keep `time_delay_s` | Implemented. The new helper `pending_delay()` in `alarm.c` picks the delay and `eval_timer()` uses it. This matches the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6): transitions to NORMAL use pTimeDelayNormal and every other transition uses pTimeDelay. The A5a timer semantics are unchanged: when the countdown starts or restarts, cancellation on "no target", `>=` comparison, and a delay of 0 meaning immediate. |
| 3 | `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME` in `alarm.c`). This matches BACnet, where pTimeDelayNormal = pTimeDelay when Time_Delay_Normal is absent. |

Other changes made to keep things consistent:
- `alarm.c`: I updated the contract summary and the A5/A5a comments at `eval_timer`
  to name both delays.
- `acceptance.vec`: I added 4 scenarios (7/7 pass, also under ASan/UBSan):
  - `return-to-normal-uses-delay-normal`: a return to normal waits the longer `delay_normal`.
  - `offnormal-and-high-low-keep-time-delay`: with `delay_normal=0`, NORMAL->LOW and
    LOW->HIGH still wait `delay`, and the return to NORMAL is immediate.
  - `delay-normal-all-ones-means-same-as-delay`: an explicit 4294967295 acts as `delay`.
  - `fault-recovery-stays-immediate`: see the second point below.
- `driver.c`, `run_vectors.py` and `Makefile`: not modified.

Open points:
- **SPEC.md** is referenced by `alarm.c` but is not in this directory, so I could not
  update it. Its A5 text (and the A5a wording "time_delay_s") should be changed to say
  "`time_delay_normal_s` for returns to NORMAL". I changed the A5a comment wording
  (a [POL] rule) only to name the new delay, not its behaviour. I took the CR, which comes
  from the product owner, as the sign-off for that.
- **FAULT -> NORMAL recovery** stays immediate and ignores `time_delay_normal_s`.
  The CR only covers HIGH/LOW -> NORMAL, and A6a [POL] requires immediate recovery.
  BACnet also does not delay fault transitions. Please confirm this reading.
- **Sentinel vs. a real value**: a configured Time_Delay_Normal of exactly 4294967295 s
  cannot be told apart from "absent". The BACnet object layer should map a missing
  Time_Delay_Normal property to 0xFFFFFFFF. It should also reject a write of 4294967295
  (e.g. VALUE_OUT_OF_RANGE) or treat it as absent. That layer is outside this module.
- The delay is chosen when the timer is evaluated, from the target of the pending
  transition. Maintenance mode (A10), escalation (A9) and acknowledgement (A8) are
  unaffected: a return to normal during maintenance also waits `time_delay_normal_s`,
  it just produces no notification.

## CR-202

RAM budget: `alarm_t` shrinks to 32 bytes and `alarm_init` keeps a pointer to the configuration.
No item conflicts with the BACnet standard or with the documented rules A1-A13, so every item was
implemented. No [POL] rule or its code was changed.

| # | Item | Status |
|---|------|--------|
| 1 | `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Implemented. The new `alarm.h` was already in place and I did not change it. The private `point_t` in `alarm.c` now holds a config pointer instead of a 24-byte copy. Its members are ordered largest first: 28 bytes with 32-bit pointers (checked by compiling `alarm.c` for `thumbv6m-none-eabi`/Cortex-M0) and exactly 32 bytes with 64-bit pointers (host build). A second `_Static_assert` now also checks alignment. All per-point state (A1-A13) is kept, and no field was dropped or narrowed. |
| 2 | `alarm_init` keeps a pointer, caller guarantees the `alarm_cfg_t` outlives the point, configs are shared | Implemented. `alarm_init` stores `cfg` and does not read it. `target()`, `pending_delay()` and `check_escalate()` read the configuration through the pointer each time it is used, so they never use a cached copy. Sharing one config between points is safe because the module never writes to it (`const`). |
| 3 | External behavior must not change | Implemented, with one caveat below. `make` builds cleanly with `-Wall -Wextra`. `acceptance.vec` passes 7/7, also under ASan/UBSan. I added no vectors: the change adds no behavior for a caller that does not modify a config while it is in use. |

Other changes made to keep things consistent:
- `alarm.c`: I updated the file header, the `point_t` comments and the `alarm_init` comment. They
  said "Copy of the configuration" / "the configuration is copied" and now describe the pointer.
- `alarm.h`, `driver.c`, `run_vectors.py`, `Makefile`, `acceptance.vec`: not modified.
  `driver.c` already keeps its `alarm_cfg_t` in static storage, so it outlives the point.

Open points:
- **Changing a config in place is now visible to live points (caveat on item 3).** With the copy,
  editing an `alarm_cfg_t` after `alarm_init` had no effect on existing points. Now the edit
  applies to every point that shares that config, starting with its next call:
  - Limits and deadband apply at the next `alarm_sample`. A5a: tick, ack and maintenance calls
    do not recompute the pending target.
  - `time_delay_s`, `time_delay_normal_s` and `escalate_after_s` apply at the next timer or
    escalation check. They are measured from the `pending_start` and `alarm_time` already
    recorded.
  - Example: with `delay=30`, a sample above the limit at t=10, the config then edited to
    `delay=5`, and a sample at t=20. This now gives TO_HIGH@20. It used to stay NORMAL until t=40.

  The CR only requires that the config outlives the point, not that it stays unchanged. Please
  decide which contract applies: configs are immutable while points use them, or an in-place
  change takes effect immediately as described above. Once decided, write it into `alarm.h`
  (I did not edit the header the lead delivered). Also, `alarm_cfg_t` must not be changed
  concurrently with a call on a point that uses it (for example from an ISR), because a call
  could then see a half-updated config.
- **BACnet object layer.** In BACnet, High_Limit, Low_Limit, Deadband, Time_Delay and
  Time_Delay_Normal belong to each object, and each can be written on its own. If objects share an
  `alarm_cfg_t`, a WriteProperty to one object must not change the other objects. The object layer
  (outside this module) must first give the written object its own config. Its RAM for configs
  counts toward the budget, unless the configs are fixed and kept in flash.
- **RAM arithmetic.** 512 points × `sizeof(alarm_t)` = 512 × 32 B = 16,384 B, which is all of the
  16 KB of RAM. Nothing would be left for stacks, configs, buffers or the BACnet stack. On the M0
  `point_t` needs only 28 bytes, so 4 bytes per point (2 KB in total) are padding caused by
  `uint64_t storage[4]`. Please check the budget. One option: size `alarm_t` from the target's
  pointer size (for example `uint32_t storage[7]` on 32-bit targets), which would need a change
  to `alarm.h`.
- **SPEC.md** is still not in this directory, so I could not check it. If it says that
  `alarm_init` copies the configuration, it needs the same update as `alarm.h`.
