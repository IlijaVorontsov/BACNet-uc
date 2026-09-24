## CR-201

Separate return-to-normal delay (Time_Delay_Normal). **Implemented.** I found no
conflict with SPEC.md or with BACnet. In ASHRAE 135 OUT_OF_RANGE (13.3.6), pTimeDelayNormal
governs HIGH_LIMIT/LOW_LIMIT -> NORMAL and pTimeDelay governs every other transition. When
Time_Delay_Normal is absent, Time_Delay applies. The CR matches this.

Item by item:
- `uint32_t time_delay_normal_s` in `alarm_cfg_t`: **done**. It was already in
  `alarm.h`, and `alarm_init` copies the whole cfg, so no change was needed there.
- HIGH -> NORMAL and LOW -> NORMAL use `time_delay_normal_s`: **implemented** in `alarm.c`
  (`pending_delay()`, used by `eval_timer()`). The pending target alone picks the delay.
  NORMAL can only be pending from HIGH or LOW.
- NORMAL -> HIGH/LOW and HIGH <-> LOW keep `time_delay_s`: **implemented**.
- `0xFFFFFFFF` = "same as `time_delay_s`": **implemented**. The sentinel is resolved
  each time the timer is evaluated. Every other value, including 0 and 0xFFFFFFFE, is
  used literally.
- Other artifacts:
  - SPEC.md is now v1.1. A5 describes the two delays and the sentinel. A5a says "the
    delay of the pending target" instead of `time_delay_s`.
  - tests/unit.vec gains blocks c201a to c201k, tagged CR201. c201e and c201i are
    regression guards and also pass on the old code. The other nine fail on the old code.
  - acceptance.vec is unchanged and still passes. It does not set `delay_normal`, so the
    default sentinel applies.
  - All 55 vectors pass.

Uncertain / decisions to confirm:
- FAULT -> NORMAL recovery stays immediate (A6a [POL]). `time_delay_normal_s` does not
  delay it. The CR only names HIGH/LOW -> NORMAL, and in BACnet fault recovery is not
  subject to the event time delays.
- The pending start time is still kept only while the *target* is unchanged. For
  example, pending NORMAL followed by a low-condition sample restarts the timer as
  pending LOW with `time_delay_s`, and a sample inside the deadband cancels a pending
  NORMAL. This follows A5a as before.
- Because 0xFFFFFFFF is the sentinel, you cannot configure a return-to-normal delay of
  exactly 2^32-1 s. I don't think this matters in practice.
- The sentinel is not re-validated against BACnet property encoding. This module does
  not expose properties; the object layer must map an absent Time_Delay_Normal to
  0xFFFFFFFF.

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by pointer. **Implemented.** I found no
conflict with SPEC.md or with BACnet. The module's observable behavior (states,
notifications, timing) is unchanged.

Item by item:
- `alarm_t` is 32 bytes (`uint64_t storage[4]`, the `alarm.h` already in place):
  **implemented**. I reordered `point_t` in `alarm.c`: the cfg pointer comes first, then
  the four 32-bit fields, then the byte-sized fields. Size: 32 B on the 64-bit host
  build, 28 B on Cortex-M0 (checked with clang `--target=thumbv6m-none-eabi`).
  `_Static_assert`s check both the size and the alignment against `alarm_t`. I also
  removed `has_value`, which was written but never read. A13 still holds because
  nothing is pending before the first sample.
- `alarm_init` keeps a pointer and does not copy the configuration: **implemented**.
  `p->cfg = cfg`, and all reads go through the pointer.
- The caller guarantees that the cfg outlives the point, and configurations are shared:
  **documented**. The contract was already in the `alarm.h` comment. SPEC.md is now
  v1.2, with a "Memory (CR-202)" paragraph under the header.
- External behavior must not change: **met**. All 55 vectors pass (acceptance.vec 3,
  tests/unit.vec 52), also under ASan/UBSan. No vectors were added or changed. The
  driver has one point and a static cfg, so nothing observable changed that a vector
  could pin.

Uncertain / decisions to confirm:
- **RAM arithmetic.** 512 points x 32 B = 16 384 B, which is the whole 16 KB. Nothing
  is left for the shared cfgs, stack, notification buffers or the BACnet stack. On M0
  the point needs only 28 B. The other 4 B per point (2 KB in total) are padding forced
  by `uint64_t storage[4]` in the header. I did not change the header because the CR
  fixes it. If the budget is really that tight, options include `uint32_t storage[7]`
  (28 B, 4-byte alignment), or about 24 B with a 16-bit cfg index and packed flags.
- **Config changes now take effect immediately.** Before, changing a cfg after
  `alarm_init` had no effect. Now a write to a cfg immediately affects every point that
  shares it, including a running pending timer (the delay is read at each evaluation)
  and escalation. That is fine only if cfgs are effectively read-only while points use
  them. In BACnet, High_Limit, Low_Limit, Deadband, Time_Delay and Time_Delay_Normal are
  per-object properties. A WriteProperty to one object must not change the others, so
  the object layer must not modify a shared cfg in place. There is no API to point an
  existing point at a different cfg. `alarm_init` would do it, but it resets the point
  and would drop an unacknowledged episode (A8, SP-3). If per-object writes are needed,
  I suggest a separate CR for an `alarm_set_cfg()`.
- **Concurrency.** If a cfg can be written from another context (a task or ISR) while a
  point is evaluated, a multi-field update (for example limit plus deadband) can be read
  half-applied. The copy at init used to hide this. The caller must serialize.
- `cfg == NULL` is still not checked, as before.
