# Notes

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **Add `uint32_t time_delay_normal_s` to `alarm_cfg_t`**: done. The field was
  already in `alarm.h`. `alarm_init` copies the whole config, so nothing else was
  needed there. `point_t` still fits in `alarm_t` (checked by the existing
  `_Static_assert`).
- **HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s`**: implemented.
  `alarm.c` now picks the delay from the pending target (`delay_for()`, used by
  `eval_timer()`).
- **All other delayed transitions (NORMAL→HIGH/LOW, HIGH↔LOW) keep `time_delay_s`**:
  implemented.
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented
  (`ALARM_DELAY_NORMAL_SAME` in `alarm.c`).

No conflict found. This matches ASHRAE 135 clause 13.3.6 (OUT_OF_RANGE): the
transitions to NORMAL use pTimeDelayNormal, the transitions to HIGH_LIMIT and LOW_LIMIT
(including HIGH↔LOW) use pTimeDelay, and if Time_Delay_Normal is absent, Time_Delay
is used. No [POL] rule is affected.

Other changes to keep the artifacts consistent:
- `SPEC.md` is now v1.1. A5 [STD] defines the delay per transition. A5a's timer
  evaluation now refers to "the delay of the pending target" instead of `time_delay_s`.
- `tests/unit.vec` has new vectors x042–x046 (tag `CR-201`):
  - different normal and alarm delays in both directions (x042, x043);
  - explicit `0xFFFFFFFF` (x044);
  - a pending return-to-normal replaced by a HIGH→LOW target, which restarts on
    `time_delay_s` (x045);
  - FAULT recovery staying immediate (x046).
  `acceptance.vec` is unchanged and still passes with the default.
- All 49 vectors pass (`make && python3 run_vectors.py acceptance.vec tests/unit.vec`).

Things I am unsure about:
- **FAULT→NORMAL recovery (A6a [POL]) stays immediate** and does not use
  `time_delay_normal_s`. The CR covers only HIGH/LOW→NORMAL, and changing A6a would need
  product-owner sign-off. Vector x046 covers this.
- **A delay is chosen by the pending target, at evaluation time.** When a pending
  target changes (for example, return-to-normal pending, then the value drops below
  `low_limit`), the new pending transition starts at `now` as before (A5a). It uses the
  new target's delay.
- **A real delay of exactly 4294967295 s cannot be set for return-to-normal**, because
  that value is the sentinel. BACnet has no such sentinel: an absent property means
  "use Time_Delay". If the value is ever exposed as a writable BACnet property, the
  object layer must map "absent" to 0xFFFFFFFF and reject or clamp a written value of
  4294967295.

## CR-202

RAM budget: `alarm_t` is 32 bytes and `alarm_init` keeps a pointer to the configuration.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. `alarm.h`
  was already updated. `point_t` in `alarm.c` now holds a `const alarm_cfg_t *`
  instead of a copy of the config. Its members are ordered largest first, so there is no
  internal padding. It is 28 bytes with 4-byte pointers (Cortex-M0; I checked this with
  `clang --target=thumbv6m-none-eabi`) and exactly 32 bytes on the 64-bit host build.
  `_Static_assert` checks the size, and a new `_Static_assert` checks the alignment.
- **`alarm_init` keeps a pointer, not a copy; the caller keeps the config alive and may
  share it between points**: implemented. The module only reads the config. It never
  writes to the config and keeps no per-point data in it. I checked this with a
  throwaway test (two points on one `const` config, one alarming and one not); the test
  is not kept.
- **External behavior must not change**: holds, if the configuration is not modified
  while points use it. All 49 vectors pass unchanged
  (`make && python3 run_vectors.py acceptance.vec tests/unit.vec`). No vector changes a
  config after `init`. No new vectors were added: the driver has one point and one
  config, so it cannot show sharing.

No conflict with SPEC or with ASHRAE 135 was found: neither says how the implementation
stores its configuration. `SPEC.md` is now v1.2, with a paragraph that documents the
memory/configuration contract. No rule changed.

Things I am unsure about:
- **Changing a config at run time is now a behavior change.** Before, a change to a
  config after `alarm_init` had no effect until re-init. Now it affects every point that
  shares the config, on its next call. That includes limits and delays of a transition
  that is already pending (the delay is read when the timer is evaluated). SPEC v1.2
  says to re-initialize points after changing their config. This keeps the old
  behavior, but it also resets their alarm state and acknowledgement. BACnet
  High_Limit/Low_Limit/Deadband/Time_Delay are normally writable. If the object layer
  must apply such writes without re-init, how live changes behave (in particular a
  pending timer) needs its own CR, and possibly review against ASHRAE 135 and [POL]
  A8/A10. If the config is written from another context (ISR/task) while an alarm call
  runs, the call can read a half-updated config. The caller must serialize this.
- **The RAM arithmetic does not close.** 512 points × 32 bytes = 16,384 bytes = the
  whole 16 KiB of RAM. Nothing is left for the shared configs, the stack or the rest of
  the firmware. On the M0 `point_t` needs only 28 bytes, so 4 bytes per point
  (2 KiB in total) are lost to the fixed 32-byte `alarm_t`. It could shrink to 24 bytes
  (e.g. `uint32_t storage[6]`) by packing state/last_notified/pending and the four
  flags into 2 bytes. On the 64-bit host build the pointer is 8 bytes, so that size
  would need a host-specific definition or a config index instead of a pointer. I did
  not change `alarm.h`. The firmware lead should confirm the budget.
- **`alarm_init` still does not validate `cfg`** (NULL is not checked, as before).
  A dangling pointer is now the caller's responsibility.

## CR-203

Collected field requests.

- **Item 1 (product owner): ACKED notification**: implemented. When `alarm_ack` clears
  an unacknowledged alarm, `alarm.c` now emits `EV_ACKED` with `time = now` and
  `value` = the most recent sample value (`alarm.h` already had the enum value). In
  maintenance mode the ack still clears the unacknowledged status but emits nothing. An
  ack when nothing is unacknowledged still emits nothing. ACKED is not a state
  transition, so it does not change `last_notified` (the maintenance catch-up, A10). A8
  is a [POL] rule, and this CR comes from the product owner, so the change has the
  sign-off it needs. It is also in line with ASHRAE 135: an acknowledgement there
  produces an acknowledgement notification too. At most 2 notes per `alarm_ack` call (a
  timer transition, then ACKED), well under `ALARM_MAX_NOTES`.
- **Item 2 (operators, site Nord): return to normal clears unacknowledged status and
  cancels escalation**: **not implemented, conflict.** SPEC A8 [POL] says explicitly
  that returning to NORMAL does *not* clear the unacknowledged status and that the
  episode can still escalate. The rationale is safety policy SP-3: every excursion must
  be seen by a person, even a transient one (freezer/cold-room incident 2023). The
  behavior Nord reports is the specified behavior (vectors x038, x041, x063 check it). A
  [POL] rule can only change with product-owner sign-off, and this request comes from
  operators. It would also drift from BACnet: a return to normal does not acknowledge
  the earlier TO_OFFNORMAL transition there (Acked_Transitions is only set by
  AcknowledgeAlarm). What Nord can do: acknowledge the alarm, which now also shows up
  on the front-end as ACKED (item 1). If they want the policy changed, they need to
  raise it with the product owner and safety (SP-3).
- **Item 3 (operators, site Nord): inclusive limits (`>= high_limit`, `<= low_limit`)**:
  **not implemented, conflict.** SPEC A2 [STD] requires strict limits (`value >
  high_limit`, `value < low_limit`; a value exactly at a limit is not offnormal). This
  follows the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6), and our BTL
  listing depends on it. Vectors x001/x002 check it and still pass. What Nord can do
  with configuration alone: set the limit just inside the range (e.g. `high_limit`
  29.9 instead of 30.0) to get an alarm at 30.0.

Other changes to keep the artifacts consistent:
- `SPEC.md` is now v1.3. A8 describes ACKED. A10 says that ACKED is not emitted in
  maintenance and is not caught up afterwards. A11 now gives the order as transitions,
  then ACKED, then ESCALATE.
- `tests/unit.vec`: the expected output of x021, x033, x036, x039, x041, x043, x060 and
  x063 now includes the ACKED notification. Nothing else in them changed, and they now
  carry the tag `CR-203`. New vectors (tag `CR-203`):
  - x064: ack in maintenance clears the status without ACKED. No ACKED catch-up on
    maintenance off, and no escalation later.
  - x065: ACKED carries NaN after a NaN fault sample. A second ack is ignored.
  - x066: an ack exactly at the escalation time gives ACKED and no ESCALATE. A new
    episode needs its own ack.
  - x067: a timer transition and ACKED in one `alarm_ack` call, in that order.
- `acceptance.vec` (already updated) now passes. All 53 vectors pass
  (`make && python3 run_vectors.py acceptance.vec tests/unit.vec`). `driver.c` and
  `run_vectors.py` are unchanged.

Things I am unsure about:
- **Maintenance mode hides acknowledgements.** If an alarm is acknowledged during
  maintenance, the front-end never gets ACKED, not even when maintenance ends. It keeps
  showing the alarm as unacknowledged, while the module neither escalates it nor
  expects another ack. This is what the CR asks for ("not emitted in maintenance
  mode"). The product owner should confirm that no catch-up ACKED is wanted on
  maintenance off, as there is for the state (A10).
- **An ack can acknowledge an alarm raised in the same call.** `alarm_ack` evaluates
  the timer first (A5a). If that raises TO_HIGH/TO_LOW, the ack acknowledges the new
  episode at once, and the call emits `TO_x` and then `ACKED` (x021, x067). This was
  already the behavior, but ACKED now makes it visible. The operator may have meant the
  previous episode.
- **ACKED's value in FAULT is the fault sample's value**, as the CR asks ("most recent
  sample value"). It can be NaN (x065) or a meaningless fault reading. The front-end
  must handle this, as it already does for TO_FAULT.
- **ACKED is per episode, not per BACnet transition.** An ack clears the whole episode.
  BACnet acknowledges TO_OFFNORMAL, TO_FAULT and TO_NORMAL separately. How the object
  layer maps ACKED to BACnet acknowledgement notifications is outside this module.
- **Duplicate vector ids (existing before this CR).** `tests/unit.vec` has two blocks
  each with ids x042, x043 and x046: the original A9/A11 ones and the CR-201 ones. The
  runner does not mind, but a FAIL line with such an id is ambiguous. I did not
  renumber them, because the CR-201 notes refer to those ids. The new vectors use
  x064–x067.
