# alarm.c review

## Changes made (all in alarm.c)

1. `target()`, HIGH state: added `if (v < low_limit) return ALARM_LOW;` before the return-to-normal check. The LOW state already goes straight to HIGH, but HIGH could not go straight to LOW. A value below the low limit was reported as TO_NORMAL (for example "NORMAL TO_NORMAL@10:5.000"), and the LOW alarm needed a second full delay.
2. `target()`, LOW state: the return-to-normal test changed from `v > low_limit + deadband` to `v >= low_limit + deadband`. The HIGH side counts the deadband edge as normal (`<=`), and so does the CR3 inclusive-limit rule (a value at a limit is normal). The LOW side excluded the edge, so with low=10 and deadband=2 a value of 12 stayed LOW for ever.
3. `alarm_sample()`, recovery from FAULT: removed the early `return n` after the FAULT->NORMAL transition. Every other path ends with `check_escalate()`, but this one did not, so an escalation due at the recovery sample was lost. The recovery sample's value was also thrown away: an out-of-range value did not start its time delay until the next sample. The value is now evaluated from NORMAL in the same call.
4. `alarm_tick()`, `alarm_ack()`, `alarm_set_maintenance()`: they now use `accept_time()` and return 0 when `now` is earlier than the latest time seen, as `alarm_sample()` already does. They used to run with the old `now`. The unsigned subtractions `now - pending_start` and `now - alarm_time` then wrapped around, so an old tick could raise an alarm or an ESCALATE at once (for example `s 10 35` then `tick 5` gave TO_HIGH@5, and `tick 30` after an alarm at 40 gave ESCALATE@30). x060 (`ack 30` then `ack 40`) fits the rule that an old call is ignored.
5. `alarm_set_maintenance()`, maintenance off: a state change that happened silently during maintenance is now announced with `notify_state()` instead of a copied `emit()` + `last_notified` update. The copy skipped the alarm bookkeeping: a HIGH or LOW announced when maintenance ended was never marked unacked and got no alarm time. It therefore never needed an ack and never escalated.

Checks run: `make` builds with no warnings, and acceptance.vec plus tests/unit.vec pass 32/32. I also ran focused scenarios for each fix and a random run under ASan/UBSan (no errors, overruns or bad counts). driver.c and run_vectors.py are unchanged.

## Suspicious-looking behavior left unchanged on purpose

- A point stays unacked and still escalates after it returns to NORMAL. This is intended: tests x038, x041 and x063 (tag CR3clear) require it.
- An ACK first runs the timers, so an alarm raised in the same call is acknowledged at once (x021).
- A sample that ends the condition cancels a pending transition even if the delay ran out between samples. Timers are only checked on calls (x019, x020).
- An unacked alarm keeps escalating while the point is in FAULT (x032).
- During maintenance no notes are sent and escalation waits until maintenance ends (x052). Only a net state change is announced when maintenance ends (x048, x051).
- A value exactly at a limit is normal: `>` and `<` are correct (x001, x002, CR3incl).
- Only NaN (or `sensor_fault`) counts as a fault; +/-inf does not. An infinite value compares correctly and raises HIGH or LOW, so it is visible. NaN would otherwise stay NORMAL without anyone noticing. Nothing supports making inf a fault, so changing this would change how points are classified.
- FAULT and NORMAL notes do not set unacked; only HIGH and LOW do. `notify_state` does this on purpose and no test disagrees.
- `alarm_init` does not check `deadband >= 0` or the order of the limits. Validating configuration belongs to the caller (see the header comment); rejecting or clamping values here would change behavior.
- The `has_value` field is written but never read. It is dead but harmless, and leaving it keeps the state layout the same.
- `accept_time()` compares with `now < last_now`, so it does not handle the 32-bit seconds counter wrapping (after about 136 years). The header documents `now` as monotonic.
