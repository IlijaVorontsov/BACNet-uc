# alarm.c review

Reference for "intended behavior": the contract and rule comments in alarm.c (SPEC.md A1-A13), since SPEC.md
itself is not in the tree. All changes are in alarm.c. driver.c, run_vectors.py, alarm.h, Makefile untouched.
Verification: `make && python3 run_vectors.py acceptance.vec regress.vec` -> 19/19 pass (new `regress.vec`
holds one block per fix; 13 of its 16 blocks failed before the fixes). A randomized differential run against an
independent model of A1-A13 (~560k commands, incl. ASan/UBSan build) showed no mismatches, overruns or UB.

## Changes

1. `target()`, HIGH case: added `if (v < low_limit) return ALARM_LOW;` before the return-to-normal test - A4 [STD] requires direct HIGH->LOW that wins over return to normal; it was missing, so HIGH went to NORMAL (and needed a second delay to reach LOW).
2. `target()`, HIGH case: `v <= high_limit - deadband` -> `v < high_limit - deadband` - A3 [STD] return condition is strict (the LOW side already was); a value exactly at high_limit - deadband wrongly cleared the alarm.
3. `alarm_sample()`, FAULT recovery: removed the early `return n;` after the TO_NORMAL transition - A6a says the recovering sample is then evaluated as a normal sample in NORMAL (pending target/timer, possible second transition with delay 0) and every accepted call ends with the A9 escalation check; both were skipped.
4. `alarm_tick()`, `alarm_ack()`, `alarm_set_maintenance()`: use `accept_time()` and return 0 on a backward time - A12 says such calls are ignored with no state change; they ran anyway, so `now - pending_start` / `now - alarm_time` wrapped (unsigned) and caused spurious immediate transitions/ESCALATE, and a stale ack/maint call still changed state.
5. `alarm_set_maintenance()`, on->off catch-up: call `notify_state()` instead of a bare `emit()` - A10/A8 say a catch-up TO_HIGH/TO_LOW starts a new alarm episode (unacked, alarm_time = now, escalation re-armed); the bare emit left it unacknowledgeable/never escalating (or already "escalated" from an old episode).

## Deliberately left unchanged

- TO_NORMAL does not clear `unacked`, so an episode stays unacknowledged and can ESCALATE after the value is back to normal (also while NORMAL/FAULT) - documented [POL] A8 with explicit rationale SP-3.
- A pending transition whose delay expired between calls is cancelled if the next sample has no target; tick/ack/maint do not recompute the target; at most one limit transition per evaluation (a new target after a transition waits for the next sample) - [POL] A5a, no rationale recorded; product-owner decision.
- `alarm_ack()` evaluates the timer before applying the ack, so an alarm raised by that same call is acked immediately without escalating - documented order (A5a/A9).
- Ack is accepted during maintenance (silently clears `unacked`); an escalation suppressed during maintenance fires on the first call after it ends if due - not contradicted by A8-A10 as written; ask product owner if unwanted.
- An excursion that starts and ends inside maintenance leaves no notification and no episode (state equals last notified state on exit) - documented [POL] A10 behaviour.
- Notifications may carry NaN / the value of a fault sample - documented A7.
- `has_value` is written but never read - documented as intentionally unused (A13 holds via `pending`).
- No validation of the configuration (negative/NaN deadband, NaN limits, low_limit > high_limit, in which case HIGH is checked first) - alarm.h states deadband >= 0 as a caller precondition; adding rejection/clamping would change behaviour.
- `alarm_t` storage is accessed through a cast to `point_t`: formally a strict-aliasing violation in ISO C; no warnings, sanitizer-clean and correct with the current compiler/flags. A clean fix (union in alarm.h or `-fno-strict-aliasing`) touches the API/build - flagged for follow-up.
- uint32 seconds wrap after ~136 years; after a wrap A12 would reject every call - out of scope for the product lifetime.
