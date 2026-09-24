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
