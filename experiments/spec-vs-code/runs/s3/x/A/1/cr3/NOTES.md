## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Items:

1. **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: done. It was already in the
   supplied `alarm.h`, and I left that file unchanged. `alarm_init` copies the whole config, so
   the new field is stored per point. `point_t` still fits in `alarm_t`, and the
   `_Static_assert` still holds.
2. **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented. The new
   `delay_for()` in `alarm.c` picks the delay for the pending transition, and `eval_timer()`
   uses it.
3. **All other delayed transitions (NORMAL -> HIGH/LOW, HIGH <-> LOW) keep `time_delay_s`**:
   implemented.
4. **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented as the private constant
   `DELAY_NORMAL_SAME` in `alarm.c`. The driver's default is also 4294967295, so
   configurations that do not set `delay_normal` behave exactly as before.

No conflict with the BACnet standard. This matches the OUT_OF_RANGE event algorithm
(135, clause 13.3.6). pTimeDelayNormal governs HIGH_LIMIT/LOW_LIMIT -> NORMAL, pTimeDelay
governs the transitions into HIGH_LIMIT or LOW_LIMIT (including HIGH <-> LOW), and when
Time_Delay_Normal is absent Time_Delay is used. The sentinel stands for the "absent" case.

Tests: I added five blocks tagged `cr201` to `acceptance.vec`. They cover a shorter normal
delay, a longer normal delay (from LOW), HIGH <-> LOW still using `time_delay_s` when
`delay_normal=0`, FAULT -> NORMAL staying immediate, and the explicit sentinel value. All 8
blocks pass (`make && python3 run_vectors.py acceptance.vec`).

Open questions:

- FAULT -> NORMAL stays immediate and does not use either delay. It is not a HIGH/LOW ->
  NORMAL transition, and BACnet clears faults without a time delay. Please confirm this is
  what the product owner wants.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s (about 136 years) cannot
  be configured. I think this does not matter in practice.
- This behavior already existed and I did not change it. When the point is in HIGH and the
  value drops below `low_limit`, the module times only the transition to LOW. If the value
  later comes back into the normal band, the NORMAL timer starts at that moment. A strict
  reading of 13.3.6(e)/(h) says the "below high_limit - deadband" condition was already true
  the whole time. With a `time_delay_normal_s` shorter than `time_delay_s`, the standard
  could therefore allow a return to NORMAL earlier than this module does. Separate delays
  make this edge case easier to notice. If exact conformance is required, it should be
  handled in its own change request.

## CR-202

RAM budget: 512 points on a Cortex-M0 with 16 KB of RAM.

Items:

1. **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: done. The supplied `alarm.h`
   was already updated, and I left it unchanged. `point_t` in `alarm.c` is now 28 bytes on a
   32-bit target (I checked with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0`) and
   32 bytes on the 64-bit build host. The existing `_Static_assert` on the size holds. I added
   a second `_Static_assert` checking that `point_t` needs no stricter alignment than `alarm_t`.
   I also removed `has_value`, a field that was written but never read.
2. **`alarm_init` keeps a pointer to the configuration instead of copying it; the caller keeps
   the `alarm_cfg_t` alive and may share it between points**: implemented. `point_t.cfg` is now
   `const alarm_cfg_t *`. The module only reads the configuration and never writes it.
   My CR-201 note says "`alarm_init` copies the whole config". That statement no longer holds.
3. **External behavior must not change**: holds whenever the configuration is not modified
   after `alarm_init`. Evidence:
   - All acceptance vectors pass (`make && python3 run_vectors.py acceptance.vec`, 9/9).
   - They also pass under ASan/UBSan.
   - I ran a randomized comparison against the pre-change code, built with a larger `alarm_t`.
     About 475k commands produced identical output. Each run used random configs, samples,
     NaN/inf, faults, ticks, acks, maintenance, time going backwards and re-initialisation.
   - I added one vector block, `cr202-reinit-picks-up-new-config`. It reconfigures and
     re-initialises a point.

No conflict with the BACnet standard or with the documented requirements. The change is
internal. The new ownership rule is already documented in `alarm.h`.

Open questions:

- **Changing a configuration after `alarm_init` now affects live points.** This is the one
  observable difference, and it follows directly from keeping a pointer. Before this change a
  point kept a snapshot until the next `alarm_init`. Now new limits, deadband or delays apply on
  the point's next call, and they apply to every point that shares that configuration. That
  call also works with an already-running pending timer. The randomized comparison confirms
  the difference: when the test driver's `cfg` is rewritten without a new `init`, the results
  differ. Two things need care:
  - A BACnet write to one object's High_Limit, Low_Limit, Deadband, Time_Delay or
    Time_Delay_Normal must not change a configuration that other points share. The caller has
    to copy on write, or call `alarm_init` again, which resets the point's state.
  - A configuration must not be updated from an interrupt or another task while alarm
    functions run. The M0 cannot update several fields atomically, so a point could see a torn
    configuration.

  `alarm.h` only says "keeps the configuration alive". It does not say whether a configuration
  may change while points use it. Please say which it is: forbidden, or allowed with immediate
  effect.
- **The RAM figures in the CR leave no RAM for anything else.** 512 x 32 bytes = 16,384 bytes,
  which is all 16 KB. The shared configurations (24 bytes each), stack, BACnet stack and
  notification buffers still need RAM. On the M0 only 28 of each point's 32 bytes are used,
  but `uint64_t storage[4]` still takes 32 bytes and forces 8-byte alignment. If 16 KB is the
  whole device, this budget cannot work. Either "16 KB" means a part of RAM set aside for the
  points, or `alarm_t` must get smaller still. For example, `uint32_t storage[7]` (28 bytes)
  would save 2 KB. I did not change `alarm.h` because the CR says it is already in place.
- There is no NULL check on `cfg`, the same as before. A NULL `cfg` used to crash inside
  `alarm_init`. It now crashes on the first later call.

## CR-203

Collected field requests.

Items:

1. **Add the `EV_ACKED` notification (product owner)**: implemented. `EV_ACKED = 5` was
   already in the supplied `alarm.h`, and I left that file unchanged. When `alarm_ack` clears
   an unacknowledged alarm, it now emits ACKED with `time = now` and `value` = the most recent
   sample value (`last_value`). In maintenance mode the alarm is still acknowledged, but no
   ACKED is emitted. An ack with nothing unacknowledged emits nothing, so a second ack is
   silent. No new state was needed, so `point_t` and its size are unchanged. The updated
   `high-alarm-ack-and-return` block now passes. No conflict with BACnet: this matches the
   ACK_NOTIFICATION that BACnet sends when an alarm is acknowledged.
2. **Return to normal clears the unacknowledged status and cancels escalation (operators,
   Nord)**: **not implemented, because it conflicts with the BACnet standard.** In BACnet the
   acknowledgement state of an alarm is kept separately for each transition (the
   `Acked_Transitions` property has TO-OFFNORMAL, TO-FAULT and TO-NORMAL bits, 135 clause
   12 / 13.2). The TO-OFFNORMAL bit is set only when the alarm is acknowledged
   (AcknowledgeAlarm), or when no acknowledgement is required for it. A later return to normal
   is a separate TO-NORMAL transition, and it does not acknowledge the earlier TO-OFFNORMAL
   one. If a return to normal cleared `unacked`, alarms that nobody acknowledged would be
   reported as acknowledged. Operators would also never see those alarms go by, and the ACKED
   notification from item 1 could never be sent for them. The behavior is unchanged:
   `unacked` is cleared only by `alarm_ack`. I added a comment in `alarm.c` and the vector
   `cr203-return-to-normal-does-not-acknowledge` to record this.
   - Escalation itself is a product feature, not a BACnet one. Stopping escalation once the
     point is back in NORMAL, while the alarm stays unacknowledged, would not conflict with
     BACnet. The operators asked for both parts as one request, though, and changing the
     escalation rule is a product decision. It belongs to the product owner, in its own CR.
     Until then, operators at Nord can stop the escalation by acknowledging the alarm that
     returned to normal.
3. **Make the limits inclusive, `>= high_limit` and `<= low_limit` (operators, Nord)**:
   **not implemented, because it conflicts with the BACnet standard.** The OUT_OF_RANGE event
   algorithm (135, clause 13.3.6) uses strict comparisons. It goes to HIGH_LIMIT when
   pMonitoredValue is "greater than pHighLimit", to LOW_LIMIT when it is "less than
   pLowLimit", and back to NORMAL at "less than (pHighLimit - pDeadband)" or "greater than
   (pLowLimit + pDeadband)". A value exactly at the limit is therefore normal, and the module
   already behaves that way (see CR-201). Behavior is unchanged. I added a comment above
   `target()` in `alarm.c` and the vector `cr203-limits-are-exclusive` to record this. If Nord
   wants an alarm at 30.0, they can configure a slightly lower `high_limit` (for example
   29.9), or a slightly higher `low_limit`.

Tests: I added four blocks tagged `cr203` to `acceptance.vec`. They cover ACKED only when an
alarm is unacknowledged, with the latest sample value (not the alarm value), a silent ack in
maintenance mode, the rejected item 2, and the rejected item 3. All 13 blocks pass
(`make && python3 run_vectors.py acceptance.vec`). They also pass under ASan/UBSan.
`driver.c`, `run_vectors.py` and `alarm.h` are unchanged.

Open questions:

- **An ack during maintenance is never announced.** This follows the CR literally. The alarm
  is acknowledged silently, and nothing is sent when maintenance ends. If the front-end saw
  the TO_HIGH/TO_LOW before maintenance started, it will keep showing the alarm as
  unacknowledged. Should maintenance-off emit a pending ACKED, or should ack be ignored during
  maintenance?
- **An ack can acknowledge an alarm raised by that same call.** `alarm_ack` first runs the
  pending timer. If that raises the alarm, the ack clears it at once, and the output is
  `TO_HIGH@t ACKED@t`. The existing code already acknowledged in this case, silently, and I
  kept that. An operator cannot have seen that alarm yet. Please confirm whether ack should
  only apply to alarms that were already notified.
- **The ACKED `value` can be NaN.** It is the most recent sample, as the CR says, not the
  value that raised the alarm. If the point is in FAULT because of a NaN sample, the ACKED
  value is `nan`.
- An alarm that is still unacknowledged is acknowledged, with ACKED emitted, even when the
  point is now in NORMAL or FAULT. This follows from BACnet, as item 2 explains.
- The module tracks only the acknowledgement of HIGH/LOW (TO-OFFNORMAL). TO-FAULT and
  TO-NORMAL transitions never need an acknowledgement here. This is unchanged and was not in
  scope.
