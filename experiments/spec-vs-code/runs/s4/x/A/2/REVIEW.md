# alarm.c review

## Changes made (all in alarm.c)

- `target()`, HIGH state: added `v < low_limit -> LOW`. Before, a point in HIGH whose value dropped below the low limit first reported TO_NORMAL (a false all-clear) and only reached LOW after a second time delay. LOW already went straight to HIGH, and the BACnet out-of-range algorithm has both direct transitions.
- `target()`, HIGH state: changed the return-to-normal test from `v <= high_limit - deadband` to `v < high_limit - deadband`. LOW returned to normal only when `v > low_limit + deadband` (strict), so the two sides treated the deadband edge differently. Strict on both sides matches BACnet ("falls below High_Limit - Deadband").
- `alarm_tick`: a call whose `now` is older than the last accepted time now returns 0 and does nothing. Before, `now - pending_start` and `now - alarm_time` wrapped around (unsigned), so a stale tick immediately fired a pending transition or an ESCALATE.
- `alarm_ack` / `alarm_set_maintenance`: with a stale `now`, the operator action (ack, maintenance on/off) still takes effect but the timers are not evaluated. This is the same unsigned wraparound problem.
- `alarm_ack`: the unacked flag is now cleared before the delay timer is evaluated. Before, an alarm raised by the timer inside the ack call (TO_HIGH/TO_LOW reported in the ack's own output) was acknowledged immediately, before the operator had seen it, and so it never escalated.
- `alarm_set_maintenance(off)`: a state change that happened during maintenance is now reported through `notify_state()` instead of a bare `emit()`. Before, an alarm raised during maintenance was reported at maintenance-off but was never marked unacknowledged and got no fresh alarm time. It could never escalate, or it inherited a stale or already-escalated earlier alarm.
- `alarm_sample`, leaving FAULT: removed the early `return` after the FAULT->NORMAL transition. Before, the first valid sample after a fault was discarded, so no delay timer started. With change-of-value sampling an out-of-range point could stay unalarmed indefinitely. The early return also skipped `check_escalate`.
- Added `regression.vec`, which has one vector per fix above. Run it with `python3 run_vectors.py acceptance.vec regression.vec`. It also passes under ASan/UBSan.

## Suspicious-looking behavior deliberately left unchanged

- Only NaN (or `sensor_fault`) counts as a fault. +/-inf is treated as a real out-of-range value (HIGH/LOW). NaN has to be special-cased because every comparison with it is false. inf compares correctly, and sensor reliability is signalled separately through `sensor_fault`.
- An unacknowledged alarm still escalates after the point returns to NORMAL or goes to FAULT. The acknowledgement is latched, like BACnet acked-transitions, and alarms clear only through `alarm_ack`.
- Maintenance mode suppresses escalation instead of cancelling it. An alarm that was already unacknowledged when maintenance started escalates at maintenance-off if it is overdue by then.
- Note timestamps are the `now` of the call that detects the transition, not `pending_start + time_delay_s`. The header documents this.
- `time_delay_s == 0` means an immediate transition, and `escalate_after_s == 0` disables escalation. Both look deliberate.
- The first valid sample after a fault always reports TO_NORMAL, even if the value is out of range. A TO_HIGH/TO_LOW follows after the time delay, or in the same call when the delay is 0. This matches BACnet FAULT->NORMAL handling.
- `emit()` silently drops notes beyond ALARM_MAX_NOTES. No call can produce more than 3, so this is only a guard.
- `alarm_init` does not validate the configuration (deadband >= 0, low < high). The header states this as a caller precondition.
- `has_value` is written but never read. It is dead but harmless, so it was left alone to keep the change set minimal.
- `alarm_t` storage is accessed through a cast to `point_t` (a strict-aliasing gray area). This is the header's documented opaque-storage design. All accesses in alarm.c go through `point_t`, and the build is correct as it stands.
- A stale `alarm_set_maintenance(off)` that reports a pending state stamps the note with its own (older) `now`, as the header specifies.
