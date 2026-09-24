## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

**Items and status**

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`.** Implemented. The field was
  already in `alarm.h` as delivered, so I left the header as it was. `point_t` still
  fits in `alarm_t`; the `_Static_assert` still passes.
- **HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s`.** Implemented in `alarm.c`
  (`pending_delay()`, used by `eval_timer()`).
- **NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s`.** Implemented.
- **`0xFFFFFFFF` means "same as `time_delay_s`".** Implemented.
- **Conflict check.** I found no conflict. ASHRAE 135 clause 13.3.6 (OUT_OF_RANGE, 2012
  and later) uses pTimeDelayNormal for HIGH_LIMIT/LOW_LIMIT→NORMAL and pTimeDelay for
  every other transition, including HIGH_LIMIT↔LOW_LIMIT. When Time_Delay_Normal is
  absent, Time_Delay is used. This matches the CR, and the sentinel stands for "property
  absent". A5a [POL] (timer semantics) had to be reworded, but it keeps its meaning, and
  the CR comes from the product owner, who is the sign-off authority for [POL] rules.

**Other artifacts updated**

- `SPEC.md` is now v1.1:
  - A5 lists which delay each transition uses and what the sentinel means.
  - A5a compares against "the delay of the pending transition" instead of
    `time_delay_s`.
  - A6a states that FAULT recovery stays immediate.
- `acceptance.vec` has 4 new blocks (`cr201-*`) covering:
  - a short return delay;
  - a long return delay (checked with `alarm_tick`);
  - HIGH→LOW still using `time_delay_s` while `delay_normal=0`;
  - FAULT→NORMAL recovery staying immediate.

  All 7 blocks pass. The existing 3 blocks are unchanged and rely on the driver's
  default `delay_normal=4294967295`.

**Open points / uncertainties**

- **Existing configs get a delay of 0.** Code that builds an `alarm_cfg_t` without
  setting the new field (`= {0}`, positional or designated initializers, memset) now
  gets `time_delay_normal_s = 0`. That means an immediate return to NORMAL, where before
  it was `time_delay_s`, and nothing warns about it. Every place that builds an
  `alarm_cfg_t` must set the field, usually to `0xFFFFFFFF`. The sentinel cannot be 0,
  because 0 is a legitimate delay ("no delay"). Please audit the integrators.
- **FAULT→NORMAL recovery (A6a [POL]) is still immediate** and uses neither delay. I read
  "transitions from HIGH or LOW back to NORMAL" literally. This also matches BACnet,
  where leaving FAULT is not subject to the event time delays.
- **`0xFFFFFFFF` can no longer be a real Time_Delay_Normal value** (about 136 years). If
  the BACnet object layer exposes a Time_Delay_Normal property, it has to translate the
  sentinel itself: report the property as absent, or report Time_Delay. That layer is
  not part of this module.
- **Unchanged behaviour:**
  - Returning to NORMAL still does not clear the unacknowledged status (A8).
  - A pending target that stays the same keeps its start time.
  - The delay is chosen from the pending target each time the timer is evaluated. The
    configuration is fixed at `alarm_init`, so this is unambiguous.

## CR-202

RAM budget: `alarm_t` shrinks to 32 bytes, and `alarm_init` keeps a pointer to the
configuration.

**Items and status**

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`).** Implemented. `alarm.h`
  already had the new size as delivered. The old `alarm.c` no longer fit: the
  `_Static_assert` failed and the build broke. The internal `point_t` is now 32 bytes on
  the 64-bit host (8-byte pointer) and 28 bytes on Cortex-M0 / 32-bit targets (checked
  with clang for `armv6m-none-eabi` and `i386`). The `_Static_assert` stays in place.
- **`alarm_init` keeps a pointer instead of copying.** Implemented. `point_t.cfg` is now
  `const alarm_cfg_t *`, and every rule reads the limits, delays and escalation time
  through it.
- **External behaviour must not change.** Implemented and checked:
  - All 7 acceptance blocks pass unchanged.
  - I also ran a throwaway differential test against the previous copying
    implementation, built with ASan and UBSan. It covered 4000 random scenarios (about
    94k commands): faults, NaN/±inf, maintenance, acks, backwards time, uint32
    wrap-around and all delay sentinels. The output was byte-identical, with no
    sanitizer findings.
  - No new vectors: nothing new is observable through the driver.
- **Conflict check.** I found no conflict with SPEC.md or with BACnet. The change only
  affects memory layout and the caller contract. No [STD] or [POL] rule changes.

**Other artifacts updated**

- `alarm.h`: only the comment on `alarm_t` changed. It now also says that the caller
  must not modify `*cfg` while a point uses it. The types and prototypes are unchanged.
- `SPEC.md` is now v1.2. New section 6 "Integration" (I1, I2) covers the 32-byte state
  and the configuration lifetime and immutability requirement. Rules A1–A13 are
  unchanged.

**Open points / uncertainties**

- **The RAM budget does not close.** 512 points × 32 B = 16384 B, which is the entire
  16 KB of RAM. That leaves nothing for stack, BACnet stack, buffers or the
  configurations themselves (unless those are `const` in flash). This needs a decision
  from the firmware lead. Options:
  - On Cortex-M0, `point_t` needs only 28 B. Making `alarm_t` 28 bytes
    (e.g. `uint32_t storage[7]`) would save 2 KiB.
  - Packing the state and flag bytes into bit-fields would bring it to 24 B, which is
    12 KiB for 512 points.
  - A 16-bit index into a configuration table instead of a pointer saves 2 B more on the
    host but not on M0 (alignment).
  - Going below 20 B would change behaviour: three 32-bit timestamps, the last value and
    the configuration reference are all needed by A5a, A9 and A12.

  I did not change the header, since the CR fixes it at 32 bytes.
- **"Must not modify" is my addition.** The CR says only that the configuration must
  outlive the point. Before, a caller could change its `alarm_cfg_t` after
  `alarm_init` and the point was unaffected. Now the change would reach every point that
  shares it, mid-episode. Declaring that unsupported is the only way to keep "external
  behaviour must not change". The CR-201 notes assumed the configuration is fixed at
  `alarm_init`; that assumption now depends on the caller. Please confirm, or say if
  live changes are wanted, which would need their own CR and rules.
- **Integrator audit needed.**
  - Any caller that passed a temporary, stack or reused `alarm_cfg_t` to `alarm_init`
    was fine before and now leaves a dangling pointer or aliasing bug.
  - The BACnet object layer must not write High_Limit, Low_Limit, Deadband,
    Time_Delay or Time_Delay_Normal of one object into a *shared* configuration. Each
    object's properties are independent in BACnet, so a write has to give that object
    its own `alarm_cfg_t` (copy-on-write) and re-init the point. The re-init resets
    the point, as it did before.
- **`alarm_init` does not check `cfg == NULL`**, same as before, when it copied from
  `*cfg`. A NULL now faults on the first call instead of in `alarm_init`.

## CR-203

Collected field requests: one item implemented, two declined because they conflict.

**Items and status**

- **1. New notification `EV_ACKED` (product owner): implemented.**
  - `alarm_ack` now emits ACKED (`time = now`, `value` = most recent sample value) when
    it clears an unacknowledged episode.
  - An ack with nothing unacknowledged still does nothing and emits nothing.
  - In maintenance mode the ack still clears the unacknowledged status (A10: the state
    machine keeps running), but ACKED is not emitted.
  - Order within the call: a transition from the timer evaluation first, then ACKED,
    then the escalation check. ESCALATE cannot fire after an ack, so an ack call emits
    at most 2 notifications, within `ALARM_MAX_NOTES`.
  - Conflict check: this changes A8 [POL], which said the ack emits no notification.
    The request comes from the product owner, who is the sign-off authority for [POL]
    rules. There is no BACnet conflict: BACnet also reports acknowledgements to
    recipients (ACK_NOTIFICATION).
  - `alarm.h` already contained `EV_ACKED = 5` as delivered; left unchanged.
- **2. Return to normal clears the unacknowledged status and cancels escalation
  (operators, site Nord): not implemented. It conflicts with the requirements.**
  - A8 [POL] says the opposite in so many words: "Returning to NORMAL does not clear the
    unacknowledged status — the episode still has to be acknowledged and can still
    escalate." Its rationale is safety policy SP-3 (every excursion must be seen by a
    person, even a transient one; freezer/cold-room incident 2023).
  - Only the product owner can sign off a change to a [POL] rule. Because SP-3 is a
    safety policy, whoever owns SP-3 should probably sign off too. The request comes
    from operators.
  - It also goes against BACnet practice. In BACnet the TO_OFFNORMAL transition stays
    unacknowledged (Acked_Transitions) until someone acknowledges it. A later return to
    normal is a separate transition and does not acknowledge the earlier one.
  - What Nord sees is the intended behaviour. Acknowledging the alarm stops the
    escalation. If Nord still wants the change, it needs a product-owner/SP-3 decision
    and a new CR. Vector `cr203-return-to-normal-keeps-unacked` pins the current
    behaviour.
- **3. Inclusive limits `>= high_limit`, `<= low_limit` (operators, site Nord): not
  implemented. It conflicts with the BACnet standard.**
  - A2 [STD] requires strict limits (`value > high_limit`, `value < low_limit`),
    modeled on the ASHRAE 135 clause 13.3.6 OUT_OF_RANGE algorithm, which our BTL
    listing of intrinsic reporting depends on. A [STD] rule cannot be changed on
    request.
  - The behaviour Nord reports (30.0 with `high_limit` 30.0 raises no alarm) is correct.
  - Within the standard, the site can get the effect it wants through configuration.
    For example, set `high_limit` to just below the value that must alarm (e.g. 29.9)
    and `low_limit` just above. That is a site configuration decision; nothing changes
    in the module.
  - Vector `cr203-limits-stay-strict` pins the strict limits and the strict deadband.

**Other artifacts updated**

- `alarm.c`: `alarm_ack` emits ACKED (item 1). No other code changes.
- `SPEC.md` is now v1.3:
  - A8 describes ACKED and when it is not emitted.
  - A10 states that an ack during maintenance is not caught up later.
  - A11 gives the order as transitions, then ACKED, then ESCALATE.
  - A2 and the "return to NORMAL does not clear" sentence of A8 are unchanged.
- `acceptance.vec`: the existing block `high-alarm-ack-and-return` already expected
  ACKED as delivered, and it failed before the change. I added 5 blocks (`cr203-*`):
  - ACKED in the same call as a timer-driven TO_HIGH, and a second ack that is ignored;
  - no ACKED when nothing is unacknowledged, or in maintenance (and no escalation or
    catch-up afterwards);
  - return to normal keeps the episode unacknowledged, it escalates, and a later ack
    emits ACKED with the current in-range value;
  - ACKED in FAULT (value NaN), and an ack with a backwards `now` is ignored;
  - strict limits and deadband.

  All 12 blocks pass, also with an ASan/UBSan build of the driver.
- `alarm.h`, `driver.c`, `run_vectors.py`, `Makefile`: unchanged.

**Open points / uncertainties**

- **Ack during maintenance is silent for good.** Suppose the front-end received
  TO_HIGH before maintenance started and the operator acknowledged during maintenance.
  The front-end never receives an ACKED for that alarm, so it may keep showing it as
  unacknowledged until the next episode. That goes against A10's own rationale
  ("the front-end must end up showing the true state"). I followed the CR ("not emitted
  in maintenance mode") and did not invent a catch-up ACKED. Please decide whether
  maintenance-off should also emit a catch-up ACKED, or whether acks should be refused
  during maintenance.
- **ACKED value can be NaN.** When the point is acknowledged while in FAULT, the most
  recent sample may have been NaN. This follows the CR wording and matches A7. The
  front-end already has to handle NaN for TO_FAULT.
- **Timer transition and ack in one call.** A delayed TO_HIGH/TO_LOW that becomes due
  exactly at the ack call is acknowledged by that same call (the timer is evaluated
  first, per A5a/A9, as before). This is now visible as `TO_HIGH` followed by `ACKED`
  with the same timestamp. Please confirm that this is wanted rather than keeping the
  new episode unacknowledged.
- **Integrators.** Front-end or BACnet-layer code that switches on `alarm_event_t`, or
  indexes a table by it, must handle value 5. ACKED does not say which transition was
  acknowledged. The module has only one episode, so it is always the latest
  TO_HIGH/TO_LOW. A BACnet ACK_NOTIFICATION, however, names the acknowledged
  transition, so the object layer has to track that itself.
