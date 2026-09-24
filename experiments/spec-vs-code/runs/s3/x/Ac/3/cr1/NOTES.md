## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Already in `alarm.h` as the CR said. I did not change `alarm.h`. The field fits in `alarm_t`, and the `_Static_assert` still holds. |
| HIGH/LOW -> NORMAL uses `time_delay_normal_s` | Implemented. The new `delay_for()` in `alarm.c` is used by `eval_timer()`. |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep `time_delay_s` | Implemented, in `delay_for()`. |
| `0xFFFFFFFF` = same as `time_delay_s` | Implemented (`DELAY_NORMAL_SAME_AS_DELAY`). The sentinel is resolved at evaluation time, and `alarm_init` still copies the configuration unchanged. |

No conflicts found. The CR matches the BACnet OUT_OF_RANGE algorithm (ASHRAE 135
clause 13.3.6), which rule A5 [STD] is based on. There, a transition to NORMAL uses
pTimeDelayNormal, and offnormal and HIGH_LIMIT <-> LOW_LIMIT transitions use pTimeDelay.
pTimeDelayNormal falls back to Time_Delay when Time_Delay_Normal is absent, which
is what the `0xFFFFFFFF` sentinel does. This comes from my knowledge of the standard;
the standard's text is not in this directory. The CR comes from the product owner, so
the small A5a [POL] wording change ("a delay of 0 means immediately" now covers
whichever delay applies) is covered by the required sign-off. The A5a timer rules
themselves are unchanged: the condition must hold continuously, a missing target
cancels the pending transition, and a transition happens only when a call observes it.

Other changes:
- Updated the `alarm.c` comments: the contract summary, `eval_timer` and the new `delay_for`.
- `acceptance.vec` has four new blocks, `cr201-*`: return to normal after
  `delay_normal`, including cancellation and restart; offnormal and HIGH->LOW still use
  `delay`, even with `delay_normal=0`; the sentinel behaves like `delay`; FAULT
  recovery stays immediate. The two main blocks fail on the old code. The three
  existing blocks still pass: the driver defaults `delay_normal` to `0xFFFFFFFF`.
  Result: 7/7 passed.

Unsure / needs follow-up:
- `alarm.c` cites SPEC.md for rules A5/A5a, but SPEC.md is not in this directory.
  Its A5 text (and the A5a "time_delay_s = 0" wording) needs the same update from its owner.
- Source compatibility: existing callers that zero-initialize `alarm_cfg_t` (memset,
  `= {0}`, or positional/designated initializers without the new field) now get
  `time_delay_normal_s = 0`. That makes the return to NORMAL immediate instead of using
  `time_delay_s`. The CR chose `0xFFFFFFFF` as "same as", so I kept it. All
  integrators must set the field explicitly, or the product owner could use 0 as
  the sentinel instead. Please confirm.
- FAULT -> NORMAL recovery (A6a) is immediate and not a delayed transition, so it
  uses neither delay. I read the CR's "all other delayed transitions" as leaving it
  unchanged.
- A real delay of exactly 0xFFFFFFFF s cannot be set for return-to-normal. This is
  unlikely to matter in practice (it is about 136 years).
