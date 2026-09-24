# alarm.c review

## Changes made (alarm.c)

1. `target()`, HIGH state: return-to-normal test changed from `v <= high_limit - deadband` to `v < high_limit - deadband`, because A3 requires a strict comparison. Before the fix, a value exactly at `high - deadband` returned to NORMAL.
2. `target()`, HIGH state: added the direct `v < low_limit` → LOW check ahead of the return-to-normal check, because A4 requires a direct HIGH→LOW transition that wins over a return to normal. Before the fix, HIGH→(low value) went to NORMAL, and a LOW alarm was never raised until later samples came in.
3. `alarm_sample()`, FAULT recovery: removed the early `return n;` after the TO_NORMAL transition. A6a says the recovering sample is then evaluated as a normal sample in NORMAL. That covers starting a pending transition, a second transition when delay=0, and the A9 escalation check that runs at the end of every call. The early return skipped all three.
4. `alarm_tick()`, `alarm_ack()`, `alarm_set_maintenance()`: they now use `accept_time()` and return 0 when `now` is below the largest time seen, because A12 says such calls are ignored entirely. Before the fix they still ran. A backwards `tick` could fire a pending transition too early, since `now - pending_start` wraps around as unsigned. A backwards `ack`/`maint` changed ack and maintenance state.
5. `alarm_set_maintenance(off)`: the catch-up notification now goes through `notify_state()` instead of a bare `emit()`, because A10 says a catch-up TO_HIGH/TO_LOW starts a new alarm episode (unacked, alarm time = now, escalation re-armed). Before the fix, a catch-up alarm never became unacknowledged and never escalated.

Added `review_tests.vec`: 20 regression vectors derived from SPEC.md. Several of them failed before the fixes (`python3 run_vectors.py acceptance.vec review_tests.vec` → 23/23 now). The fixed module was also cross-checked against an independent Python model of SPEC.md on about 230k randomized commands, and under ASan/UBSan, with no differences and no sanitizer reports. driver.c, run_vectors.py and the Makefile were not modified.

## Suspicious-looking behavior deliberately left unchanged

- Returning to NORMAL does not clear the unacknowledged status, and a NORMAL point can still ESCALATE. This is required by A8 [POL] (safety policy SP-3).
- The escalation check still runs on fault samples and ticks while in FAULT, so a point that went HIGH→FAULT unacked can ESCALATE. A9 says the check runs "at the end of every call", and A6a says TO_FAULT does not change the ack state. "Further fault samples do nothing" is read as "no state change or transition".
- A fault sample received while already in FAULT still updates the stored last value. A7/A9 require notifications to carry the most recent sample value, including fault samples.
- A pending transition whose delay expired between calls is cancelled if the next sample no longer satisfies it. This is required by A5a [POL].
- `alarm_set_maintenance(on)` when already on, and `(off)` when already off, still evaluate the timer and run the escalation check. A5a/A9 require that for every call, and "changes nothing" refers to the maintenance mode itself (no catch-up).
- `alarm_ack` also clears the unacked status while in maintenance mode. The spec does not restrict ack in maintenance.
- Deadband thresholds (`high - deadband`, `low + deadband`) are computed in float, matching the float API types. The spec does not require higher precision, and changing it would move edge-case results.
- The config is not validated (negative deadband, low > high, NaN limits). The header documents `deadband >= 0` as a caller precondition, and the spec defines no error handling.
- `has_value` is written but never read. A13 already holds because nothing is pending before the first sample, so this is harmless and left alone.
- `point_t` is stored in `alarm_t`'s `uint64_t` storage through a pointer cast. It is formally a strict-aliasing gray area, but it is the documented opaque-storage design, sized and aligned correctly (static assert), and it causes no observed issue.
- `emit()` silently drops notifications past ALARM_MAX_NOTES. At most 2 can be produced per call (e.g. TO_NORMAL + TO_HIGH on recovery; ESCALATE cannot follow a TO_HIGH/TO_LOW in the same call, since the new episode resets the alarm time to now), so it is only a safety guard.
