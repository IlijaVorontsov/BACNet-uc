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
