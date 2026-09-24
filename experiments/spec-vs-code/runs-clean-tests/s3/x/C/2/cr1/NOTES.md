## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). No item conflicts with the
BACnet standard: in the OUT_OF_RANGE event algorithm, transitions to NORMAL use
pTimeDelayNormal, and transitions to HIGH_LIMIT/LOW_LIMIT (including HIGH <-> LOW) use
pTimeDelay. When Time_Delay_Normal is absent, Time_Delay is used, which is what the
0xFFFFFFFF value stands for here.

| Item | Status |
|---|---|
| `uint32_t time_delay_normal_s` in `alarm_cfg_t` | Done. It was already in `alarm.h`, which I did not change. `alarm_init` copies it along with the rest of the config. `point_t` still fits in `alarm_t`, and the static assert still holds. |
| HIGH/LOW -> NORMAL uses `time_delay_normal_s` | Implemented in `alarm.c`: the new `delay_for()` function is used by `eval_timer()`. This covers samples, ticks, acks and maintenance calls. |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s` | Implemented, in `delay_for()`. |
| `0xFFFFFFFF` = "same as `time_delay_s`" | Implemented. The value is resolved each time the delay is checked, not once at init. |

Tests: I added `tests/unit.vec` blocks x064 to x069 (tag CR201) and the acceptance
scenario `high-alarm-quick-return-to-normal`. They cover a shorter and a longer normal
delay, HIGH <-> LOW still using `time_delay_s` when `delay_normal=0`, the explicit
0xFFFFFFFF value, the timer restarting when the value bounces back, and FAULT -> NORMAL.
All 51 vectors pass. 5 of the new blocks fail without the change. The other 2 check
behaviour that should not change: the explicit 0xFFFFFFFF value, and FAULT -> NORMAL.

Things I am unsure about or left unchanged:
- FAULT -> NORMAL stays immediate, as before. `time_delay_normal_s` does not apply to it,
  because the CR only names HIGH/LOW -> NORMAL. This matches BACnet, where fault
  transitions have no time delay.
- In HIGH, a value below `low_limit` also meets the return-to-normal condition
  (below `high_limit - deadband`). The same overlap exists in LOW, for a value above
  `high_limit`. The existing precedence is kept: the point goes to the other limit
  after `time_delay_s`. So if `time_delay_normal_s < time_delay_s`, it does not drop to
  NORMAL first. A strict reading of the BACnet algorithm, where conditions (c) and (e)
  overlap, could allow the NORMAL transition first. The standard sets no precedence and
  the existing behaviour (see x013) was not in scope, so I did not change it. Product
  should confirm.
- The pending timer still restarts whenever the pending target changes (existing
  behaviour). The new delay does not change this.
- Because of the 0xFFFFFFFF value, a real Time_Delay_Normal of 4294967295 s cannot be
  configured. If this is exposed as a writable BACnet property, the BACnet layer should
  treat 4294967295 as "not present" or reject it.
