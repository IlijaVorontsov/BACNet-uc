## CR-201

Separate return-to-normal delay (Time_Delay_Normal).

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Already in `alarm.h` as delivered; used unchanged. `point_t` grows by 4 bytes and still fits in `alarm_t` (the static assert still holds). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | **Implemented** (`delay_for()` in `alarm.c`, used by `eval_timer`). |
| NORMAL→HIGH/LOW and HIGH↔LOW keep using `time_delay_s` | **Implemented.** |
| `0xFFFFFFFF` = "same as `time_delay_s`" | **Implemented** (`DELAY_NORMAL_SAME` in `alarm.c`). |

No conflicts found. The CR matches BACnet OUT_OF_RANGE (135 clause 13.3.6). pTimeDelayNormal
applies to the HIGH_LIMIT/LOW_LIMIT→NORMAL transitions. pTimeDelay applies to the others,
including the direct HIGH↔LOW ones. When Time_Delay_Normal is absent, Time_Delay is used,
and the sentinel stands for that case. No [POL] rule changes meaning. The CR comes from the
product owner.

Other artifacts updated:
- `SPEC.md` is now v1.1. A5 [STD] now defines the delay for each transition. A5a now
  refers to "the applicable delay (A5)" instead of `time_delay_s`, and the timer semantics
  are otherwise unchanged. A5 also says that FAULT→NORMAL stays immediate (A6a,
  unchanged). With `time_delay_s = 0` the A6a example of a second transition still holds.
- `tests/unit.vec` has new blocks x070–x078 (tag `CR-201`). They cover a longer delay,
  a shorter delay and a zero delay on return. They check that HIGH↔LOW and NORMAL→offnormal
  still use `time_delay_s`, and that the explicit sentinel works. They also cover
  cancel/restart of a pending return, immediate FAULT→NORMAL, and `0xFFFFFFFE` as a real
  delay. The last block covers maintenance catch-up and escalation with a delayed return.
- `acceptance.vec` is unchanged. The driver defaults `delay_normal` to the sentinel, so the
  existing scenarios keep their meaning. All 53 blocks pass.

Unsure / to flag:
- **Zero-initialised configs change behaviour.** Existing integration code might build
  `alarm_cfg_t` with `memset(…, 0, …)`, `= {0}`, or designated initialisers that omit the
  new field. In that code `time_delay_normal_s` is 0, not the sentinel. Returns to NORMAL
  then become immediate instead of following `time_delay_s`. Every caller must set the
  field explicitly, using `0xFFFFFFFF` for the old behaviour. A named constant in
  `alarm.h` might help. I did not add one because the header was delivered as final.
- The sentinel takes one value from BACnet's Unsigned range. If the object layer writes a
  Time_Delay_Normal of exactly 4294967295, it is read as "absent / same as Time_Delay".
  That is harmless in practice (about 136 years), but the BACnet property mapping must
  write the sentinel when the property is absent and must not pass 4294967295 through
  unchanged.
- The delay is chosen from the *pending target*. A pending NORMAL target only exists in
  HIGH or LOW. If the configuration changed while a transition was pending, the new delay
  would apply to the running timer. That cannot happen today because `alarm_init` is the
  only way to set the configuration.

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by reference.

| Item | Status |
|---|---|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | `alarm.h` already had it and I used it unchanged. **Implemented** in `alarm.c`: `point_t` now holds a `const alarm_cfg_t *` instead of a 24-byte copy, with the members reordered largest first. It is 28 bytes on Cortex-M0 (ILP32 AAPCS, checked with clang `--target=thumbv6m-none-eabi`) and exactly 32 bytes on the LP64 test host. Static asserts check both size and alignment against `alarm_t`. |
| `alarm_init` keeps a pointer, no copy | **Implemented.** Every access now goes through `p->cfg->…`. |
| Caller keeps the `alarm_cfg_t` alive; configs are shared between points | **Implemented / documented.** The module only reads the config, so points can share one. `SPEC.md` is now v1.2 and has a new "Configuration (API contract)" paragraph: the config must stay alive, at the same address and **unchanged**, while any point uses it, and changing a point's config still means calling `alarm_init` again. No [STD]/[POL] rule changed meaning. |
| External behaviour must not change | **Holds** as long as the contract above is kept. All 53 vector blocks pass without edits to `tests/unit.vec` or `acceptance.vec`, also under ASan/UBSan. A throwaway check (not kept) ran two points on one shared config against two points with private copies and got identical outputs. |

No conflict with SPEC or BACnet was found, so I implemented every item. The CR comes from the
firmware lead, not the product owner. That is fine here because no [POL] rule changes.

Unsure / to flag:
- **The RAM budget does not close.** 512 × 32 B = 16,384 B, which is *all* of a 16 KiB part (and
  more than 16,000 B). Nothing would be left for the shared configs (24 B each), stack, the
  BACnet stack or notification buffers. On the M0 the state needs only 28 B. With
  `alarm_t` = 28 B the points would take 14 KiB, which is probably still too much. `has_value` is
  written but never read and could be dropped, and the flags could become bit-fields
  (about 24 B → 12 KiB). But `sizeof(alarm_t)` is fixed by the delivered header. The firmware
  lead has to decide on this. I did not change `alarm.h`.
- **No spare room on the host.** With an 8-byte pointer, `point_t` fills all 32 bytes on
  LP64. The next field anyone adds will break the host/test build (static assert) even
  if it still fits on the M0.
- **The shared config must not be modified in place.** Before this change, a point used
  a snapshot. Now any write to the struct reaches every point that shares it on its next
  call. That includes running timers: the CR-201 note "that cannot happen today" is no
  longer true if a caller writes the struct. A write also becomes visible half-done if the
  alarm calls run in another task or an ISR (for example `high_limit` updated but
  `deadband` not yet). BACnet limit/delay properties belong to each object separately. A
  WriteProperty to one object's High_Limit must therefore not write a shared config.
  The object layer has to give that object its own config, with static lifetime, and
  re-`alarm_init` the point as before. If live updates of shared configs *are* intended,
  that is a behaviour change and needs its own CR and SPEC rule. I suggest adding "must
  not be modified while in use" to the `alarm.h` comment. I left the delivered header as
  it was.
- **Lifetime is only by contract.** Existing callers that build the config in a local
  variable or a temporary and then call `alarm_init` now leave a dangling pointer (UB)
  that the compiler will not catch. All call sites need to be checked. The test `driver.c`
  uses a static config, so it is fine. But a `cfg` line that is not followed by `init` now
  changes the live point. No vector does this.
- The CR-201 warning about zero-initialised configs (`time_delay_normal_s = 0`) still applies,
  now for every point that shares such a config.

## CR-203

Collected field requests. Items 2 and 3 conflict with the SPEC, so I implemented item 1
only.

| Item | Status |
|---|---|
| 1. `EV_ACKED` on `alarm_ack` (product owner) | **Implemented** (`alarm_ack` in `alarm.c`). When an ack clears an unacknowledged episode it emits ACKED with `time = now` and `value` = most recent sample value. The order is: transition from the ack's own timer evaluation first, then ACKED, then the escalation check (A11), which can no longer fire because the ack has already cleared the status. An ack with nothing unacknowledged emits nothing. An ack whose time goes backwards emits nothing (A12). In maintenance the ack still clears the status as before, but ACKED is not emitted. This changes the [POL] rule A8 ("no notification"). That change is allowed because the request comes from the product owner. BACnet has the same idea (the ACK_NOTIFICATION sent after AcknowledgeAlarm), so there is no conflict with the standard. `alarm.h` already had the enum value and I did not change it. |
| 2. Return to NORMAL clears unacknowledged / cancels escalation (operators, Nord) | **Not implemented: conflict.** A8 [POL] says explicitly that returning to NORMAL does *not* clear the unacknowledged status and that the episode can still escalate. The rule comes from safety policy SP-3: every excursion, even a transient one, must be seen by a person (freezer/cold-room incident 2023). The operators are describing that rule as a bug. A [POL] rule may only change with product-owner sign-off, and this request comes from site operators. BACnet also treats acknowledgement as an explicit operator action (AcknowledgeAlarm). A TO_NORMAL transition does not acknowledge the earlier TO_OFFNORMAL transition. If the escalation is a nuisance, the product owner would have to change SP-3/A8, or the site can use a longer `escalate_after_s` or 0 = off. That per-site configuration choice should also be checked with the product owner. Operators can also acknowledge sooner, and they now get ACKED feedback from item 1. |
| 3. Inclusive limits (`>= high_limit`, `<= low_limit`) (operators, Nord) | **Not implemented: conflict.** A2 [STD] requires strict limits (`value > high_limit`, `value < low_limit`), following the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6), which we need for the BTL listing of intrinsic reporting. A value exactly at High_Limit is normal under the standard. To get the effect they want, the site can configure the limit slightly inside the range they care about, for example `high_limit` 29.9 instead of 30.0. |

Other artifacts updated:
- `SPEC.md` is now v1.3. A8 describes ACKED: value, time, an ignored ack emits nothing,
  and maintenance clears the status silently. A7 says that ACKED and ESCALATE are not
  state transitions. A10 says that maintenance also suppresses ACKED and that ACKED has
  no catch-up. A11 gives the order as transitions, then ACKED, then ESCALATE. A2 and the
  "returning to NORMAL does not clear" sentence of A8 are unchanged.
- `tests/unit.vec`: I updated the expected output of the existing ack blocks x021, x033,
  x036, x039, x041, x043, x060 and x063, which now show ACKED, and added the tag
  `CR-203`. The inputs are unchanged. x056 (ack in maintenance) and x062 (ack before any
  sample) are unchanged. New blocks:
  - x080: ack in maintenance, with no catch-up and no escalation afterwards.
  - x081: ACKED after the ack's own timer-driven TO_NORMAL; the value is the most recent
    sample, not the alarm value; a new episode re-arms ACKED.
  - x082: ack in FAULT, where the value is NaN.
  - x083: ack after ESCALATE.
  - x084: pins the unchanged behaviour for items 2 and 3.
- `acceptance.vec` was delivered already updated, and I did not edit it. All 58 blocks pass
  (`make && python3 run_vectors.py tests/unit.vec acceptance.vec`), also under
  ASan/UBSan. `point_t` is unchanged, so there is no RAM impact.

Unsure / to flag:
- **Ack during maintenance is invisible to the front-end.** An ack in maintenance clears
  the episode, as it always has (x056), and emits no ACKED. Nothing is caught up when
  maintenance ends. The front-end may therefore keep showing an alarm as unacknowledged
  that the module considers acknowledged. The CR says only that no notification is
  emitted in maintenance, so I kept A10 literal. The product owner should decide between
  two options: a catch-up ACKED on maintenance off, or ignoring acks in maintenance.
- **ACKED value.** The value is the most recent sample, as the CR says, not the value
  that raised the alarm. After a return to NORMAL (x041, x084) it is the current normal
  value. In FAULT it can be NaN (x082). The front-end must accept a NaN value in ACKED.
- **Front-end compatibility.** Every existing ack of an unacknowledged alarm now produces
  a notification with event code 5. Front-end or BMS gateway code that rejects unknown
  event codes must be updated before this firmware ships.
- A late ack (after `escalate_after_s`, with no call in between) emits ACKED and no
  ESCALATE (x043), because A9 applies the ack before the escalation check. I kept that
  behaviour.
- Items 2 and 3 should go back to the requesters (site Nord) with the explanations
  above. Item 2 should also go to the product owner if the site wants SP-3 revisited.
