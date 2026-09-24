# alarm.c review

## Changes made (all in alarm.c)

1. `target()`, HIGH state: added `v < low_limit -> LOW`. Reason: from HIGH, a reading below the low limit fell back to NORMAL and then needed a second full time delay to reach LOW. LOW already goes straight to HIGH, and BACnet OUT_OF_RANGE does the same both ways.
2. `target()`, HIGH state: changed the return-to-normal test from `v <= high_limit - deadband` to `v < high_limit - deadband`. Reason: it was the only non-strict comparison of the four. The LOW exit is `v > low_limit + deadband`, and BACnet uses strict comparisons both ways. Example: with high=30 and deadband=2, a reading of exactly 28 cleared a HIGH alarm, but a reading of exactly 12 did not clear a LOW alarm (low=10).
3. `eval_timer()` / `check_escalate()`: added `elapsed()`, which treats a `now` older than the start time as "no time passed". Reason: `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` accept a late `now` (they only avoid moving `last_now` backwards), but the timers then ran `now - start` on unsigned values, which wrapped around to a huge number. A late tick then raised a pending alarm at once (`s 100 35; tick 90` with delay=30 gave `TO_HIGH@90`) or sent an early ESCALATE (`ESCALATE@50` for an alarm raised at 100). In-order calls behave exactly as before.
4. `alarm_sample()`, fault recovery: removed the early `return n;` after the FAULT -> NORMAL transition. Reason: the recovery sample is valid but was never checked against the limits, and `check_escalate` was skipped. An out-of-range value at recovery started its time delay one sample late. With delay=0 the point stayed NORMAL at a reading of 35 until the next sample, even through ticks. The FAULT -> NORMAL transition (TO_NORMAL) is still always sent first, as before.
5. `alarm_set_maintenance(off)`: the catch-up notification now goes through `notify_state()` instead of a hand-written `emit` plus `last_notified` update. Reason: a HIGH or LOW state entered during maintenance was announced but never marked unacknowledged, and `alarm_time` and `escalated` were not reset. That alarm could never escalate, or it inherited a stale `escalated` flag from an earlier alarm. Now it is treated like any other alarm notification from the moment maintenance ends.

## Suspicious-looking behavior deliberately left unchanged

- Only NaN (or `sensor_fault`) counts as a fault; +inf and -inf are treated as readings and cause HIGH or LOW alarms. An infinite value is out of range and comparable, so alarming on it is reasonable. The code states the fault rule explicitly (`sensor_fault || isnan`), so changing it would change the design.
- Escalation still fires when an alarm is unacknowledged but the point has since returned to NORMAL (or gone to FAULT). Acknowledgement is tracked per notified alarm, not per current state, and BACnet also requires acknowledgement after return to normal. Escalating an alarm nobody acknowledged is consistent with that.
- Recovery from FAULT always goes to NORMAL first (TO_NORMAL carries the recovery value, even when it is out of range) before the limits are checked. This matches BACnet, where FAULT goes to NORMAL.
- `alarm_ack` runs `eval_timer` before clearing `unacked`, so an alarm that becomes due at the exact time of the ack is acknowledged by that ack. Every command first brings the state up to date at `now` (maintenance does the same), so this looks intended.
- A late `alarm_sample` is dropped completely, but a late ack or maintenance command is still applied, and `last_now` never moves backwards. This difference is explicit in the code: operator commands are not discarded, stale readings are. Only the timer arithmetic was fixed (change 3).
- Escalation is suppressed during maintenance, but its timer keeps running, so an overdue escalation fires as soon as maintenance ends. The `!maint` check is explicit, and nothing says the timer should pause.
- State changes during maintenance still happen (silently). Only the net change is reported when maintenance ends, so a HIGH -> NORMAL -> HIGH round trip during maintenance produces no notification. This is intended: maintenance suppresses notifications, not alarm evaluation.
- `escalate_after_s == 0` turns escalation off instead of escalating at once. The explicit `> 0` check makes this intended.
- The `has_value` field is set but never read. It is harmless, so I left it alone.
- The `unacked` check in `alarm_ack` (`if (p->unacked) p->unacked = false;`) is redundant but harmless.
- `alarm_t` storage is accessed through a `point_t *` cast, which technically breaks strict aliasing. Everything goes through this module's own functions, and the size is checked with `_Static_assert`, so I left it as is. Using a union or memcpy would be the fully portable form.

## Verification
- `make` builds with no warnings. `python3 run_vectors.py acceptance.vec` passes 3/3.
- I ran scenarios for each fix through the driver: HIGH->LOW, the boundary values 28 and 12, late tick and late escalation, maintenance followed by escalation, and fault recovery with delay 0 and delay 30.
- I ran 3000 random scenarios, including late times, nan and inf, and maintenance and ack commands, against the fixed module under ASan and UBSan. There were no sanitizer errors, no BADCOUNT and no OVERRUN.
