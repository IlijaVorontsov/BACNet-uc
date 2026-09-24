## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: already present in the
  supplied `alarm.h`; no header change needed. `alarm_init` copies it with the rest
  of the configuration.
- **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented. `alarm.c` now
  picks the delay for the pending transition in `pending_delay()`: a pending
  NORMAL transition (which can only arise from HIGH or LOW) uses
  `time_delay_normal_s`.
- **Other delayed transitions (NORMAL -> HIGH/LOW, HIGH <-> LOW) keep `time_delay_s`**:
  implemented. This is also what the BACnet OUT_OF_RANGE event algorithm specifies
  (pTimeDelay for offnormal and HIGH<->LOW transitions, pTimeDelayNormal only
  for transitions to NORMAL).
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. It corresponds to
  BACnet's "Time_Delay_Normal absent -> use Time_Delay".

No item conflicts with the documented requirements or with the BACnet standard, so
every item was implemented.

Other changes:
- `acceptance.vec`: six new blocks cover a longer and a shorter return-to-normal
  delay, `delay_normal=0`, HIGH<->LOW still using `time_delay_s`, the explicit
  sentinel, and FAULT -> NORMAL. The existing blocks leave `delay_normal` at its
  default (the sentinel), so their behavior is unchanged. All 9 blocks pass.

Open points:
- FAULT -> NORMAL (sensor fault clears) is still immediate and ignores both delays.
  The CR only covers HIGH/LOW -> NORMAL, and in BACnet the fault-clear transition
  is not governed by Time_Delay_Normal either. If the product owner meant
  "every transition to NORMAL", that needs a separate CR.
- The sentinel means 4294967295 s (about 136 years) cannot be configured as a real
  Time_Delay_Normal. BACnet allows any Unsigned value, so a BACnet front-end should
  reject that value on write, or map it to the sentinel knowingly.
- As before, the delay is chosen when the timer is checked, based on the pending
  target. If the pending target changes (e.g. HIGH, value first drops into the
  normal band and then below the low limit), the timer restarts and the new
  target's delay applies. That matches the existing restart-on-new-target behavior.

## CR-202

RAM budget: 512 points on a Cortex-M0 with 16 KB of RAM.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. The
  supplied `alarm.h` already had the new size. In `alarm.c`, `point_t` now holds a
  pointer to the configuration instead of a copy. The fields are ordered pointer
  first, then the 32-bit fields, then the byte fields, so there is no internal
  padding. `point_t` is 28 bytes on ILP32 (checked with clang for
  `thumbv6m-none-eabi`/Cortex-M0) and 32 bytes on the LP64 host test build.
  `_Static_assert`s check both the size and the alignment against `alarm_t`. I
  also removed the `has_value` flag: the code set it and never read it.
- **`alarm_init` keeps a pointer instead of copying; the caller keeps the
  `alarm_cfg_t` alive, and configurations may be shared**: implemented.
  `alarm_init` stores `cfg`, and every limit, deadband, delay and escalation lookup
  reads through that pointer. The configuration is only read, never written, so
  sharing it between points is safe. This replaces the CR-201 note that "`alarm_init`
  copies it with the rest of the configuration": `time_delay_normal_s` is now read
  through the pointer like every other field.
- **External behavior must not change**: holds when the configuration is not
  modified after `alarm_init`. `acceptance.vec` passes 9/9. I also ran 6000 random
  scenarios (samples, faults, NaN/inf, ticks, acks, maintenance, re-init, `now`
  wraparound and time going backwards) against the pre-change module, built with a
  larger `alarm_t`. The outputs were identical. `acceptance.vec` did not need
  changes.

No item conflicts with the documented requirements or with the BACnet standard, so
every item was implemented.

Open points / things I am unsure about:
- **Changing a configuration after init now has a visible effect.** Before, each
  point kept the values it had at `alarm_init`. Now a write to an `alarm_cfg_t`
  takes effect right away, on the next call, for *every* point that shares it. That
  includes any pending delay and escalation timer that is running. This follows
  from "keeps a pointer", so it is part of the requested change, but it is the one
  way the external behavior differs. The random scenarios above do show different
  output once the configuration is rewritten after init. Consequences:
  - A BACnet front-end that allows writes to High_Limit/Low_Limit/Deadband/
    Time_Delay/Time_Delay_Normal must not write into a configuration that other
    objects share. In BACnet these are per-object properties. The front-end needs
    its own `alarm_cfg_t` for that object, and the only way to attach a point to a
    different configuration is `alarm_init`, which resets the point's state. That
    was already the case before this CR.
  - Writes to a configuration are not atomic with respect to the alarm calls. If an
    ISR or another task updates a configuration while `alarm_sample`/`alarm_tick`
    runs, it can see some old and some new values. The caller has to serialize
    this.
  - Test infrastructure: `driver.c` passes its single static `cfg` to `alarm_init`.
    A `cfg` line issued after `init` therefore now reconfigures the running point.
    That includes a `cfg` line rejected with BADCMD, which has already zeroed the
    struct. All current vectors re-`init` after `cfg`, so none are affected.
- **The RAM arithmetic does not close.** 512 points x 32 bytes = 16384 bytes, which
  is all 16 KB of RAM. That leaves nothing for the shared configurations (24 bytes
  each), stack, notification buffers or the rest of the firmware (BACnet stack,
  I/O). On the M0 the state actually needs only 28 bytes. An `alarm_t` of 28 bytes
  with 4-byte alignment (e.g. `uint32_t storage[7]` on 32-bit targets) would save
  2 KB, but 14 KB for the points alone is still very tight. I left the supplied
  `alarm.h` as it is. Please confirm whether "16 KB" is the whole RAM or a budget
  just for the points.

## CR-203

Collected field requests.

- **Item 1 — `EV_ACKED` notification (product owner)**: implemented. `alarm_ack`
  now emits `ACKED` with `time = now` and `value` = the most recent sample value
  (`last_value`) when it clears an unacknowledged alarm. It emits nothing when
  there is no unacknowledged alarm, so a second ack is silent. In maintenance mode
  the notification is suppressed, but the acknowledgement itself still takes
  effect, as it did before. This matches BACnet, which reports acknowledgements
  with an ACK_NOTIFICATION. `alarm.h` already had `EV_ACKED = 5`. I only added a
  comment to it.
- **Item 2 — return to normal clears the unacknowledged status and cancels the
  escalation (operators, site Nord)**: **not implemented, because it conflicts with
  BACnet.** In BACnet, acknowledgement applies to each transition
  (`Acked_Transitions`: TO-OFFNORMAL, TO-FAULT, TO-NORMAL). The TO-OFFNORMAL bit
  becomes TRUE only when an operator acknowledges the alarm
  (AcknowledgeAlarm). Returning to normal does not acknowledge it. An offnormal
  event that has since cleared therefore stays unacknowledged, and it keeps
  appearing in GetEventInformation/GetAlarmSummary until someone acknowledges
  it. Clearing `unacked` on return to normal would silently drop that
  acknowledgement, and a BACnet front-end would report a wrong acknowledgement
  state. Escalation exists to make sure someone looks at every alarm that was
  never acknowledged, including short excursions that have already cleared.
  What operators can do today: acknowledge the alarm (this now also produces
  ACKED), or change `escalate_after_s`. Escalation is our own feature and not part
  of BACnet. If the product owner wants escalation to stop after return to normal
  *while the alarm stays unacknowledged*, that could be a separate CR. It would
  change the product's escalation rule, so it needs the product owner's
  decision, not a field request.
- **Item 3 — inclusive limits (`>= high_limit`, `<= low_limit`) (operators, site
  Nord)**: **not implemented, because it conflicts with BACnet.** The BACnet
  OUT_OF_RANGE event algorithm (clause 13.3.6 in 135-2012 and later) raises
  HIGH_LIMIT only when the value is *greater than* pHighLimit and LOW_LIMIT only
  when it is *less than* pLowLimit, both for pTimeDelay. `target()` implements
  exactly this, so a value equal to the limit is normal. To get the operators'
  intent, configure the limit slightly inside the range they want. For example,
  set `high_limit` just below 30.0 (e.g. 29.95, depending on sensor resolution)
  if 30.0 itself must alarm.

Other changes:
- `acceptance.vec`: the updated `high-alarm-ack-and-return` block now passes. I
  added four blocks:
  - ACKED is emitted once and carries the latest sample (36.0), not the value
    that raised the alarm (35.0). An ack with no unacknowledged alarm emits
    nothing.
  - ACKED is suppressed in maintenance, but the ack still applies: there is no
    escalation afterwards and no replay when maintenance ends.
  - Return to normal does not acknowledge the alarm: escalation still fires and a
    later ack emits ACKED. This pins the BACnet behavior behind the decision on
    item 2.
  - A value exactly at the limit does not alarm, and a value exactly at
    `high_limit - deadband` does not return to normal. This pins the BACnet
    behavior behind the decision on item 3.

  All 13 blocks pass.

Open points / things I am unsure about:
- **ACKED is not replayed after maintenance.** State changes that were suppressed
  during maintenance are reported when maintenance ends. An ack made during
  maintenance is not reported, so the front-end never learns that the alarm was
  acknowledged. I read "not emitted in maintenance mode" literally. If the
  front-end tracks acknowledgement state (e.g. `Acked_Transitions`), it may need
  ACKED to be replayed when maintenance ends, or acks to be refused during
  maintenance. The product owner should decide.
- **An ack can acknowledge an alarm raised in the same call.** `alarm_ack` first
  runs the delay timer. If the timer expires in that call, the result is
  `TO_HIGH`/`TO_LOW` followed immediately by `ACKED`, although the operator can
  never have seen that alarm. This ordering was already there before this CR
  (the ack silently cleared the new alarm). ACKED only makes it visible. I left
  it unchanged.
- **The ACKED value can be NaN or a faulted reading.** If the alarm went
  HIGH/LOW -> FAULT before the ack, "most recent sample value" is the faulted
  sample (NaN, or the value passed with `sensor_fault`). I followed the CR
  literally.
- One acknowledgement covers the whole point, not each transition as in BACnet.
  TO-FAULT and TO-NORMAL transitions never need an acknowledgement here. That was
  already the case, and this CR does not change it.
