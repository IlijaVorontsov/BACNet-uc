# alarm.c review

All changes are in `alarm.c`. `driver.c`, `run_vectors.py`, `alarm.h` and the vector files are unchanged. `make` builds with no warnings. `acceptance.vec` and `tests/unit.vec` pass (32/32). ASan/UBSan and a random fuzz run found no crashes or overruns.

## Changes made

1. `target()`, HIGH state (line 39): added the direct HIGH -> LOW transition (`v < low_limit`), checked before the return-to-normal test. Reason: LOW already goes straight to HIGH, but HIGH could not go straight to LOW. A reading below the low limit was reported as TO_NORMAL, and TO_LOW only came one delay later. This matches BACnet OUT_OF_RANGE.
2. `target()`, HIGH state (line 41): the return to normal now uses `v < high_limit - deadband` instead of `<=`. Reason: the LOW side (`v > low_limit + deadband`) and the alarm entry tests are all strict. A value exactly at `high - deadband` returned to normal, while a value exactly at `low + deadband` stayed in alarm. BACnet uses strict comparisons on both sides.
3. `eval_timer()` (line 92): added `now >= pending_start &&` before the unsigned subtraction. Reason: `tick`, `ack` and `maint` accept an earlier `now` (they only keep `last_now` from going backwards). With such a `now`, `now - pending_start` wrapped to about 4e9 and caused an immediate false transition. Example: `s 10 35; s 20 35; tick 5` gave `TO_HIGH@5`.
4. `check_escalate()` (line 99): added the same guard, `now >= alarm_time &&`. Reason: the same wraparound caused a false ESCALATE when a stale tick, ack or maint call came after an alarm. Example: HIGH at 200, then `tick 150` escalated.
5. `alarm_sample()` (line 132): a sample is now a fault when `!isfinite(value)`, not just `isnan(value)`. Reason: +/-inf is not a valid analog reading, such as an overflowed or divide-by-zero conversion. Before, it raised a HIGH or LOW alarm instead of FAULT.
6. `alarm_sample()` (line 138): removed the early `return n;` after the FAULT -> NORMAL recovery. Reason: it skipped `check_escalate()`, so an unacknowledged alarm whose escalation was due was not reported on the recovery call. It also skipped the limit check on the recovery sample, so a sample still out of range did not start the time delay. Every other path runs both steps.
7. `alarm_set_maintenance()`, maintenance off (line 188): the catch-up notification now goes through `notify_state()`, not a partial inline copy (`emit` plus `last_notified`). Reason: an alarm that happened during maintenance and was announced when maintenance ended was never marked unacknowledged, and its escalation timer was never started. So it could never escalate, unlike an alarm announced by `transition()`.

## Suspicious-looking behavior left unchanged (believed intended)

- Escalation continues after the point returns to NORMAL, or while it is in FAULT, until the alarm is acknowledged. Reason: `unacked` means "an alarm notification is unacknowledged", not "currently in alarm". Tests x038, x063 and x032 confirm this.
- `ack` evaluates the delay timer before clearing `unacked`, so an alarm raised in the same call is acknowledged at once. `ack` also clears `unacked` before the escalation check. Tests x021 and x043 confirm this.
- A pending transition is cancelled only by a sample that no longer meets the condition, even if the delay has already passed by that sample's time. A `tick` never cancels. Tests x019 and x020 confirm this.
- During maintenance, transitions still happen but are silent, and escalation is held back. On maintenance off, the module sends a notification only if the state differs from the last one it sent, then runs any escalation that is due. `ack` works during maintenance. Tests x023, x048, x051, x052, x056 and x057 confirm this.
- A new HIGH/LOW notification restarts the escalation timer and clears `escalated`, even while an earlier alarm is still unacknowledged. Test x046 confirms this.
- Stale `tick`, `ack` and `maint` calls (with `now` below `last_now`) are still processed and do not move `last_now` back. Stale samples are dropped. I only removed the arithmetic wraparound, because the separate `if (now > last_now)` handling looks deliberate. Change 3 and change 4 cover the wraparound.
- In a LOW (or now HIGH) state, the pending timer restarts when the target changes, for example from "to HIGH" to "to NORMAL". This is the same behavior the existing LOW state already had.
- The fault sample's value, including NaN or inf, is stored as `last_value` and used in later notes, such as ESCALATE while in FAULT. This matches x032 and the header ("value" of the latest sample).
- FAULT -> NORMAL recovery has no time delay. The existing code and tests x032 and x055 both show this.
- `escalate_after_s == 0` turns escalation off. Test x042 confirms this.
- `has_value` is written but never read. It is harmless dead state, so I left it in place.
- `alarm_t` is accessed through a `point_t *` cast. The storage is 8-byte aligned and a `_Static_assert` checks the size. This is the opaque-handle design the header describes, and I left it as is.
- If more than `ALARM_MAX_NOTES` notes are produced, `emit()` drops the extras without warning. At most 2 notes can be produced per call, so this cannot happen.
