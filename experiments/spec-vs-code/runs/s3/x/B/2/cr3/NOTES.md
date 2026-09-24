## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). None of the items conflict
with SPEC.md or with the BACnet standard. The CR matches ASHRAE 135 clause 13.3.6
(OUT_OF_RANGE), where HIGH_LIMIT/LOW_LIMIT → NORMAL uses pTimeDelayNormal and every other
transition, including HIGH_LIMIT ↔ LOW_LIMIT, uses pTimeDelay. A5 is a [STD] rule, so
changing it brings the module closer to the standard and does not touch any [POL] rule.

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` | Implemented. It was already in the delivered `alarm.h`, which I did not modify. `alarm_init` copies the whole config, so the field is stored. `point_t` still fits in `alarm_t` (the `_Static_assert` passes). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented. `delay_for()` in `alarm.c` picks the delay for the pending transition, and `eval_timer` uses it. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep `time_delay_s` | Implemented. This behaviour did not change. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented. The constant `DELAY_NORMAL_SAME_AS_DELAY` is local to `alarm.c`. |

Other changes to keep the artifacts consistent:
- `SPEC.md` is now v1.1. A5 now states which delay applies to each transition and the
  0xFFFFFFFF sentinel. A5a now says "its delay" instead of `time_delay_s`.
- `acceptance.vec` has 7 new `cr201-*` blocks. They cover both returns to normal, a
  return delay of 0, a return delay longer than `time_delay_s`, a direct HIGH→LOW with a
  short return delay, a pending return that is replaced by LOW (the timer restarts with
  `time_delay_s`), the explicit 0xFFFFFFFF value, and fault recovery. All 10 blocks pass.
  With the old code, 4 of the new blocks fail.

Things I am unsure about:
- **Fault recovery.** FAULT→NORMAL (A6a) and the maintenance catch-up (A10) are still
  immediate. They are not delayed by `time_delay_normal_s`. I read the CR as covering only
  HIGH/LOW→NORMAL, and BACnet does not apply Time_Delay_Normal to fault recovery either.
- **Sentinel value.** Because 0xFFFFFFFF is the sentinel, a real return delay of
  4294967295 s (about 136 years) cannot be configured. In BACnet that value is a legal
  Unsigned value for Time_Delay_Normal. An object layer that maps the property to this
  field needs to know this. For an absent property it should write 0xFFFFFFFF.
- **Public constant.** I did not add a named constant for the sentinel to `alarm.h`,
  because the CR says the updated header is already in place. Callers must write the
  literal `0xFFFFFFFF` or `UINT32_MAX`. A `#define` in the header may be worth adding.
- **Existing callers.** Callers that build `alarm_cfg_t` with zero-initialisation (for
  example `= {0}` or `memset`) now get `time_delay_normal_s = 0`. That means an
  **immediate** return to normal, not "same as `time_delay_s`". Every such caller
  outside this directory must set the field to 0xFFFFFFFF explicitly to keep the old
  behaviour. The test driver already does this.

## CR-202

RAM budget: a 32-byte `alarm_t` and the configuration kept by reference. None of the
items conflict with SPEC.md or with the BACnet standard. Memory layout and config
ownership are not covered by any [STD] rule. No [POL] rule A1–A13 changes.

| Item | Status |
|---|---|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Implemented. It was already in the delivered `alarm.h`, which I did not modify. In `alarm.c`, `point_t` now holds a `const alarm_cfg_t *` instead of a copy of the config, and its members are ordered largest first so there are no holes. It is 28 bytes on Cortex-M0 (checked with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0`) and 32 bytes on the 64-bit host. The size `_Static_assert` passes. I added a second `_Static_assert` for alignment. |
| `alarm_init` keeps a pointer instead of copying the config; the caller keeps it alive; configs may be shared | Implemented. The four places that read the config now go through the pointer. SPEC.md is now v1.2 with a new rule A14 for this contract. |
| External behaviour unchanged | Checked, with one caveat (see below). All 10 blocks in `acceptance.vec` pass. I compared the new code with a build of the old code (same sources, larger `alarm_t`) on about 510,000 random driver commands: every output line was identical. The comparison covered limits, deadband, both delays, the 0xFFFFFFFF sentinel, NaN/±inf, faults, ack, maintenance, escalation, time going backwards and time near the uint32 wrap. The run under ASan and UBSan was clean. I added no new vectors: the behaviour did not change, and the driver has only one point and one config, so it cannot test sharing. |

Things I am unsure about:
- **Changing a config after `alarm_init` now behaves differently.** Before, the point
  had its own copy, so later edits to the caller's struct had no effect until the next
  `alarm_init`. Now the point sees the edit. For example, `cfg high=30`, `init 0`, then
  `cfg high=50` without `init`, then `s 1 40`: the old code gives TO_HIGH, the new code
  stays NORMAL. The CR only says the config must *outlive* the point. To keep "external
  behaviour must not change", A14 also says the config must not change while in use:
  to change it, edit it and call `alarm_init` again, which works the same as before.
  I did not define live, in-place changes. Pending transitions and escalation would then
  mix the old and new limits and delays, and a write from another context during a call
  could tear. If live changes are wanted, they need their own CR.
- **Shared configs and BACnet writes.** BACnet `High_Limit`, `Low_Limit`, `Deadband`,
  `Time_Delay`, `Time_Delay_Normal` are per-object properties. If several objects share
  one `alarm_cfg_t`, a WriteProperty to one object must not edit the shared struct. The
  object layer has to give that object its own config (with no heap, from a static pool)
  and re-init it. This is outside this module, but whoever implements config sharing
  needs to know it.
- **The RAM arithmetic leaves no margin.** 512 × 32 B = 16,384 B, which is the whole
  16 KB of RAM. That leaves nothing for the shared configs (24 B each), the stack, the
  BACnet stack or the note buffers. On the M0, `point_t` uses only 28 of the 32 bytes
  (512 × 28 B = 14 KB). Packing the seven 1-byte fields into bit-fields would bring it
  to 24 B. That only saves RAM if `alarm.h` changes, for example to
  `uint32_t storage[6]`, which also drops the 8-byte alignment. I did not change
  `alarm.h`. The firmware lead should check the budget.
- **NULL `cfg`.** It is still not checked, as before. The old code crashed inside
  `alarm_init`. The new code crashes later, at the first call that reads the config.

## CR-203

Collected field requests. I implemented item 1. Items 2 and 3 conflict with the
specification (item 3 also with the BACnet standard), so I did not implement them.

| Item | Status |
|---|---|
| 1. `EV_ACKED` on `alarm_ack` (product owner) | Implemented. When `alarm_ack` clears the unacknowledged status it emits ACKED with `time = now` and `value` = most recent sample value. No ACKED in maintenance mode. An ack when nothing is unacknowledged still emits nothing. This changes A8, a [POL] rule; the request comes from the product owner, which is the sign-off a [POL] change needs. `EV_ACKED = 5` was already in the delivered `alarm.h`, which I did not modify. |
| 2. Return to normal clears unacknowledged / cancels escalation (operators, Nord) | **Not implemented — conflicts with A8 [POL].** A8 says explicitly: "Returning to NORMAL does not clear the unacknowledged status — the episode still has to be acknowledged and can still escalate", with rationale safety policy SP-3 (every excursion must be seen by a person, even a transient one; freezer/cold-room incident 2023). A [POL] rule may only change with product-owner sign-off, and this request comes from operators. The behaviour they describe (ESCALATE 15 min after the alarm although the value is back to normal) is the specified behaviour. It is also in line with BACnet: returning to normal does not acknowledge the TO_OFFNORMAL transition (Acked_Transitions). With item 1, the operators' ack is now visible to the front-end as ACKED, and acking after the return stops the escalation. If site Nord needs something else, it must go to the product owner as a change to SP-3/A8. |
| 3. Inclusive limits `>= high_limit`, `<= low_limit` (operators, Nord) | **Not implemented — conflicts with A2 [STD] and the BACnet standard.** A2 requires strict limits (`value > high_limit`, `value < low_limit`), modelled on the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6: offnormal when the monitored value is *greater than* High_Limit / *less than* Low_Limit). [STD] rules are required for our BTL listing. The reported behaviour (30.0 with `high_limit` 30.0 gives no alarm) is correct. Workaround within the standard: configure the limit slightly inside the range, e.g. `high_limit = 29.9`. |

Other changes to keep the artifacts consistent:
- `alarm.c`: `alarm_ack` emits ACKED after the timer evaluation and before the
  escalation check. At most 2 notes per `alarm_ack` call (one transition + ACKED), so
  `ALARM_MAX_NOTES` is still enough.
- `SPEC.md` is now v1.3. A7 lists ACKED and ESCALATE as the non-transition
  notifications. A8 describes ACKED. A10 says an ack in maintenance clears the
  unacknowledged status silently and that there is no catch-up for it. A11 gives the
  order: transitions, then ACKED, then ESCALATE. A2 and the "return to NORMAL does not
  clear" sentence of A8 are unchanged.
- `acceptance.vec` has 10 new `cr203-*` blocks: ack when nothing is unacknowledged,
  double ack, ack after return to normal (ACKED in NORMAL, no later escalation), a
  transition and ACKED in the same call, ack exactly at the escalation time, late ack
  after ESCALATE, ack in maintenance (silent, no catch-up), ack in FAULT (value NaN), ack
  with time going backwards (ignored, still unacknowledged). Two blocks pin the behaviour
  kept by rejecting items 2 and 3 (value exactly at a limit is normal; return to normal
  still escalates). All 20 blocks pass, also under ASan/UBSan. With the old code, 9
  blocks fail (the updated product-owner block and 8 new ones).

Things I am unsure about:
- **ACKED suppressed in maintenance is lost.** An ack during maintenance clears the
  unacknowledged status, but the front-end never gets ACKED, not even when maintenance
  ends (the A10 catch-up covers only the state). The front-end may keep showing the
  alarm as unacknowledged. I followed the CR literally ("not emitted in maintenance
  mode"). The product owner should decide whether the ack should be kept pending in
  maintenance, or a catch-up ACKED emitted on `maintenance off`.
- **ACKED outside HIGH/LOW.** I read "clears an unacknowledged alarm" as any ack that
  clears the unacknowledged status, so ACKED is also emitted when the point has already
  returned to NORMAL, or is in FAULT (then `value` is NaN, as for other notifications).
- **BACnet mapping.** A BACnet ACK_NOTIFICATION refers to the acknowledged transition
  and its time stamp. ACKED carries only `now` and the latest value, as the CR asks; the
  module does not report the episode's alarm time. If the object layer needs it for
  ACK_NOTIFICATION, it must keep it itself (e.g. from the TO_HIGH/TO_LOW note).
- **Front-end consumers.** Code that handles notifications must accept the new event
  value 5. Anything that assumed `alarm_ack` never returns notes other than transitions
  must be checked.
