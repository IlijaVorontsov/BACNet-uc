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

## CR-202

RAM budget: 32-byte `alarm_t`, and the configuration is kept by pointer.

| Item | Status |
|---|---|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Already in `alarm.h` as the CR said. I did not change `alarm.h`. I reordered `point_t` in `alarm.c` so it has no internal padding. It uses 31 bytes, 32 with tail padding on a 64-bit host (8-byte pointer). On the Cortex-M0 (ILP32) it uses 27 bytes, 28 with padding. I checked the layout with clang for `thumbv6m-none-eabi`, x86-64 and i386. All per-point fields keep their full width. A second `_Static_assert` now checks alignment as well as size. |
| `alarm_init` keeps a pointer instead of copying the configuration | Implemented. `point_t.cfg` is a `const alarm_cfg_t *`, and `target()`, `delay_for()` and `check_escalate()` read the configuration through it. The CR-201 sentinel was already resolved at evaluation time, so nothing is written to the configuration. That is why a shared `const` configuration works. |
| Caller keeps the `alarm_cfg_t` alive; configurations are shared between points | Documented in the `alarm.c` layout and `alarm_init` comments. A two-point test with one shared configuration gave the same results as the same points with private copies. |
| External behavior must not change | Holds. `acceptance.vec` passes 7/7. I also built the pre-change `alarm.c` in a throwaway harness and compared it with the new build on 20,000 random scenarios (samples, faults, NaN/inf, ticks, acks, maintenance, time going backwards, delays of 0 and 0xFFFFFFFF, near-wrap times). The output was identical. An ASan/UBSan build was clean. I deleted the harness afterwards. I added no vectors: the driver drives one point, so it cannot show sharing, and the behavior did not change. |

No conflict found with the documented [STD]/[POL] rules or with BACnet. A1-A13 are
unchanged, and where the configuration is stored is an implementation detail.

Unsure / needs follow-up:
- **The RAM arithmetic does not close.** 512 points x 32 bytes = 16,384 bytes, which is
  the whole 16 KiB of RAM. Nothing is left for the stack, the shared configurations,
  the notification buffers or the BACnet stack. Also, `uint64_t` makes `alarm_t` 8-byte
  aligned, and on the M0 4 of the 32 bytes are unused (2 KiB over 512 points). The
  firmware lead should recheck the budget. For example, `alarm_t` could be sized for
  ILP32. A 64-bit host build needs 32 bytes, and 26 would be possible if the flags were
  packed into bits. I did not change `alarm.h`, because its size is the lead's decision.
- **The configuration must not be modified while points use it.** With the old copy,
  changing the caller's `alarm_cfg_t` after `alarm_init` had no effect. Now the change
  would reach every point that shares it, at once and mid-state. For example, a
  pending HIGH computed under the old `high_limit` could fire on a `tick` under the new
  one, which would break A5's "held continuously". I documented in `alarm.c` that the
  configuration must stay unchanged, and that reconfiguring means re-init with a
  configuration that holds the new values, as before. The `alarm.h` comment only says
  "keeps the configuration alive". Its owner may want to add "and unchanged". Consequence
  for integrators: if one object's limits can be written (for example, a BACnet
  WriteProperty to High_Limit), that point needs its own configuration, not a shared one.
- In the test driver, a `cfg` command between `init` and later calls now affects the
  running point. Before, it had no effect. No existing vector does this.
- `alarm.c` cites SPEC.md, which is not in this directory. If SPEC.md says the
  configuration is copied, its owner needs to update it.
