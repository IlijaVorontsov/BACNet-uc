# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). I found no conflict with BACnet or with the existing behaviour and tests, so every item below is implemented. There is no requirements document in this directory; the unit vectors cite requirement IDs A2–A13, but the document itself is missing, so I checked the change only against the existing vectors. All 44 original vectors still pass, and so do the 8 new ones (52 of 52 in total).

| Item | Status |
|------|--------|
| Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. The field was already in the supplied `alarm.h`, which I left unchanged. `alarm_init` copies it together with the rest of the configuration, and `point_t` still fits in `alarm_t` (the `_Static_assert` still holds). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented through `pending_delay()` in `alarm.c`, which `eval_timer` now calls. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | Implemented. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME` in `alarm.c`). This value is the driver's default, so existing configurations behave exactly as before. |
| `driver.c` `delay_normal=N` / updated `alarm.h` | I checked that both are present and did not modify either one. |

Tests: I added vectors `y001` to `y008` (tag `CR201`) to `tests/unit.vec`. They cover:
- a longer and a shorter normal delay
- HIGH↔LOW transitions still using `time_delay_s`
- the explicit `0xFFFFFFFF` value
- the timer restarting when the value leaves the band
- the return to normal during maintenance
- FAULT→NORMAL

I left `acceptance.vec` unchanged; its scenarios still pass.

BACnet consistency: this change matches Time_Delay_Normal semantics. The OUT_OF_RANGE algorithm uses pTimeDelayNormal for transitions to NORMAL and pTimeDelay for everything else. When Time_Delay_Normal is absent, Time_Delay is used, which is what the `0xFFFFFFFF` value represents here.

Points I am unsure about:
- **FAULT→NORMAL.** This stays immediate, with no `time_delay_normal_s` delay (vector `y007`). BACnet treats leaving FAULT as a reliability change, not as an event-algorithm transition, and existing vectors x029 and x030 already rely on it being immediate. I read the CR's "HIGH or LOW back to NORMAL" as not covering FAULT.
- **Value falling from above high to below low while in HIGH.** The code keeps its existing behaviour: LOW is the pending target and is timed with `time_delay_s`. This follows the CR's "HIGH↔LOW keep using time_delay_s". However, the BACnet algorithm also treats "below high−deadband for Time_Delay_Normal" as satisfied at the same time. So if `time_delay_normal_s` < `time_delay_s`, a strict reading of BACnet could give TO_NORMAL first, where this code goes straight to LOW. The pre-existing restart-on-target-change rule (A5a, x013) is related and is also unchanged. If product wants the strict BACnet behaviour, that needs its own CR.
- **The `0xFFFFFFFF` value.** Because it means "same as `time_delay_s`", a literal delay of 4294967295 s (about 136 years) cannot be configured. I consider this harmless.

## CR-202

This CR asks for a smaller `alarm_t` and for `alarm_init` to keep a pointer to the configuration. I found no conflict with BACnet or with the existing behaviour and tests, so every item is implemented. There is a problem with the RAM arithmetic, though (see the first point under "Points I am unsure about"). All 52 unit vectors and the 3 acceptance scenarios pass unchanged.

| Item | Status |
|------|--------|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Implemented. The updated `alarm.h` was already in place and I left it unchanged. In `alarm.c`, `point_t` no longer contains an `alarm_cfg_t`. Its fields are reordered largest first, so it takes 28 bytes with 32-bit pointers (Cortex-M0; checked with `clang --target=thumbv6m-none-eabi`) and 32 bytes with 64-bit pointers (host test build). `_Static_assert`s check both its size and its alignment against `alarm_t`. |
| `alarm_init` keeps a pointer instead of copying the configuration | Implemented. `point_t.cfg` is now `const alarm_cfg_t *`, and every read of a limit, delay or escalation time goes through it. |
| The caller keeps the `alarm_cfg_t` alive, and configurations are shared between points | Relied on. The module cannot check this. The driver's configuration is `static`, so it meets the rule. |
| External behavior must not change | Implemented. Besides the vectors, I compared the new code with the previous copying implementation, rebuilt temporarily with a larger `alarm_t`. I ran 15,000 random scenarios (about 280,000 commands, covering every command type, `delay_normal`, time wrap and time going backwards) and the outputs were byte-for-byte identical. |

I added no new vectors. For a caller that keeps its configuration unchanged, nothing is externally different. The only new behaviour is the one described under "Changing a configuration after `alarm_init`", and the CR does not specify it, so I did not fix it in a test.

Points I am unsure about:
- **The RAM budget does not add up.** 512 points × 32 bytes = 16,384 bytes, which is all of a 16 KB part. That leaves nothing for the shared `alarm_cfg_t`s (24 bytes each), the stack, the notification buffers or the BACnet stack. The firmware lead should confirm the figures. Options that do not touch the alarm logic:
  - On the target, `point_t` needs only 28 bytes, so a 28-byte `alarm_t` (14 KB for 512 points) would work. The host build with 64-bit pointers still needs 32 bytes.
  - Packing the state, pending target and flags into bit-fields would bring it to about 24 bytes on the target (12 KB).
  - Storing a 16-bit configuration index instead of a pointer would save more.

  I did not change `alarm.h`, because the CR says it is already in place.
- **Changing a configuration after `alarm_init`.** Before, changes had no effect until the next `alarm_init`, and that call also reset the point's state. Now a change takes effect at the next call on every point that shares that `alarm_cfg_t`. The CR does not say whether in-place changes are allowed. If they are, the writes are not atomic: a change made from another task or an ISR during an `alarm_*` call can be seen half-applied, for example a new `high_limit` with the old `deadband`. A configuration on the stack or freed early now leaves a dangling pointer. This is the caller's responsibility under the new contract.
- **BACnet integration.** High_Limit, Low_Limit, Deadband, Time_Delay and Time_Delay_Normal are per-object properties. A WriteProperty to one object must not change other objects that share its `alarm_cfg_t`, so the application layer has to give that object its own configuration first (copy-on-write). There is no API to move a point to a different `alarm_cfg_t` without `alarm_init`, and `alarm_init` resets the point's alarm state. This is not a conflict inside this module, but the integrator needs to know it.
- **Earlier notes.** The CR-201 section above says that `alarm_init` "copies" the configuration. CR-202 supersedes that. The unused `has_value` field (written, never read) is still there. It fits in the budget, and removing it would not reduce `sizeof(alarm_t)`.

## CR-203

Collected field requests. I implemented item 1 only. Items 2 and 3 conflict with the BACnet standard and with the product's documented requirements, so I did not implement them; the conflicts are explained below. The requirements document is still missing from this directory, so "documented requirements" here means the requirement IDs cited by `tests/unit.vec` and the behaviour those vectors fix. All vectors pass: 58 of 58 unit vectors and 3 of 3 acceptance scenarios (61 of 61).

| Item | Status |
|------|--------|
| 1. Emit `EV_ACKED` when `alarm_ack` clears an unacknowledged alarm | Implemented. |
| 2. Returning to normal clears the unacknowledged status and cancels escalation | Not implemented: conflicts with BACnet and with requirements A8/A9. |
| 3. Inclusive limits (`>= high_limit`, `<= low_limit`) | Not implemented: conflicts with BACnet and with requirement A2. |

### Item 1: EV_ACKED (implemented)

- `alarm_ack` emits `ACKED` with `time = now` and `value` = the most recent sample value, only when the call actually clears the unacknowledged status. It emits nothing when:
  - nothing is unacknowledged (no alarm yet, or a repeated ack);
  - the call is rejected because time went backwards (z008);
  - the point is in maintenance mode. In that case the status is still cleared, as before, and the ACKED is not replayed when maintenance ends (x056, z004).
- An alarm entered during maintenance is not unacknowledged until its deferred TO_HIGH/TO_LOW has been notified, so an ack during maintenance emits nothing for it (z005). This is the existing rule; I did not change it.
- `alarm.h` already contained `EV_ACKED = 5` and I left it unchanged, as I did `driver.c`, `run_vectors.py` and the updated `acceptance.vec`. The only code change is in `alarm_ack` in `alarm.c`. An ack call now writes at most 2 notifications (a timer transition plus ACKED), well within `ALARM_MAX_NOTES`.
- BACnet: this corresponds to an ACK_NOTIFICATION after AcknowledgeAlarm, so it does not conflict.
- Existing vectors: I updated 8 of them. In each, only the output of the `ack` command changed, gaining the ACKED notification: x021, x033, x036, x039, x041, x043, x060 and x063. No other expected line changed.
- New vectors: `z001` to `z009` (tag `CR203`). z001 to z008 cover:
  - the value being the latest sample rather than the alarm value;
  - a repeated ack;
  - an ack while a return to normal is pending;
  - an ack after the return to normal;
  - maintenance;
  - an ack in FAULT (value NaN);
  - an ack after escalation;
  - an ack with time going backwards.

  z009 pins the exclusive limits (see item 3).

Points I am unsure about for item 1:
- **ACKED in the same call as the alarm.** If the ack's own timer evaluation raises the alarm, the output is `TO_HIGH` followed by `ACKED` in the same call (x021). The ack then acknowledges an alarm the operator cannot yet have seen. That was already the behaviour of the unacked flag; the notification only makes it visible. Product may want the ack to apply only to alarms notified before the call.
- **No replay after maintenance.** The front-end never learns about an ack given during maintenance. I read "not emitted in maintenance mode" as "dropped", not "deferred". If the BMS needs the ack to clear its display, the ACKED would have to be replayed at `maint off`. That is a separate decision.
- **Ack in FAULT.** It acknowledges the earlier HIGH/LOW alarm, and `value` is the fault sample, which can be NaN (x033, z006). The module still keeps one acknowledgment flag for the latest off-normal notification, not BACnet's per-transition Acked_Transitions. TO_FAULT and TO_NORMAL are never "unacknowledged", and ACKED does not say which transition it acknowledged. These limitations are pre-existing.

### Item 2: return to normal clears unacked and cancels escalation (not implemented)

- **BACnet.** Acknowledgment is per transition (Acked_Transitions, the AcknowledgeAlarm service). A TO-OFFNORMAL transition stays unacknowledged until an operator acknowledges it. The later TO-NORMAL transition is a separate event and does not acknowledge the earlier one. An object with unacknowledged transitions is still reported (for example by GetEventInformation) while its Event_State is NORMAL. Clearing the status automatically on return to normal would hide alarms that nobody acknowledged.
- **Product requirements.** Vectors x038 (A8 A9) and x063 (A5a A8 A9 A10) require an unacknowledged alarm that has returned to normal to still escalate (`NORMAL ESCALATE@110`, `NORMAL ESCALATE@400`). Vectors x041 and z003 show that the operator's acknowledgment is what stops it.
- **What the operators can do.** Acknowledge the alarm. With item 1 this now also sends ACKED to the front-end and cancels the escalation (z003).
- Escalation itself is a product feature, not a BACnet one. Changing it so that it runs only while the point is still in alarm, while the alarm stays unacknowledged, would not conflict with BACnet. It would change requirement A9 and vectors x038/x063, so it needs a decision by the product owner in its own CR, not an operator request.

### Item 3: inclusive limits (not implemented)

- **BACnet.** The OUT_OF_RANGE event algorithm uses strict comparisons: the transition to HIGH_LIMIT requires pMonitoredValue > pHighLimit and the transition to LOW_LIMIT requires pMonitoredValue < pLowLimit. The return to normal likewise uses < pHighLimit − pDeadband and > pLowLimit + pDeadband. BACnet clients and other devices interpret High_Limit/Low_Limit this way, so a value equal to the limit must not alarm.
- **Product requirements.** Vectors x001 and x002 (A2) require that a value exactly at `high_limit` (30.0) or `low_limit` (10.0) raises no alarm, even when held for longer than the delay. The new vector z009 restates this for CR-203.
- **Workaround for site Nord** (configuration only, no firmware change). Set High_Limit/Low_Limit just inside the value that must alarm, for example `high_limit` 29.9 if 30.0 must alarm.

### Earlier notes

The CR-202 section says "All 52 unit vectors and the 3 acceptance scenarios pass". The actual counts at that time were 49 unit vectors and 3 acceptance scenarios, 52 in total.
