## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **`time_delay_normal_s` field in `alarm_cfg_t`**: already present in the delivered
  `alarm.h`. No further header change was needed. `alarm_init` copies the whole config, so
  the new field is stored with the rest of it.
- **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented in `alarm.c`
  (`pending_delay()`, used by `eval_timer()`).
- **NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s`**: implemented. This
  matches the BACnet OUT_OF_RANGE event algorithm, where only transitions to NORMAL use
  pTimeDelayNormal and HIGH_LIMIT <-> LOW_LIMIT use pTimeDelay. I found no conflict with
  the standard or with any documented requirement.
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. This stands in for BACnet's
  "Time_Delay_Normal absent, so use Time_Delay". The driver's default for `delay_normal` is
  the same sentinel, so the existing scenarios behave exactly as before.
- **Tests**: added five `cr201-*` blocks to `acceptance.vec`: a shorter normal delay, a
  longer normal delay, HIGH <-> LOW still using `time_delay_s`, `delay_normal=0` (return is
  immediate), and the explicit sentinel. All 8 blocks pass. `driver.c` and
  `run_vectors.py` are unchanged.

Open points:
- FAULT -> NORMAL is still immediate, with no delay, as it was before this CR. The CR
  covers only HIGH/LOW -> NORMAL, so I did not change it.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s cannot be configured.
  Delays that long are not useful in practice.
- If the reading moves between the normal band and the other limit, the pending target
  changes and its timer restarts each time. The delay that applies is always the one for
  the current pending target. This is the same behaviour the module already had.
