## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|---|---|
| `uint32_t time_delay_normal_s` in `alarm_cfg_t` | Already in `alarm.h` (as delivered). `alarm.c` now uses it. |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | **Implemented** (`delay_for()` in `alarm.c`, used by `eval_timer`). |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | **Implemented** (no change to those paths). |
| `0xFFFFFFFF` = "same as `time_delay_s`" | **Implemented**. |

No conflict found. This is how ASHRAE 135 clause 13.3.6 (OUT_OF_RANGE) works:
returns to NORMAL use pTimeDelayNormal, which is Time_Delay_Normal when present and
Time_Delay when absent. The HIGH_LIMIT↔LOW_LIMIT and NORMAL→offnormal transitions use
pTimeDelay. The sentinel stands for "property absent".

Other changes, kept consistent with the above:
- `SPEC.md` is now v1.1. A5 [STD] states which delay applies to which transition.
  In A5a [POL], "`now − start ≥ time_delay_s`" now reads "≥ the delay of the pending
  target". The timer semantics are otherwise unchanged. This edits a [POL] rule; the CR
  comes from the product owner, and I take that as the required sign-off.
- `tests/unit.vec` has new vectors `y201a`–`y201j` (tag `CR201`). They cover a longer
  and a shorter return delay, the explicit sentinel, HIGH→LOW and LOW→HIGH still using
  `time_delay_s`, a pending NORMAL replaced by a pending LOW, fault recovery staying
  immediate, maintenance catch-up, and the value 0xFFFFFFFE (a real delay, not the
  sentinel). All 54 vectors pass. Seven of the new ones fail against the old code.
- `acceptance.vec` is unchanged. Its scenarios leave `delay_normal` at the default, and
  they still pass.

Unsure / for review:
- **Existing configs:** in C, any `alarm_cfg_t` that is zero-initialised, or built with
  a positional or designated initializer that leaves out the new field, gets
  `time_delay_normal_s = 0`. That means an immediate return to normal, not "same as
  `time_delay_s`". Every place that builds a config (outside this directory) must set
  `0xFFFFFFFF` explicitly. Otherwise the return-to-normal delay changes without anyone
  noticing. It might be safer to make 0 the "same" sentinel, but the header is already
  fixed by the CR, so I did not change it.
- The sentinel means an actual Time_Delay_Normal of 4294967295 s cannot be configured.
  That is about 136 years, so it does not matter in practice. The BACnet object layer
  must still map "property absent" to `0xFFFFFFFF`.
- FAULT transitions (A6 and A6a) and the maintenance catch-up (A10) are still not
  delayed. The fault recovery TO_NORMAL does not use `time_delay_normal_s`. I read the
  CR as covering only the HIGH/LOW→NORMAL limit transitions.
- Existing behaviour, now easier to see: in HIGH, suppose a pending NORMAL has a short
  normal delay that expires between two calls. If the next sample meets the low
  condition, A4 (direct transition wins) replaces the pending NORMAL with a pending LOW,
  and the timer restarts with `time_delay_s`. This follows A4/A5a as written. I did not
  change it.

## CR-202

RAM budget: a smaller `alarm_t`, and the configuration is kept by pointer.

| Item | Status |
|---|---|
| `alarm_t` is 32 bytes (`uint64_t storage[4]`) | **Implemented.** `alarm.h` was already in place (as delivered). `point_t` in `alarm.c` now fits: 32 bytes with 8-byte pointers (host), 28 bytes on Cortex-M0. `_Static_assert`s check its size and alignment against `alarm_t`. I checked the M0 layout with `clang --target=thumbv6m-none-eabi`. |
| `alarm_init` keeps a pointer instead of copying the config | **Implemented.** `point_t.cfg` is now `const alarm_cfg_t *`. The module only reads through it, so a shared config can be `const` (in flash). |
| Caller keeps the `alarm_cfg_t` alive; configs are shared between points | Nothing to do in the module. This is documented in `alarm.h` (as delivered), in the `point_t` comment, and in `SPEC.md`. |
| External behavior must not change | **Met.** All 54 vectors pass (`tests/unit.vec` and `acceptance.vec`), also under ASan/UBSan. A throwaway test ran 512 points sharing one `static const` config, with different histories. The points stayed independent and the config was not written. |

No conflict with `SPEC.md` or with BACnet was found for the items themselves.

Other changes:
- I removed `has_value` from the point state. It was written but never read. A13 still
  holds, because nothing can be pending before the first sample.
- `SPEC.md` is now v1.2. It has a short, informative "Memory" paragraph and no rule changes.
- There are no new vectors, because nothing externally visible changed. The driver has one
  point and one static config, so the vector format cannot express sharing.

Unsure / for review:
- **The RAM budget does not add up.** 512 × 32 bytes = 16,384 bytes. That is all 16 KB,
  and nothing is left for the stack, the BACnet stack or buffers. On M0 the state needs
  only 28 bytes, but `uint64_t storage[4]` forces 32 bytes and 8-byte alignment, which
  wastes 2 KB over 512 points. `uint32_t storage[7]` would be 28 bytes (14 KB). Packing the
  flags into bits would bring it to 24 bytes (12 KB). Either option needs a header change,
  and the CR fixed the header, so I left it as it is. Please confirm the budget.
- **Changing a config while points use it.** With the old copy, a change had no effect
  until `alarm_init` ran again. Now it takes effect at the next call of **every** point
  that shares the config. A pending transition keeps its start time. Its target is
  recomputed at the next sample, but a changed delay applies at once. `SPEC.md` leaves
  this undefined. In BACnet, High_Limit, Low_Limit, Deadband, Time_Delay and
  Time_Delay_Normal belong to each object. If the object layer accepts WriteProperty on
  them, it must not write into a shared config: a write to one object would silently
  change the others (copy-on-write, or one config per distinct set of values). Writes from
  another task or ISR could also be read half-updated in the middle of a call.
- **Dangling pointers.** Any caller that builds the config in a local variable and then
  calls `alarm_init` now fails without any warning. Every `alarm_init` call site outside
  this directory needs to be checked.
- The cast from `alarm_t *` to `point_t *` is the same pattern as before, and I did not
  change it. It is formally a strict-aliasing violation, and `-Wstrict-aliasing=1` warns
  about it. The build (`-O2 -Wall -Wextra`) gives no warnings.

## CR-203

Collected field requests.

| Item | Status |
|---|---|
| 1. `EV_ACKED` notification on `alarm_ack` (product owner) | **Implemented.** |
| 2. Return to NORMAL clears the unacknowledged status and cancels escalation (operators, site Nord) | **Not implemented. It conflicts with A8 [POL] (safety policy SP-3) and with BACnet.** |
| 3. Inclusive limits, `>= high_limit` / `<= low_limit` (operators, site Nord) | **Not implemented. It conflicts with A2 [STD], the BACnet OUT_OF_RANGE algorithm.** |

**Item 1: implemented.**
- `alarm_ack` in `alarm.c`: after the timer evaluation, when the point is unacknowledged,
  it clears the status and emits `ACKED` with `time = now` and `value` = the most recent
  sample value. In maintenance mode the ack still clears the status, but emits nothing.
  An ack when nothing is unacknowledged is still ignored and emits nothing. `alarm.h` was
  already in place (as delivered).
- A8 [POL] said "(no notification)". I changed that rule. The CR comes from the product
  owner, and I take that as the required sign-off. It also matches BACnet, where an
  acknowledgement is reported to the recipients as an ACK_NOTIFICATION.
- `SPEC.md` is now v1.3. A8 describes ACKED. A7 says that ACKED and ESCALATE carry
  `time` and `value` the same way as transitions. A10 says that an ack in maintenance
  clears the status silently and that no catch-up ACKED follows. A11 sets the order to
  transitions, then ACKED, then ESCALATE. An ack call never has both, because the ack is
  applied before the escalation check (A9).
- `tests/unit.vec`: eight existing vectors called `alarm_ack` on an unacknowledged
  alarm. Their expected output now includes the ACKED line: x021, x033, x036, x039, x041,
  x043, x060 and x063 (tag `CR203` added). No other expected line changed. The new
  vectors `y203a`–`y203f` cover these cases: the value is the most recent sample, not the
  alarm value; a second ack does nothing; an ack after ESCALATE; a timer transition and
  ACKED in one ack call (transition first); an ack in maintenance (silent, no catch-up)
  and the next episode's ack; an ack of an episode started by a catch-up TO_HIGH; an ack
  in FAULT (the value can be NaN); an ack with time going backwards (ignored, the alarm
  then escalates). `acceptance.vec` was already updated by the CR. All 60 vectors pass,
  also under ASan/UBSan. Against the old code, all 15 changed or new blocks fail.

**Item 2: not implemented (conflict).**
- A8 [POL] says: "Returning to NORMAL does not clear the unacknowledged status — the
  episode still has to be acknowledged and can still escalate." The rationale is safety
  policy SP-3: every excursion must be seen by a person, even a transient one (the
  freezer/cold-room incident of 2023). The request asks for exactly the opposite. It
  comes from operators, and a [POL] rule may only change with product-owner sign-off.
- It would also go against BACnet. There, a return to normal does not acknowledge the
  TO_OFFNORMAL transition: its `Acked_Transitions` bit stays cleared until an operator
  acknowledges it (AcknowledgeAlarm).
- The escalation the operators describe is the intended behavior. They can acknowledge
  the alarm once it has returned to normal (now with an ACKED notification, item 1).
  If the product owner wants a
  different policy (for example, no escalation after a return to normal while the
  episode stays unacknowledged), that needs a new CR signed off against SP-3.
- The vectors x038 and x041 (tag `CR3clear`) still pin the current behavior.
  x041 now also shows the ACKED.

**Item 3: not implemented (conflict).**
- A2 [STD] requires strict limits: high condition `value > high_limit`, low condition
  `value < low_limit`, and "a value exactly at a limit is *not* offnormal". This follows
  the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6), which uses
  `pCurrentValue > pHighLimit` and `pCurrentValue < pLowLimit`. We need it for our BTL
  listing of intrinsic reporting. With inclusive limits, a device would report an alarm
  that a BACnet workstation and the BTL tests do not expect.
- If site Nord wants 30.0 to alarm, the fix is in the configuration: set `High_Limit`
  just below the value that must alarm (for example 29.9). The same applies to
  `Low_Limit`, set just above.
- The vectors x001 and x002 (tag `CR3incl`) still pin the current behavior.

Unsure / for review:
- **Ack during maintenance.** Following the CR and A10, the ack clears the
  unacknowledged status silently, and leaving maintenance only catches up the state.
  The front-end then never gets an ACKED for that episode, and it may keep showing the
  alarm as unacknowledged, although the point is acknowledged and will not escalate.
  That goes against the A10 rationale ("the front-end must end up showing the true
  state"). Options: a catch-up ACKED on exit from maintenance, or not accepting acks
  during maintenance. Either one is a [POL] change for the product owner. I did not
  make it.
- **Ack of an alarm raised in the same call.** `alarm_ack` evaluates the timer first
  (A5a). If a pending TO_HIGH/TO_LOW expires in that call, the new episode is
  acknowledged at once: `TO_HIGH@t` and then `ACKED@t` (x021, y203b). This was already
  the behavior. With ACKED it is now visible, and an operator seems to acknowledge an
  alarm they could not have seen yet. I did not change it.
- **ACKED in FAULT.** An ack while the point is in FAULT (with the episode from an
  earlier TO_HIGH/TO_LOW still unacknowledged) emits ACKED with the fault sample's
  value, which can be NaN (y203e). This is consistent with A7, but the front-end has to
  accept NaN in ACKED as well.
- ACKED always refers to the current alarm episode, the only one the module tracks. It
  does not map one to one onto BACnet's per-transition `Acked_Transitions` bits
  (TO_OFFNORMAL, TO_FAULT, TO_NORMAL). TO_FAULT and TO_NORMAL still need no
  acknowledgement here (A6a, A8). The BACnet object layer must map this.
