# alarm.c review

The build is clean with `make`. After the changes, `run_vectors.py acceptance.vec tests/unit.vec` passes 32/32. A 3000-scenario random fuzz under ASan/UBSan showed no errors, no note overruns and no bad counts.

## Changes made (all in alarm.c)

1. `target()`, HIGH state: added `v < low_limit -> ALARM_LOW` before the return-to-normal check. The reason: LOW already goes straight to HIGH, but HIGH could not go straight to LOW. A drop below the low limit passed through NORMAL instead, which produced a spurious TO_NORMAL and started the time delay again.
2. `target()`, LOW state: changed the return-to-normal test from `v > low + deadband` to `v >= low + deadband`. The reason: the HIGH side uses `v <= high - deadband`, and CR3incl treats the boundary value as normal. At exactly low+deadband the point stayed in LOW, so the two sides disagreed.
3. Added `elapsed()` and used it in `eval_timer()` and `check_escalate()`. The reason: `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` accept a `now` older than `last_now` and passed it on as is. `now - pending_start` or `now - alarm_time` then wrapped around to a huge value. So a late tick could fire a transition before its time delay had run out (e.g. `TO_HIGH@5`), or fire a false ESCALATE. A late call now counts as no time elapsed. It is still processed: an ack or a maintenance change still takes effect, and notes still carry the call's `now`.
4. `alarm_sample()`, recovery from FAULT: removed the early `return n` after the FAULT->NORMAL transition. The reason: that call skipped `check_escalate`, so an escalation due at that moment was delayed or lost. It also threw away the valid recovery sample, so an out-of-range value did not start its time delay until the next sample. The point still returns to NORMAL immediately, with TO_NORMAL reported as before, and the sample is then evaluated like any other.
5. `alarm_set_maintenance()`, leaving maintenance: the state is now reported through `notify_state()` instead of an inline `emit`. The reason: the inline copy updated `last_notified` but skipped the alarm bookkeeping (`unacked`, `alarm_time`, `escalated`). A HIGH/LOW alarm first reported when maintenance ended could therefore never be acknowledged or escalated. It could also escalate at once, timed from an older alarm. Maintenance exits that report NORMAL or FAULT behave exactly as before.

## Suspicious-looking behavior left unchanged (intended or out of scope)

- A value exactly at a limit does not alarm (strict `>` / `<`). This is intended (CR3incl, x001/x002).
- An alarm that is still unacknowledged keeps escalating after the point returns to NORMAL. This is intended (CR3clear, x038/x063).
- A new HIGH/LOW notification restarts the escalation timer and clears `escalated`, even if the previous alarm was overdue. This is intended (A11, x046).
- An ack that arrives after the escalation time has passed stops the escalation (the ack is applied before the check). This is intended (x043).
- An ack in the same call that raises the alarm acknowledges that new alarm. This is intended (x021).
- Escalation continues during FAULT and is held off during maintenance, then fires when maintenance ends. This is intended (x032, x052).
- Notes carry the latest sample value, not the value that started the time delay, and ESCALATE carries the current value. This is intended (x034, x038).
- A sample flagged with `sensor_fault` still stores its value, which is used in TO_FAULT/ESCALATE notes. This is intended (x025, x055).
- Only NaN (or `sensor_fault`) counts as a fault. ±inf is treated as an ordinary out-of-range value (HIGH/LOW). Left as is because the code consistently applies the limit rules and nothing specifies that ±inf is a fault. It is worth confirming with the spec owner.
- An out-of-order sample is dropped completely, but out-of-order tick/ack/maintenance calls are still processed (they just do not move `last_now` backwards). This split looks deliberate (x060), so only the time arithmetic was fixed.
- An alarm that clears and re-enters the same state during maintenance is not reported again when maintenance ends, because the state equals the last reported one. This is intended (x048 pattern).
- FAULT is not an acknowledgeable or escalating alarm itself. Nothing indicates otherwise.
- `point_t.has_value` is written but never read. It is dead but harmless, so it was left alone.
- `alarm_init` does not check the configuration (for example deadband < 0 or low > high). The header makes `deadband >= 0` a caller precondition.
- Access through `(point_t *)` on an `alarm_t` is technically type-punning, but it is the intended opaque-storage design. Size is checked by `_Static_assert` and alignment by the `uint64_t` storage.
