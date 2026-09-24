# alarm.c review

Checked against SPEC.md v1.0. Every change below was first reproduced by a failing vector
in `review.vec`. All vectors failed on the original code and now pass (`acceptance.vec` 3/3,
`review.vec` 12/12).

## Changes made (all in alarm.c)

1. `target()`, HIGH state: return-to-normal test changed from `v <= high - deadband` to `v < high - deadband`. Reason: A3 requires a strict comparison. The old test left HIGH when the value was exactly at `high - deadband`.
2. `target()`, HIGH state: added the low condition (`v < low_limit` gives LOW), checked before the return test. Reason: A4 requires a direct HIGH to LOW transition that wins over return to normal. Before, a HIGH point that dropped below the low limit went to NORMAL (TO_NORMAL) and only reached LOW on a later call, or never if `time_delay_s` was never met again. LOW to HIGH was already correct.
3. `alarm_tick`, `alarm_ack`, `alarm_set_maintenance`: they now call `accept_time()` and return 0 when `now` goes backwards, as `alarm_sample` already did. Reason: A12. Before, a backwards `now` was still processed. The unsigned math `now - pending_start` and `now - alarm_time` then wrapped to a huge number, giving an early TO_x transition or ESCALATE at a past time. It also changed the maintenance flag and ack state.
4. `alarm_sample`, recovery from FAULT: removed the early `return n` after the TO_NORMAL transition. Reason: A6a says the recovering sample is then evaluated as a normal sample in NORMAL: its target and pending timer are set, and with delay 0 it can cause a second transition such as TO_HIGH. A9 also says the escalation check runs at the end of every call. Before, both were skipped: the recovering value never started a pending timer, and an unacknowledged episode could not escalate on that call.
5. `alarm_set_maintenance(off)`: the catch-up notification now goes through `notify_state()` instead of a bare `emit()`. Reason: A10 says a catch-up TO_HIGH/TO_LOW starts a new alarm episode (unacknowledged, alarm time = now, escalation re-armed). Before, an alarm that happened during maintenance was reported but never needed acknowledgement and never escalated. That breaks SP-3.

Added test files (no changes to test infrastructure):
- `review.vec`: 12 regression vectors built from the spec, one or more per defect above. Run: `python3 run_vectors.py acceptance.vec review.vec`.
- `fuzz_model.py`: a separate Python model of SPEC.md that runs random scenarios through `./drv` and compares the output. Run: `python3 fuzz_model.py [seed] [n] [drv]`. The fixed code matched on 40,000+ random scenarios. The original code failed about 53% of them. A build with ASan and UBSan was clean.

## Suspicious-looking behavior deliberately left unchanged

- **Returning to NORMAL keeps the point unacknowledged, and escalation can fire while in NORMAL or FAULT.** This is intended by [POL] A8/A9 (SP-3: every excursion must be seen). Not a bug.
- **TO_FAULT and the recovery TO_NORMAL do not change the ack state.** This is intended by A6a (TO_FAULT is not an alarm).
- **A pending transition whose delay ran out between calls is dropped if the next sample no longer meets the condition.** This is intended by [POL] A5a, which gives exactly this example. Timers only advance through API calls.
- **Fault samples while already in FAULT still update the last value and run the escalation check.** A6's "do nothing" means no transition or notification. A5a, A7 and A9 require the value update and the end-of-call escalation check.
- **`alarm_ack` checks the timer first, so an ack can acknowledge an episode that begins in the same call.** This is the order A5a and A9 specify.
- **Ack is accepted during maintenance, and `maint on` while already on still checks the timer.** The spec does not restrict ack during maintenance. A5a requires the timer check on every call. Only the mode change itself is "nothing".
- **The catch-up notification does not cancel or restart a pending transition.** The spec asks for no such thing. Only real transitions clear the pending transition.
- **`last_notified` starts as NORMAL even though `alarm_init` sends no notification.** So a maintenance window that ends in NORMAL sends no catch-up. This treats NORMAL as the front-end's starting state, which is the only sensible reading of A10 before any notification exists.
- **The configuration is not checked** (negative deadband, `high < low`, NaN limits). The spec has no validation rule, and adding one would change behavior. The header documents `deadband >= 0` as the caller's responsibility.
- **`has_value` is written but never read.** A13 already holds because nothing is pending until the first sample. Harmless dead field, left alone to keep the change small.
- **`emit()` silently drops notifications past `ALARM_MAX_NOTES`.** A call can produce at most 3 (TO_NORMAL, TO_x, ESCALATE), so the cap is never hit.
- **`alarm_t` storage is cast to the internal `point_t`.** The size is checked by `_Static_assert` and the `uint64_t` storage gives enough alignment. This is the usual opaque-handle pattern, and all access goes through `point_t` after init.
