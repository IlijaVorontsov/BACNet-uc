## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Conflict check: I found no conflict with the BACnet standard. In the OUT_OF_RANGE
event algorithm (ASHRAE 135, clause 13.3.6), HIGH_LIMIT/LOW_LIMIT -> NORMAL waits
pTimeDelayNormal. NORMAL -> HIGH_LIMIT/LOW_LIMIT and HIGH_LIMIT <-> LOW_LIMIT wait
pTimeDelay. When Time_Delay_Normal is absent, Time_Delay is used. The CR asks for
the same thing. This directory has no written requirements document other than the
comments in `alarm.h` and the requirement tags in the vectors, and nothing there
conflicts with the CR.

Items:

1. Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`: **done**. It was already in
   the supplied `alarm.h`. I did not change the header. `point_t` in `alarm.c` still
   fits in `alarm_t`; the `_Static_assert` still passes.
2. HIGH/LOW -> NORMAL uses `time_delay_normal_s`: **implemented**. The new
   `pending_delay()` in `alarm.c` picks the delay from the pending target.
   `eval_timer()` uses it for both `alarm_sample()` and `alarm_tick()`/`ack`/`maint`.
3. NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s`:
   **implemented**. A pending return to NORMAL that is replaced by a pending
   HIGH/LOW restarts the timer with `time_delay_s`, and the reverse case restarts
   it with the normal delay. This matches the existing restart-on-new-target rule.
4. `0xFFFFFFFF` means "same as `time_delay_s`": **implemented**
   (`TIME_DELAY_NORMAL_UNSET`).

Tests: I added `tests/unit.vec` x064-x074 (tag `CR201`) and the acceptance
scenario `high-alarm-returns-after-normal-delay`. I did not modify
`driver.c` or `run_vectors.py`. All 56 blocks pass. 9 of the new
blocks fail against the old delay logic. x069, x071 and x073 are regression guards
that pass either way.

Unsure / worth reviewing:
- FAULT -> NORMAL stays immediate. `time_delay_normal_s` does not apply to it. The
  CR names only HIGH/LOW -> NORMAL, and in BACnet a fault clear is not delayed by
  Time_Delay_Normal. x073 pins this.
- Zero-initialised configs: an integrator who builds `alarm_cfg_t` with `= {0}`,
  `memset` or designated initializers and does not set the new field gets
  `time_delay_normal_s = 0`. That makes the return to normal immediate, not
  "same as `time_delay_s`". This is a silent behaviour change for existing callers,
  and they should set the field to `0xFFFFFFFF` explicitly. The test driver already
  does this.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s cannot be
  configured. The largest usable value is 4294967294 (x072).
- The existing rule is unchanged: the config is copied at `alarm_init`, so a
  change to either delay needs a re-init. (Superseded by CR-202: the config is no longer
  copied.)
- The requirement tags (A3, A4, A5, A5a, A6, A10) on the new vectors are my best
  guess from the existing vectors. No requirements list was available to confirm
  them.

## CR-202

RAM budget: `alarm_t` shrinks to 32 bytes, and `alarm_init` keeps a pointer to the
configuration instead of copying it.

Conflict check: I found no conflict with the BACnet standard or with the documented
requirements (the comments in `alarm.h` and the requirement tags in the vectors).
BACnet does not require the implementation to snapshot the alarm limits and delays
at initialisation. The updated `alarm.h` already documents the new contract, and I
did not change the header.

Items:

1. `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`): **implemented**. The
   header was already in place. In `alarm.c`, `point_t` now holds
   `const alarm_cfg_t *cfg` in place of the 24-byte copy. It is 32 bytes on a 64-bit
   host and 28 bytes on Cortex-M0. I checked the M0 layout with
   `clang --target=thumbv6m-none-eabi`. The existing `_Static_assert` on size passes,
   and I added one on alignment.
2. `alarm_init` keeps a pointer and does not copy: **implemented**. All reads of
   the configuration go through the pointer.
3. The caller keeps the `alarm_cfg_t` alive, and configurations are shared between
   points: **nothing to implement**. This is a caller contract, and it is documented
   in `alarm.h`. The module does not check it, and a NULL `cfg` is not detected.
4. External behaviour must not change: **met for callers that keep to the
   contract and do not modify a configuration after `alarm_init`**. All 56 blocks
   pass (`tests/unit.vec` and `acceptance.vec`), including builds with gcc, clang
   and ASan/UBSan. I added no vectors because there is no new behaviour to pin. The
   driver has one point and one config, so it cannot exercise sharing.

Unsure / worth reviewing:
- **The RAM budget does not close.** 512 points x 32 bytes = 16384 bytes, which is
  all 16 KB of RAM. Nothing is left for the stack, `.data`/`.bss`, the notification
  buffers or the rest of the firmware, and the shared `alarm_cfg_t` objects would
  also have to live in flash as `const`. The CR is implemented as written, but I
  do not think this configuration can run. On a 32-bit target `point_t` could be
  packed to 24 bytes by using bitfields for the state and flag bytes, which gives
  512 x 24 = 12 KB. That would need a target-dependent `alarm_t` size in
  `alarm.h`, so it is a firmware-lead decision and I did not do it.
- **Behaviour changes for callers that modify or reuse a configuration after init.**
  Before this CR, `alarm_init` took a snapshot, so later writes to the caller's
  struct had no effect until the next re-init (see the CR-201 note above). Now:
  - A change to a live `alarm_cfg_t` applies at the point's next call, to every
    point that shares it. This includes timers that are already running: for example,
    lowering `time_delay_s` can fire a pending transition on the next
    `alarm_tick`. Such a change is not atomic with respect to a concurrent
    `alarm_*` call, such as an ISR or task writing the config while another context
    evaluates points.
  - Integration code that fills one temporary or stack `alarm_cfg_t` and passes
    it to `alarm_init` for several points is now wrong. Every point ends up with the
    last configuration, or with a dangling pointer. Such call sites must be audited.
    No vector covers this because the driver keeps one static config and never
    re-issues `cfg` after `init`.
- The cast from `alarm_t` to `point_t` is not new, and it still relies on the
  compiler tolerating type punning through `alarm_t`. It now also stores a pointer
  in that storage. This works with gcc and clang at `-O2`, but it is not strictly
  conforming C.

## CR-203

Collected field requests. I implemented item 1. I did not implement items 2 and 3
because each conflicts with the BACnet standard and with the documented
requirements (the requirement tags in the vectors). `alarm.h` already contained
`EV_ACKED`, and I did not change the header.

Items:

1. `EV_ACKED` on acknowledgment (product owner): **implemented**. When `alarm_ack`
   clears an unacknowledged alarm, it emits ACKED with `time = now` and
   `value = last_value`, which is the most recent sample. In maintenance mode the ack
   still clears the unacknowledged status, but no ACKED is emitted. This matches
   BACnet: an AcknowledgeAlarm (clause 13.5) produces an event notification with
   Notify_Type ACK_NOTIFICATION. An ack with nothing unacknowledged, or an ack
   rejected because `now` went backwards, emits nothing.
2. Return to normal clears the unacknowledged status and cancels escalation
   (operators, Nord): **not implemented, conflict**.
   - BACnet: a return to normal does not acknowledge an alarm. The
     Acked_Transitions TO_OFFNORMAL flag is cleared when the off-normal event
     occurs. It is set again only when an acknowledgment arrives, or when no
     acknowledgment is required. A TO_NORMAL transition does not set it, so an
     alarm that returned to normal without being acknowledged stays unacknowledged.
     GetEventInformation and GetAlarmSummary still report it. Clearing `unacked`
     on TO_NORMAL would break this.
   - Documented requirements: A8/A9 require an unacknowledged alarm to escalate
     after `escalate_after_s` even if it has returned to normal. x038 (`NORMAL
     ESCALATE@110`, tagged `CR3clear`) and x063 (`NORMAL ESCALATE@400`) pin this.
   - What operators can do: acknowledge the alarm. The ack stops the escalation
     (x041), and it now also produces an ACKED notification. If escalation should
     apply only while the point is still in alarm, the product owner must change
     requirement A9. That change is separate from the unacknowledged status, and
     the unacknowledged status has to stay as it is.
3. Inclusive limits, `>= high_limit` / `<= low_limit` (operators, Nord): **not
   implemented, conflict**. The BACnet OUT_OF_RANGE algorithm (clause 13.3.6) goes
   to HIGH_LIMIT only when pMonitoredValue is *greater than* pHighLimit, and to
   LOW_LIMIT only when it is *less than* pLowLimit. Requirement A2 pins this
   boundary: x001 and x002 (tagged `CR3incl`) expect no alarm at exactly 30.0 and
   exactly 10.0. What operators can do: set the limit to the last acceptable
   value. For example, if 30.0 should alarm, use `high_limit` 29.9 (or whatever
   resolution the sensor has).

Tests (`driver.c` and `run_vectors.py` are unchanged):
- I updated the expected ack lines for item 1 in x021, x033, x036, x039, x041, x043,
  x060 and x063. Only the ack line changed in each. In x041 (`CR3clear`) the
  no-escalation-after-ack line is unchanged.
- The block `high-alarm-returns-after-normal-delay` in `acceptance.vec` was not
  covered by the CR's acceptance update. I added it in CR-201. Its `ack 40` now
  expects `HIGH ACKED@40:35.000`.
- I added x075 to x079 (tag `CR203`): the value is the latest sample and not the
  alarm value; the ACKED is suppressed and not deferred in maintenance; ack after an
  escalation, followed by a new alarm cycle; ack in FAULT after a NaN sample
  (`ACKED@20:nan`); ack rejected because time went backwards.
- 61/61 blocks pass with gcc, with ASan/UBSan and with clang. 14 blocks fail
  against the pre-CR ack logic. x076 is a regression guard that passes either way.
- I left the vectors for items 2 and 3 (x001, x002, x038, x041, x063) unchanged,
  apart from the ACKED lines.

Unsure / worth reviewing:
- **Maintenance:** an ack during maintenance clears the alarm silently. No ACKED
  is sent then, and none is sent at `maint off` either. Deferred state changes
  work differently: they are re-notified at `maint off`. So the front-end is never
  told about such an ack. This is how I read "not emitted in maintenance mode", but
  the product owner should confirm that they do not want it deferred.
- **Ack in the same call that raises an alarm:** if a pending alarm's delay has
  expired when `alarm_ack` runs, the ack raises it and then acknowledges it at
  once. The output is `TO_HIGH` followed by `ACKED` in the same call (x021). The
  auto-acknowledgment was already there before this CR. It is now visible, and it
  acknowledges an alarm that the operator could not have seen yet.
- **Value in FAULT:** "most recent sample value" can be a faulted reading, for
  example 0 from `sf 10 0` (x033) or NaN (x078). ESCALATE already uses this value.
- **What can be acknowledged:** only HIGH/LOW alarms are acknowledgeable, and
  there is one flag for both. A FAULT is never acknowledged and emits no ACKED.
  HIGH -> LOW before an ack needs only one ACKED. BACnet acknowledges TO_FAULT and
  each transition type separately, but that is a pre-existing difference and
  outside this CR.
- **Buffer:** an ack emits at most 2 notifications (one transition and ACKED),
  because escalation cannot fire once `unacked` is cleared. This is within
  `ALARM_MAX_NOTES`.
