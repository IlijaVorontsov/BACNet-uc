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

## CR-203

Collected field requests.

| Item | Status |
|---|---|
| 1. `EV_ACKED` when `alarm_ack` clears an unacknowledged alarm (product owner) | Implemented. `EV_ACKED` was already in `alarm.h`, and I did not change `alarm.h`. `alarm_ack` now emits ACKED with `time = now` and `value` = the most recent sample value, which can be NaN after a fault sample (same as A7). An ack when nothing is unacknowledged emits nothing. In maintenance mode the ack still clears the status, but no ACKED is emitted. |
| 2. Return to normal should clear "unacknowledged" and cancel escalation (operators, site Nord) | **Not implemented: conflict.** See below. |
| 3. Inclusive limits, `>= high_limit` / `<= low_limit` (operators, site Nord) | **Not implemented: conflict.** See below. |

Item 1 changes rule A8 [POL]: an ack used to clear the status "with no notification". The
request comes from the product owner, so it has the sign-off a [POL] change needs.
ACKED is not a transition, so A7 ("every transition emits one notification") is unchanged.
The A11 order is now: transitions, then ACKED, then ESCALATE. ACKED and ESCALATE never
occur in the same call, because the ack is applied before the escalation check (A9).
An ack call can emit at most 2 notifications (one timer transition plus ACKED), which is
within `ALARM_MAX_NOTES`. I found no conflict with BACnet: as far as I know, BACnet also
reports acknowledgements to recipients (ack notifications). The standard's text is not
in this directory, so this is from my knowledge.

Item 2 conflicts with A8 [POL], which has a recorded rationale: safety policy SP-3.
Every excursion must be seen by a person, even a transient one (freezer/cold-room
incident 2023). A TO_HIGH/TO_LOW episode therefore stays unacknowledged after the
return to NORMAL, and it escalates under A9 if nobody acknowledges it. The request comes
from operators, not the product owner. Changing a [POL] rule needs product-owner
sign-off, and this one also needs the safety policy to change. It would also differ
from BACnet, where an offnormal transition stays unacknowledged (Acked_Transitions)
after the object returns to normal (from my knowledge of the standard). Options for
site Nord within the current rules: acknowledge the episode, which now gives visible
ACKED feedback (item 1), or have the product owner review `escalate_after_s` for their
points (it is configuration, not code). The product owner should reply to site Nord.

Item 3 conflicts with A2 [STD] and with the BACnet OUT_OF_RANGE algorithm (ASHRAE 135
clause 13.3.6), which our BTL listing of intrinsic reporting depends on. There, HIGH_LIMIT
requires pMonitoredValue > pHighLimit and LOW_LIMIT requires pMonitoredValue <
pLowLimit, so a value exactly at a limit is not offnormal (from my knowledge of the
standard). Workaround by configuration: set `high_limit` slightly below the value that
must alarm, for example 29.9 with a sensor resolution of 0.1, and set `low_limit`
slightly above. The return threshold (limit -/+ deadband) moves by the same amount.

Other changes:
- `alarm.c`: the ACKED emission in `alarm_ack`. Updated comments: the contract summary
  (A8/A11), `alarm_ack` and the maintenance (A10) comment. Short notes at A2 (`target()`)
  and A8 (`notify_state()`) record that items 3 and 2 were rejected and why.
- `acceptance.vec`: the `high-alarm-ack-and-return` block was already updated by the CR.
  I added five `cr203-*` blocks: ACKED suppressed in maintenance, with no ACKED or
  ESCALATE afterwards; ACKED only when something is unacknowledged, and after a
  transition in the same call; ACKED carrying a NaN fault-sample value; an episode that
  stays unacknowledged after return to normal, escalates and is then acknowledged (the
  unchanged behavior behind item 2); strict limits at exactly 30.0 / 10.0 (item 3).
  Result: 12/12 passed. An ASan/UBSan build also passes 12/12.
- `alarm.h`, `driver.c` and `run_vectors.py` are unchanged.

Unsure / needs follow-up:
- **An ack during maintenance is never reported.** It clears the status silently, and
  the A10 catch-up covers only the state. The front-end keeps showing an episode it
  saw as TO_HIGH/TO_LOW as unacknowledged, while the point considers it acknowledged.
  I followed the CR ("not emitted in maintenance mode"). The product owner may want a
  catch-up ACKED when maintenance ends, or acks refused during maintenance. Please
  confirm.
- If a pending transition fires in the ack call itself (for example `ack 70` gives
  `TO_HIGH@70 ACKED@70`), the ack acknowledges an episode the operator has not seen yet.
  The code did this before this CR. ACKED now makes it visible. The product owner
  should confirm this is intended.
- `alarm.c` cites SPEC.md, which is not in this directory. Its owner needs to update A8
  (the ack now emits ACKED), A10 and A11, and the list of notification types.
- Integrators: front-end code that switches on `alarm_event_t` has to handle the new
  value 5.
