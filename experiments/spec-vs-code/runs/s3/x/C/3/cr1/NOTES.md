## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|------|--------|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Implemented. The field was already in the supplied `alarm.h`; `alarm_init` copies it with the rest of the config. |
| HIGH/LOW -> NORMAL uses `time_delay_normal_s` | Implemented (`delay_for()` in `alarm.c`, used by `eval_timer`). |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s` | Implemented, unchanged behaviour. |
| `0xFFFFFFFF` = "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME_AS_DELAY` in `alarm.c`). |

Conflict check: no conflict found. This matches the BACnet OUT_OF_RANGE event
algorithm. Transitions to NORMAL use pTimeDelayNormal, transitions to an
offnormal state (including HIGH_LIMIT <-> LOW_LIMIT) use pTimeDelay, and when
Time_Delay_Normal is absent pTimeDelayNormal = Time_Delay. The sentinel stands
for "absent". There is no requirements document in this directory, so I
checked against the standard and the existing tests only.

Tests: added `tests/unit.vec` x101-x109 (tag CR201) and the acceptance scenario
`high-alarm-quick-return-to-normal`. The driver defaults `delay_normal` to
4294967295, so all existing vectors are unchanged and pass.

Open points:
- Precedence while in HIGH with the value below `low_limit` (and in LOW with
  the value above `high_limit`): the existing rule is kept. The pending target
  is the opposite alarm, timed with `time_delay_s`, and the timer restarts
  whenever the target changes (see x013). So a shorter `time_delay_normal_s`
  does not produce an intermediate TO_NORMAL in that case. A literal reading
  of the BACnet algorithm could produce TO_NORMAL first, because
  "value < high_limit - deadband" also holds. Tell me if you want that.
- FAULT -> NORMAL stays immediate and is not delayed by either delay. This
  matches BACnet, where fault transitions do not use the time delays.
- A real delay of exactly 4294967295 s cannot be configured, because that
  value is the sentinel. In practice this does not matter.
- The sentinel constant is private to `alarm.c`, because I left the supplied
  `alarm.h` as is. It could be made a public macro in `alarm.h` if
  integrators need it.
