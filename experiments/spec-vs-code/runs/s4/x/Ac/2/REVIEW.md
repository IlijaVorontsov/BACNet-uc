# alarm.c review

## Changes made (alarm.c)

- `target()`, HIGH case: added the missing `v < low_limit -> LOW` check before the return-to-normal check. Without it, HIGH went to NORMAL instead of LOW, which breaks A4 [STD] (direct HIGH<->LOW, and the direct transition wins).
- `target()`, HIGH case: changed the return condition from `v <= high_limit - deadband` to `v < high_limit - deadband`. A3 [STD] requires a strict comparison, and the LOW branch already used one.
- `alarm_sample()`, FAULT recovery: removed the early `return n;` after the FAULT->NORMAL transition. Per A6a the recovering sample must then be evaluated as a sample in NORMAL (possibly TO_NORMAL then TO_HIGH in one call), and the A9 escalation check must still run.
- `alarm_tick()`, `alarm_ack()`, `alarm_set_maintenance()`: these now use `accept_time()` and return 0 when `now` is earlier than the latest time seen. Before, they only skipped updating `last_now` and carried on. That broke A12, and `now - pending_start` / `now - alarm_time` could wrap to a huge value: a late tick fired pending transitions or escalations early, and a late ack/maint changed state.
- `alarm_set_maintenance()`, on->off catch-up: now calls `notify_state()` rather than emitting the event directly. A10 and the `notify_state` header both say that a catch-up TO_HIGH/TO_LOW starts a new A8 episode (unacked, alarm_time = now, escalation re-armed). Before this change, an alarm raised during maintenance could never be acknowledged or escalate.
- Added `regression.vec` with one vector per fix. `acceptance.vec` and `regression.vec` pass 10/10, and a separate ASan/UBSan build of the same code runs clean. `driver.c` and `run_vectors.py` are unchanged.

## Suspicious-looking behavior deliberately left unchanged

- A pending transition is cancelled when a sample has no target, even if its delay had already expired since the previous call. There is no retroactive transition. This is documented A5a [POL]; no rationale is recorded, so it needs the product owner.
- TO_NORMAL does not clear `unacked`, so an episode that has returned to normal can still escalate. This is A8 [POL] and has a recorded rationale (SP-3).
- `alarm_ack` evaluates the timer before applying the ack, so an ack arriving in the same call as a TO_HIGH/TO_LOW immediately acknowledges the new episode. This is documented ordering (A5a/A8 [POL]).
- tick/ack/maintenance do not recompute the target. The last sample value is assumed to persist (A5a [POL]).
- Fault samples overwrite `last_value`, so notifications can carry NaN or the value from a faulted sample (A7 [POL]). +/-Infinity is treated as a valid value (A6 [POL]).
- Recovery from FAULT to NORMAL is immediate and ignores `time_delay_s`. Timers do not run in FAULT and a fault cancels the pending transition (A6/A6a [POL]).
- An unacknowledged episode can still escalate while the point is in FAULT, because TO_FAULT does not touch the ack state (A6a [POL]).
- Maintenance catch-up is emitted only if the final state differs from the last notified state. An excursion that starts and ends inside maintenance (e.g. HIGH->NORMAL->HIGH) is never reported. An old unacked episode can escalate as soon as maintenance ends (A10/A9 [POL]).
- An ack emits no notification and does nothing when nothing is unacknowledged. `escalate_after_s = 0` disables escalation (A8/A9 [POL]).
- `alarm_init` does not validate the configuration (low > high, negative or NaN deadband/limits). The header only states `deadband >= 0` as a precondition. Changing this would change the API contract.
- `emit()` silently drops notifications beyond ALARM_MAX_NOTES. This is unreachable: at most 3 notes per call after the fixes.
- The `has_value` field is written but never read. It is documented as intentional and is harmless.
- The timer and escalation checks use `>=`, so they fire exactly at the delay. With `time_delay_s = 0` the transition happens on the sample itself (A5a [POL]).
