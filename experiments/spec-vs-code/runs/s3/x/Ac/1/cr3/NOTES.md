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

## CR-203

Collected field requests. Item 1 is implemented. Items 2 and 3 were **not implemented** because
they conflict with the documented rules (and, for item 3, with the BACnet standard).

| # | Item | Status |
|---|------|--------|
| 1 | `alarm_ack` emits `EV_ACKED` (time = now, value = most recent sample), not in maintenance | Implemented. When `alarm_ack` clears an unacknowledged episode it now emits `ACKED@now:<last sample value>`. An ack with nothing unacknowledged still does nothing and emits nothing. In maintenance the ack still clears the status but emits no ACKED (A10). The order within the call is: the timer transition (if one is due), then ACKED, then the escalation check. The escalation check can never fire after an ack because the ack has just cleared `unacked`, so an ack call writes at most 2 notifications (limit is `ALARM_MAX_NOTES` = 4). `alarm.h` already had `EV_ACKED = 5` and I did not change it. A8 is a [POL] rule, and its text "clear the unacknowledged status, with no notification" changes. I took this item, which comes from the product owner, as the sign-off. ACKED does not conflict with BACnet: an AcknowledgeAlarm there also produces an ACK_NOTIFICATION. |
| 2 | Return to normal clears "unacknowledged" and cancels escalation | **Not implemented: conflicts with A8 [POL].** A8 says: "TO_HIGH/TO_LOW open an alarm episode that must be acknowledged even if the value returns to normal", and escalation (A9) stays armed until the ack. SPEC.md gives an explicit rationale for this: safety policy SP-3, "every excursion must be seen by a person, even a transient one" (freezer/cold-room incident 2023). The request asks for exactly what SP-3 rules out. It comes from operators, not the product owner, so there is no sign-off to change a [POL] rule. BACnet takes the same view: a return to normal does not acknowledge the TO_OFFNORMAL transition, and only AcknowledgeAlarm sets its Acked_Transitions bit. What operators can do: acknowledge the alarm after it returns to normal. This stops the escalation, and the front-end now sees an ACKED (item 1). If they want escalation relaxed after short excursions, the product owner has to decide that (and whether SP-3 still applies). A per-site `escalate_after_s` (0 = off) is already available. |
| 3 | Inclusive limits (`>= high_limit`, `<= low_limit`) | **Not implemented: conflicts with the BACnet standard and A2 [STD].** The BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6) uses "greater than pHighLimit" / "less than pLowLimit". A2 implements that with strict limits, and our BTL listing for intrinsic reporting depends on it. What the site can do: set the limit one resolution step inside the threshold they want. For example, `high_limit = 29.9` (or the float just below 30.0) raises the alarm at 30.0; do the same for `low_limit`. The deadband return threshold (`high_limit - deadband`) moves by that same small step. |

Other changes made to keep things consistent:
- `alarm.c`:
  - `alarm_ack` implements item 1.
  - The contract summary now names ACKED and the order "transitions, then ACKED, then ESCALATE" (A11).
  - The A2 comment at `target()` and the A8 comment at `notify_state()` say that CR-203 items 3
    and 2 were rejected and why, so nobody "fixes" them later without the sign-off.
- `acceptance.vec`:
  - My CR-201 block `return-to-normal-uses-delay-normal` acks an unacknowledged alarm at t=50,
    so it now expects `HIGH ACKED@50:35.000`. The stakeholder update had only changed
    `high-alarm-ack-and-return`.
  - I added 6 scenarios for item 1. All 13 blocks pass, also under ASan/UBSan:
    - `acked-only-when-unacknowledged`: an ack with nothing pending, a repeated ack, and an ack
      after the episode was already acked all emit nothing. The value is the latest sample, not
      the alarm value.
    - `acked-after-return-to-normal-reports-latest-value`: acking after a return to normal gives
      ACKED with the normal value, and there is no later ESCALATE.
    - `acked-follows-transition-in-same-call`: TO_HIGH then ACKED in one ack call.
    - `acked-after-escalation`: an escalated episode can still be acked.
    - `acked-in-fault-reports-nan`: an ack in FAULT after a NaN sample reports `nan`.
    - `ack-in-maintenance-is-silent`: no ACKED, no catch-up, and no ESCALATE afterwards.
- `alarm.h`, `driver.c`, `run_vectors.py`, `Makefile`: not modified.

Open points:
- **SPEC.md** is still not in this directory, so I could not update it. Its A8 text ("with no
  notification") and its A11 ordering need to add ACKED. A7's "every notification" wording should
  make clear that ACKED is a notification but not a transition.
- **Ack during maintenance.** Following the CR, the ack clears the status silently. When
  maintenance ends, the A10 catch-up covers only the state, so the front-end never sees an ACKED
  for that episode and may keep showing it as unacknowledged. Please confirm whether leaving
  maintenance should also send a catch-up ACKED, or whether an ack in maintenance should be
  refused.
- **ACKED value in FAULT.** "Most recent sample value" can be NaN, or the value of a
  `sensor_fault` sample, when the ack arrives in FAULT. I implemented it as written.
- **BACnet mapping.** ACKED does not say which transition was acknowledged. The BACnet object
  layer (outside this module) has to map it to an ACK_NOTIFICATION for the TO_OFFNORMAL
  transition and set that Acked_Transitions bit. Ack source and time stamps are not part of this
  module.
