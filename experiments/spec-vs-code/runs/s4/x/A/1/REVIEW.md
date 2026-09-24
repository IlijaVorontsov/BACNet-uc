# alarm.c review

## Changes made

1. `target()`, HIGH state: added `if (v < low_limit) return ALARM_LOW;` before the return-to-normal check. Reason: LOW already goes straight to HIGH (`v > high_limit`), but HIGH had no matching path to LOW. With a wide deadband (for example high=30, low=10, deadband=25), a value below the low limit left the point stuck in HIGH. The fix matches the LOW branch and BACnet OUT_OF_RANGE rule (c).
2. `target()`, HIGH state: `v <= high_limit - deadband` became `v < high_limit - deadband`. Reason: the LOW branch clears only when the value is strictly beyond the deadband (`v > low_limit + deadband`), while HIGH cleared on the boundary itself. A value exactly at the deadband edge now behaves the same for both limits, as in BACnet OUT_OF_RANGE rules (e) and (f).
3. `eval_timer()`: added `now >= pending_start` before `now - pending_start >= time_delay_s`. Reason: `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` accept a `now` earlier than the last time seen. The unsigned subtraction then wrapped to a huge number and fired the pending transition too early, even at a time before the condition began (for example "tick 5" after samples at 10 and 20 raised TO_HIGH@5).
4. `check_escalate()`: added `now >= alarm_time` for the same reason. Reason: a call with a stale time wrapped the unsigned difference and sent ESCALATE right away, even with a timestamp before the alarm itself.
5. `alarm_sample()`: removed the early `return n;` after the FAULT to NORMAL transition. Reason: the valid sample that cleared the fault was thrown away instead of being checked against the limits, so the alarm delay started one sample late (with delay=0, an out-of-range recovery value raised no alarm until the next sample). The early return also skipped `check_escalate`, which every other path runs.
6. `alarm_set_maintenance(off)`: the catch-up notice for a state change hidden during maintenance now goes through `notify_state()`, not a bare `emit()`. Reason: the bare path skipped the alarm bookkeeping (`unacked`, `alarm_time`, `escalated`). A HIGH or LOW alarm first reported when maintenance ended therefore never needed an ack and never escalated, or it escalated using the start time of an older alarm.

## Suspicious-looking behavior deliberately left unchanged

- `isnan(value)` rather than `!isfinite(value)`: +inf and -inf are ordered values, so they raise HIGH or LOW instead of FAULT. This reads as an intended "saturated reading" result. It is not clearly a bug, so I kept it.
- `unacked` is not cleared when the point returns to NORMAL, so an unacknowledged alarm can still escalate after the value recovers. This is normal "unacknowledged, returned to normal" handling: only `alarm_ack` clears it.
- FAULT transitions do not set `unacked` and do not escalate by themselves. `notify_state` explicitly limits ack tracking to HIGH and LOW.
- `alarm_ack`, `alarm_tick` and `alarm_set_maintenance` first bring the timers up to date, then apply the command. So an ack at the moment a delayed alarm fires also acknowledges that alarm. The same order is used in all three functions, so I kept it.
- `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` do not reject a stale `now` (only `alarm_sample` does). A late ack or maintenance command still takes effect. Only the timer arithmetic was guarded (changes 3 and 4).
- `accept_time` accepts a sample whose time equals the last one. Equal timestamps are valid for a monotonic clock.
- State changes are still tracked during maintenance. Only notifications and escalation are held back, and the net change is reported when maintenance ends.
- The unsigned subtractions `now - pending_start` and `now - alarm_time` are safe once the order checks from changes 3 and 4 are in place.
- `has_value` is written but never read. It is unused state with no effect on behavior, so I kept it to avoid changing the layout.
- `alarm_init` does not check the config (deadband >= 0, low <= high). The header documents `deadband >= 0` as a precondition for the caller.
- `emit()` silently drops notes beyond ALARM_MAX_NOTES. No path produces more than 3 notes in one call, so this cannot happen.
