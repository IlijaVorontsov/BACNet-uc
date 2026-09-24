# alarm.c review

## Changes made

- `target()`, HIGH state: added `if (v < low_limit) return ALARM_LOW;` so HIGH can go directly to LOW. LOW could already go directly to HIGH, but HIGH could only fall back to NORMAL and then needed a second full time delay to reach LOW, which delayed the LOW alarm.
- `target()`, HIGH state: the return-to-NORMAL test changed from `v <= high_limit - deadband` to `v < high_limit - deadband`. It was the only non-strict comparison: the LOW return (`v > low_limit + deadband`) and both limit checks are strict, as in the BACnet OUT_OF_RANGE algorithm. With deadband=2, HIGH=30, a value of exactly 28 returned to NORMAL, while the mirror case (LOW=10, value exactly 12) did not.
- `alarm_sample()`, fault recovery: removed the early `return n;` after the FAULT to NORMAL transition. It skipped `check_escalate()`, so an overdue unacknowledged alarm did not escalate on the recovery call. It also threw away the recovering sample itself, so an out-of-range value at recovery did not start the delay timer until the next sample.
- `alarm_tick()`, `alarm_ack()`, `alarm_set_maintenance()`: these now reject a `now` earlier than the last accepted time through `accept_time()` (return 0 and do nothing), the same as `alarm_sample()`. Before, they went on with a stale `now`, and the unsigned `now - pending_start` / `now - alarm_time` wrapped around. Result: an instant false alarm (e.g. `s 10 35; s 20 35; tick 5` gave `TO_HIGH@5` with delay=30) or an early ESCALATE.
- `alarm_set_maintenance(off)`: a state that changed during maintenance is now reported through `notify_state()` instead of a bare `emit()`. The old code did not mark the newly reported HIGH/LOW alarm as unacknowledged and did not restart its escalation timer. So an alarm that started during maintenance could never escalate, and one that replaced an older unacked alarm escalated right away using the old alarm's time.
- Added `tests/regression.vec` (13 blocks) covering each fix. It passes along with `acceptance.vec` and `tests/unit.vec` (45/45), also under ASan/UBSan.

## Suspicious-looking behavior left unchanged (intentional)

- `alarm_ack()` runs the delay timer before clearing `unacked`, so an ack that raises an alarm also acknowledges it in the same call. Unit test x021 relies on this.
- An unacknowledged alarm still escalates after the point returns to NORMAL or goes to FAULT, and entering FAULT/NORMAL does not reset `alarm_time`. Tests x032, x038 and x063 rely on this.
- Escalation is held back during maintenance and fires on the first call after maintenance ends (x052). An ack during maintenance does count (x056).
- A valid sample in FAULT returns the point to NORMAL right away, with no time delay (x032, x055).
- A HIGH/LOW alarm that clears and comes back during maintenance is not reported again at maintenance-off, because the state equals the last notified state. That is the intended "report only net state changes" behavior (x051, x063).
- Only NaN (or `sensor_fault`) counts as a fault. `+inf`/`-inf` are treated as ordinary out-of-range values (HIGH/LOW). Nothing says they should be faults.
- `has_value` is set but never read. It is dead but harmless, so I left it in to keep the change small.
- `accept_time()` compares `now < last_now` directly, which is not safe across a uint32 wraparound (after about 136 years of uptime). The delay and escalation timers use wrap-safe subtraction. I left it alone.
- `P()` accesses the `uint64_t` storage in `alarm_t` through a `point_t *`. This is the usual opaque-storage pattern, and the header explicitly allows it. Alignment and size are fine (`_Static_assert`), so I did not change it.
- Configuration is not validated (e.g. a negative deadband or `low_limit > high_limit`). The header documents `deadband >= 0` as the caller's responsibility.
