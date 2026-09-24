# alarm.c review

## Changes made

1. `target()`, HIGH state: return-to-normal test changed from `v <= high - deadband` to `v < high - deadband`. It was asymmetric with the LOW state (`v > low + deadband`, strict) and with BACnet OUT_OF_RANGE, so a value exactly at `high - deadband` cleared a HIGH alarm but the mirror value `low + deadband` did not clear a LOW alarm.
2. `target()`, HIGH state: added `v < low_limit -> ALARM_LOW` before the return-to-normal check. LOW->HIGH was already direct, but HIGH->LOW went through a spurious TO_NORMAL and a second full time delay.
3. `alarm_set_maintenance(off)`: the deferred state report now goes through `notify_state()` instead of a bare `emit()`. Before, a HIGH/LOW alarm raised during maintenance was announced at maint-off but never marked unacked and never timestamped. It could not escalate, and it kept the old `unacked`/`escalated`/`alarm_time` of an earlier alarm.
4. `eval_timer()` / `check_escalate()`: elapsed time is now computed by a new helper, `elapsed()`, which treats `now < since` as no time elapsed. `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` accept a `now` older than the last call. `now - pending_start` or `now - alarm_time` then wrapped to about 4e9, which fired the time delay or the escalation immediately (for example `s 100 35` then `tick 50` gave `TO_HIGH@50` even with delay=60).
5. Added `tests/regression.vec`, which covers the four fixes above plus the rejection of stale samples. Run it with `python3 run_vectors.py acceptance.vec tests/regression.vec` (12/12 pass). A 3000-scenario random run under ASan and UBSan found no errors, overruns or bad counts.

## Suspicious-looking behaviour left unchanged (believed intentional)

- Only NaN counts as an implicit sensor fault; `+inf` and `-inf` are handled as ordinary out-of-range readings (HIGH/LOW after the delay). Infinities compare correctly and `sensor_fault` is the explicit fault channel. Nothing in the spec says inf means fault.
- Recovery from FAULT goes straight to NORMAL (no time delay) and returns early. The recovering sample's value is not checked against the limits, so any delay starts at the next sample, and `check_escalate` is skipped for that one call. The code is explicit and matches BACnet fault-clear semantics.
- Escalation does not check the current state: an unacknowledged alarm still escalates after the point has returned to NORMAL or gone to FAULT. The ack/escalate logic tracks the unacknowledged alarm, not the live condition.
- `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` accept stale times; only samples are rejected. Rejecting them would drop operator acks and maintenance commands. Only the wrap-around consequence was fixed (item 4).
- During maintenance, state keeps tracking silently and escalation is suppressed, but `alarm_time` keeps aging. An old unacked alarm can therefore escalate in the same call as maint-off. Round trips during maintenance (for example HIGH->NORMAL->HIGH) are not reported, because only a final state that differs from the last notified one is.
- A sample back in range cancels a pending transition even if the delay would have expired since the last evaluation (no tick in between). The value at evaluation time decides.
- Two samples with the same `now` are both accepted.
- Note values (TO_FAULT, ESCALATE, maint-off report) use the latest sample value, which can be NaN or not the value that caused the transition.
- `has_value` is written but never read. It is a dead field and harmless.
- `alarm_init` does not validate the configuration (negative deadband, low > high). The header documents `deadband >= 0` as the caller's contract.
