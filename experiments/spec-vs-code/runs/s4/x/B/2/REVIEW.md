# alarm.c review

I reviewed the module against SPEC.md v1.0. All changes are in `alarm.c`. I did not touch `alarm.h`, `driver.c`, `run_vectors.py` or `acceptance.vec`.
Verification: `acceptance.vec` passes (3/3). The new `review_tests.vec` passes (31/31); 15 of its vectors fail on the original code. The fixed code matches an independent model of the spec (`spec_model_fuzz.py`) on 20,000 random scenarios, and it is clean under ASan/UBSan.

## Changes made

1. `target()`, state HIGH: added the low condition (`v < low_limit` gives LOW), checked before the return condition. Reason: A4 requires a direct HIGH to LOW transition, and the direct transition wins over return-to-normal. The old code went HIGH to NORMAL, or stayed HIGH when the deadband was large.
2. `target()`, state HIGH: changed the return condition from `v <= high_limit - deadband` to `v < high_limit - deadband`. Reason: A3 says the return condition is strict. The LOW branch was already strict.
3. `alarm_sample()`, recovery from FAULT: removed the early `return n` after the TO_NORMAL transition. The sample is now evaluated as a normal sample in NORMAL, and the escalation check runs. Reason: A6a says recovery can produce TO_NORMAL then TO_x in the same call (with delay 0) or start a pending timer. A9 says the escalation check runs at the end of every call. The early return skipped both.
4. `alarm_tick()`, `alarm_ack()` and `alarm_set_maintenance()` now use `accept_time()` and return 0 when `now` is older than the latest time seen. Reason: A12 says such a call is ignored entirely. These functions used to run anyway, and the unsigned `now - start` wrapped around. So a stale call could fire a pending transition early, escalate spuriously, acknowledge an alarm or change maintenance mode.
5. `alarm_set_maintenance(off)`: the catch-up notification now goes through `notify_state()` instead of a plain `emit()`. Reason: A10 says a catch-up TO_HIGH/TO_LOW starts a new alarm episode (unacknowledged, alarm time = now, escalation re-armed). The old code sent the notification but left the acknowledgement and escalation state stale.

## Left unchanged on purpose (looks suspicious, but is intended or out of scope)

- Returning to NORMAL does not clear the unacknowledged status, and the episode can still escalate after the return. This differs from BACnet per-transition acknowledgement, but it is [POL] A8 (safety policy SP-3), which needs product-owner sign-off to change.
- `alarm_ack` evaluates the timer before acknowledging. So an alarm raised by that same call's timer evaluation is acknowledged at once. This is the order A5a prescribes (evaluate the timer first, then do the call's own action).
- `alarm_ack` and `alarm_set_maintenance` with nothing to do (nothing unacknowledged, or maintenance already on/off) still evaluate the timer and run the escalation check. A5a and A9 apply to every call; "ignored" and "changes nothing" refer only to the call's own action.
- A fault sample while already in FAULT still updates the stored last value and runs the escalation check. A7 says values include fault samples, and A9 says the check runs at the end of every call.
- `alarm_ack` works during maintenance. The spec does not forbid it.
- The `pending_start` of an expired but cancelled pending transition is never used again. A5a says a transition only happens if a call observes it.
- `emit()` silently drops notifications beyond `ALARM_MAX_NOTES`. The most one call can produce is 3 (recovery: TO_NORMAL + TO_x + ESCALATE), so this is only a safety net.
- The `has_value` field is written but never read. A13 already holds because nothing is pending before the first sample. The field is harmless, so I left it.
- Configuration is not validated (for example a negative deadband or `low_limit > high_limit`). `alarm.h` states `deadband >= 0` as a caller precondition, and the spec defines no error path.
- `alarm_t` is accessed through a cast to `point_t *`. Strictly, that is a type-punning/aliasing concern, but it works with this build (separate translation units, alignment guaranteed by the `uint64_t` storage, size checked by `_Static_assert`). Changing it is a design change, not a bug fix.

## Files added (review aids, not part of the build)

- `review_tests.vec`: spec-derived regression vectors. Run with `python3 run_vectors.py acceptance.vec review_tests.vec`.
- `spec_model_fuzz.py`: an independent Python model of SPEC.md, fuzz-compared against `./drv`.
