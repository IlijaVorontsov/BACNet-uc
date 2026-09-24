## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: already present in the
  supplied `alarm.h`; no header change needed. `alarm_init` copies it with the rest
  of the configuration.
- **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented. `alarm.c` now
  picks the delay for the pending transition in `pending_delay()`: a pending
  NORMAL transition (which can only arise from HIGH or LOW) uses
  `time_delay_normal_s`.
- **Other delayed transitions (NORMAL -> HIGH/LOW, HIGH <-> LOW) keep `time_delay_s`**:
  implemented. This is also what the BACnet OUT_OF_RANGE event algorithm specifies
  (pTimeDelay for offnormal and HIGH<->LOW transitions, pTimeDelayNormal only
  for transitions to NORMAL).
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. It corresponds to
  BACnet's "Time_Delay_Normal absent -> use Time_Delay".

No item conflicts with the documented requirements or with the BACnet standard, so
every item was implemented.

Other changes:
- `acceptance.vec`: six new blocks cover a longer and a shorter return-to-normal
  delay, `delay_normal=0`, HIGH<->LOW still using `time_delay_s`, the explicit
  sentinel, and FAULT -> NORMAL. The existing blocks leave `delay_normal` at its
  default (the sentinel), so their behavior is unchanged. All 9 blocks pass.

Open points:
- FAULT -> NORMAL (sensor fault clears) is still immediate and ignores both delays.
  The CR only covers HIGH/LOW -> NORMAL, and in BACnet the fault-clear transition
  is not governed by Time_Delay_Normal either. If the product owner meant
  "every transition to NORMAL", that needs a separate CR.
- The sentinel means 4294967295 s (about 136 years) cannot be configured as a real
  Time_Delay_Normal. BACnet allows any Unsigned value, so a BACnet front-end should
  reject that value on write, or map it to the sentinel knowingly.
- As before, the delay is chosen when the timer is checked, based on the pending
  target. If the pending target changes (e.g. HIGH, value first drops into the
  normal band and then below the low limit), the timer restarts and the new
  target's delay applies. That matches the existing restart-on-new-target behavior.
