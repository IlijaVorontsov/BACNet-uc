# alarm.c review

## Changes made (alarm.c only)

1. `alarm_tick` / `alarm_ack` / `alarm_set_maintenance`: stale timestamps (`now` earlier than the last accepted time) are now rejected through `accept_time()` (returns 0, no state change), the same way `alarm_sample` already handles them. Before, they ran `eval_timer`/`check_escalate` with the stale `now`, so `now - pending_start` / `now - alarm_time` wrapped around. That caused a spurious transition or escalation stamped with the old time (e.g. `s 20 35` then `tick 10` raised `TO_HIGH@10` with delay=30).
2. `target()` in HIGH: added a direct HIGH -> LOW transition when `v < low_limit`. This mirrors the existing LOW -> HIGH branch. Before, a HIGH point whose value dropped below the low limit first reported a false `TO_NORMAL`, then needed a second full time delay before `TO_LOW`.
3. `target()` in LOW: the clear threshold changed from `v > low_limit + deadband` to `v >= low_limit + deadband`. This matches the HIGH side (`v <= high_limit - deadband`) and the inclusive-boundary rule (CR3incl: a value on a boundary counts as normal). Before, a LOW alarm did not clear at exactly low+deadband, but a HIGH alarm did clear at exactly high-deadband.
4. `alarm_sample` fault recovery: removed the early `return n` after FAULT -> NORMAL. Before, that sample skipped `check_escalate`, so a due escalation of an unacknowledged alarm was lost at the recovery sample. The valid recovery value was also not evaluated: its pending/time-delay start was dropped until the next sample. Now the sample returns the point to NORMAL and is then evaluated normally.
5. `alarm_set_maintenance(off)`: the deferred state notification now goes through `notify_state()` instead of a bare `emit()`. Before, a HIGH/LOW alarm reported at the end of maintenance was never marked unacknowledged and never given an alarm time, so it could never escalate. Its escalation state was also left stale.

All 32 provided vectors pass after the changes (also under ASan/UBSan).

## Suspicious-looking behavior deliberately left unchanged

- An alarm that clears without being acknowledged stays unacknowledged and still escalates while NORMAL or FAULT. This is intended per CR3clear (x038, x041) and x032.
- Limits are strict for entering an alarm (`>` high, `<` low): a value exactly on a limit does not alarm. This is intended per CR3incl (x001, x002). The HIGH clear `<=` is left as is for the same inclusive-boundary reason (see change 3).
- An ack in the same call that raises an alarm (the timer matures inside `alarm_ack`) acknowledges that new alarm. This is intended per x021.
- Acks are accepted during maintenance, and escalation is only postponed (not cancelled) while maintenance is on. This is intended per x056 and x052.
- Entering FAULT, and returning from FAULT to NORMAL, is immediate (no time delay). The `TO_FAULT` note carries the faulty sample value. This is intended per x025 and x032.
- `+inf`/`-inf` samples are treated as ordinary out-of-range values, not as a sensor fault. Only NaN (which breaks every comparison) is forced to FAULT. Nothing indicates that inf should be a fault.
- A stale `alarm_ack` or `maint` call is now ignored rather than applied (see change 1). This matches `alarm_sample` and the "monotonic now" contract in alarm.h.
- `point_t.has_value` is written but never read. It is harmless (the state cannot change without a sample), so it is left in place.
- No validation of the configuration (for example `deadband < 0` or `low_limit >= high_limit`). alarm.h puts that on the caller (`/* >= 0 */`).
- Notifications are silently capped at `ALARM_MAX_NOTES`. The most any call can produce is 2, so the cap is never reached.
