# alarm.c review

The intended behavior is taken from the contract in alarm.c's header and the per-rule comments (SPEC.md was not in the workspace). Every fix makes the code do what its own [STD]/[POL] comment already says. No rule was changed.

## Changes made (all in alarm.c)

1. `target()`, HIGH case: added the missing `v < low_limit -> ALARM_LOW` check before the return-to-normal check. Reason: A4 [STD] requires a direct HIGH->LOW transition that wins over a return to normal. Before the fix, a HIGH point whose value dropped below low_limit went to NORMAL and never raised TO_LOW.
2. `target()`, HIGH case: changed `v <= high_limit - deadband` to `v < high_limit - deadband`. Reason: A3 [STD] requires a strict return condition, and the LOW side already uses strict `>`. Before the fix, a value exactly at high_limit - deadband cleared a HIGH alarm.
3. `alarm_sample()`, FAULT recovery: removed the early `return n;` after the TO_NORMAL transition. Reason: A6a says the recovering sample "is then evaluated below as a normal sample in NORMAL". Before the fix, an out-of-range recovery value set no pending timer (so no TO_HIGH or TO_LOW followed while the value held), and the A9 escalation check was skipped for that call.
4. `alarm_tick()`, `alarm_ack()`, `alarm_set_maintenance()`: replaced `if (now > last_now) last_now = now;` with `if (!accept_time(p, now)) return 0;`. Reason: A12 requires a call with an earlier time to be ignored with no state change. Before the fix, such a call went ahead. `now - pending_start` then wrapped around as an unsigned value and fired a pending transition early (for example `tick 50` after a sample at 100 raised TO_HIGH@50). A late call could also apply an ack or toggle maintenance.
5. `alarm_set_maintenance()`, catch-up on maintenance off: now calls `notify_state()` instead of a bare `emit()` plus a `last_notified` update. Reason: A10 says "a catch-up TO_HIGH/TO_LOW starts a new alarm episode (A8, via notify_state)". Before the fix, an alarm that happened during maintenance was reported but never became unacknowledged and never escalated (the escalation also used a stale alarm_time).

Added `regress.vec`, which has one vector per fix. Also added `fuzz_model.py`, a development aid that checks `./drv` against an independent Python model of the contract. After the fixes: acceptance.vec 3/3, regress.vec 16/16, 24,000 random fuzz scenarios agree, and the ASan/UBSan build is clean. driver.c and run_vectors.py are unchanged.

## Deliberately left unchanged

- TO_NORMAL does not clear `unacked`, so a point that is back to NORMAL can still escalate. This is intended: A8 [POL], safety policy SP-3 (every excursion must be seen by a person).
- A pending transition whose delay expired between calls is cancelled if the next sample has no target. This is intended: A5a [POL], a transition happens only when a call observes it.
- tick, ack and maintenance calls do not recompute the target and assume the last sample value persists. This is intended: A5a [POL].
- Fault samples overwrite `last_value`, so notifications can carry NaN. Repeated fault samples while in FAULT emit nothing but still update `last_value`. This is intended: A5a/A7 [POL].
- `unacked` is kept across FAULT, so a point can escalate while in FAULT. A6a says TO_FAULT does not touch the acknowledgement state. Whether escalating in FAULT is desirable is a product-owner question under the DO-NOT-CHANGE [POL] rule, so it was not changed.
- `alarm_ack` works during maintenance and clears the episode silently. No rule forbids this, and changing it would be a [POL] change without sign-off.
- Escalation of an episode that started before maintenance fires right after maintenance ends if its time has passed. This is intended: A9/A10, where escalation is only suppressed while maintenance is on.
- `has_value` is written but never read. The comment documents this as intentional (A13 already holds through `pending`). It is harmless, so it was kept.
- A negative deadband, high_limit < low_limit, and NaN limits are not validated. alarm.h documents `deadband >= 0` as a caller precondition. Adding validation would change the API behavior.
- The bound check in `emit()` silently drops notifications past ALARM_MAX_NOTES. The rules produce at most 3 per call (TO_NORMAL, TO_x, ESCALATE on FAULT recovery), so the check only protects `out[]`.
