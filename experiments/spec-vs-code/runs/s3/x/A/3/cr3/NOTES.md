## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

- **`time_delay_normal_s` field in `alarm_cfg_t`**: already present in the delivered
  `alarm.h`. No further header change was needed. `alarm_init` copies the whole config, so
  the new field is stored with the rest of it. (Superseded by CR-202: `alarm_init` now keeps
  a pointer to the config instead of a copy. The field is still read from there.)
- **HIGH/LOW -> NORMAL uses `time_delay_normal_s`**: implemented in `alarm.c`
  (`pending_delay()`, used by `eval_timer()`).
- **NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s`**: implemented. This
  matches the BACnet OUT_OF_RANGE event algorithm, where only transitions to NORMAL use
  pTimeDelayNormal and HIGH_LIMIT <-> LOW_LIMIT use pTimeDelay. I found no conflict with
  the standard or with any documented requirement.
- **`0xFFFFFFFF` means "same as `time_delay_s`"**: implemented. This stands in for BACnet's
  "Time_Delay_Normal absent, so use Time_Delay". The driver's default for `delay_normal` is
  the same sentinel, so the existing scenarios behave exactly as before.
- **Tests**: added five `cr201-*` blocks to `acceptance.vec`: a shorter normal delay, a
  longer normal delay, HIGH <-> LOW still using `time_delay_s`, `delay_normal=0` (return is
  immediate), and the explicit sentinel. All 8 blocks pass. `driver.c` and
  `run_vectors.py` are unchanged.

Open points:
- FAULT -> NORMAL is still immediate, with no delay, as it was before this CR. The CR
  covers only HIGH/LOW -> NORMAL, so I did not change it.
- Because of the sentinel, a real Time_Delay_Normal of 4294967295 s cannot be configured.
  Delays that long are not useful in practice.
- If the reading moves between the normal band and the other limit, the pending target
  changes and its timer restarts each time. The delay that applies is always the one for
  the current pending target. This is the same behaviour the module already had.

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by pointer.

- **`alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`)**: implemented. The header was
  already updated. In `alarm.c`, `point_t` now holds a `const alarm_cfg_t *` instead of an
  embedded copy, and the three flags (`unacked`, `escalated`, `maint`) are 1-bit bitfields.
  I removed `has_value`: it was written but never read. `point_t` is 24 bytes, 4-byte
  aligned, on Cortex-M0 (checked by cross-compiling with `clang --target=thumbv6m-none-eabi`).
  On the 64-bit host it is 32 bytes, 8-byte aligned. Compile-time asserts check both the
  size and the alignment against `alarm_t`.
- **`alarm_init` keeps a pointer instead of copying the config**: implemented. Every
  config read (`target()`, `pending_delay()`, `check_escalate()`) goes through the pointer.
  The module never writes through it, so one `alarm_cfg_t` can be shared by many points.
- **External behavior must not change**: holds for callers that follow the new contract. I
  checked this in three ways:
  - the existing 8 acceptance blocks pass;
  - a differential fuzz of about 20,000 random scenarios (NaN/inf, sensor faults,
    maintenance, acks, time going backwards, re-init, the `delay_normal` sentinel) gives
    byte-identical output to the pre-CR build;
  - an ASan/UBSan build is clean.

  I added one vector, `cr202-reconfigure-then-reinit`, for the supported reconfiguration
  path. It passes on both the old and the new build. `driver.c` and `run_vectors.py` are
  unchanged. No item conflicts with the BACnet standard or a documented requirement, so
  nothing was refused.

Open points / things I am unsure about:
- **Changing a config while points use it is now visible.** Before, a point used its own
  copy, so later changes to the caller's `alarm_cfg_t` had no effect until `alarm_init`
  was called again. Now a change takes effect on the next call, in **every** point that
  shares that config. For example, the fuzz shows different output when the driver runs
  `cfg` again without `init`. The CR requires the config to outlive the point but does
  not say whether it may be modified. Please add one of these to the contract: "do not
  modify while in use", or "changes apply immediately to all sharing points". A
  half-written config (for example, a `cfg` command that fails to parse after its
  `memset`) would also be seen by live points. Only static or long-lived configs are safe.
  A stack-local `alarm_cfg_t` passed to `alarm_init` would now leave a dangling pointer.
- **BACnet per-object properties.** High_Limit, Low_Limit, Deadband, Time_Delay and
  Time_Delay_Normal belong to each object. A WriteProperty to one object must not change
  other objects that happen to share an `alarm_cfg_t`. The BACnet layer must give that
  object its own config (copy-on-write) before it applies the write. This module cannot
  enforce that.
- **The RAM arithmetic does not close.** 512 points x 32 bytes = 16,384 bytes. That is all
  of a 16 KiB RAM, before the shared configs (24 bytes each), stack, BACnet buffers and the
  rest of the firmware. On Cortex-M0 the implementation uses only 24 bytes per point.
  Sizing `alarm_t` to 24 bytes on the 32-bit target would cut the array to 12 KiB. This
  needs a pointer-size-dependent `storage` in `alarm.h`, because the host build needs
  32 bytes. That header decision belongs to the firmware lead, so I left `alarm.h` as
  delivered.

## CR-203

Collected field requests: an ACKED notification, clearing the unacknowledged status on
return to normal, and inclusive limits.

- **Item 1: `EV_ACKED` notification (product owner)**: implemented in `alarm_ack`. When
  the call clears the unacknowledged status, it emits `ACKED` with `time = now` and
  `value` = the most recent sample value (`last_value`). In maintenance mode the status is
  still cleared, as before, but no `ACKED` is emitted. This matches BACnet, where a
  successful AcknowledgeAlarm is followed by an ACK_NOTIFICATION. I added one comment to
  `EV_ACKED` in `alarm.h`. `point_t` did not change, so it still fits the 32-byte `alarm_t`.
- **Item 2: returning to normal clears the unacknowledged status and cancels the
  escalation (operators, Nord)**: **not implemented, because it conflicts with the BACnet
  standard.** BACnet tracks acknowledgment for each transition (`Acked_Transitions`:
  TO_OFFNORMAL, TO_FAULT, TO_NORMAL). A return to normal is a new TO_NORMAL transition. It
  does not acknowledge the earlier TO_OFFNORMAL transition, which stays unacknowledged
  until an operator acknowledges it (AcknowledgeAlarm). The alarm therefore stays in the
  alarm and event summaries. This is on purpose: operators must see alarms that cleared by
  themselves. Clearing the flag automatically would also mean `ACKED` (item 1) is never
  sent for those alarms. The escalation is our own feature and exists to escalate exactly
  these unacknowledged alarms, so I did not change it separately either. If the product
  owner wants "no escalation once the value is back to normal" while the alarm stays
  unacknowledged, that is a separate decision about our escalation feature. It does not
  conflict with BACnet as long as the unacknowledged status is kept. It should come as its
  own CR. The new vector `cr203-ack-after-return-to-normal` records the current behaviour.
- **Item 3: inclusive limits, `>= high_limit` / `<= low_limit` (operators, Nord)**:
  **not implemented, because it conflicts with the BACnet standard.** The OUT_OF_RANGE
  event algorithm moves to HIGH_LIMIT only when pMonitoredValue is *greater than*
  pHighLimit, and to LOW_LIMIT only when it is *less than* pLowLimit. A value exactly at
  the limit is in range. The existing code already follows this, in both the NORMAL ->
  HIGH/LOW and the HIGH <-> LOW comparisons. Sites that want 30.0 to alarm should set the
  limit just below it (e.g. `High_Limit` 29.9). The new vector `cr203-limits-are-exclusive`
  records the current behaviour.
- **Tests**: I added five `cr203-*` blocks to `acceptance.vec`. They cover:
  - `ACKED` with the latest value, and a second ack emits nothing;
  - an ack in maintenance emits nothing, is not sent later on `maint off`, and still
    prevents escalation;
  - an ack after return to normal;
  - an ack whose own timer evaluation raises the alarm (`TO_HIGH` then `ACKED` in the same
    call);
  - values exactly at the limits.

  All 14 blocks pass, including under ASan/UBSan. A differential fuzz of 12,000 random
  scenarios against the pre-CR build gives identical output once `ACKED` tokens are
  removed. `ACKED` appears only on `ack` calls, at most once per call. `driver.c` and
  `run_vectors.py` are unchanged.

Open points / things I am unsure about:
- **Ack in maintenance mode.** I read "not emitted in maintenance mode" as suppressed, not
  deferred. An ack made in maintenance clears the status silently, and nothing is sent on
  `maint off`. The BMS front-end then never sees `ACKED` for that alarm, though it did see
  the `TO_HIGH`/`TO_LOW`. If the front-end needs every alarm to be closed with `ACKED`,
  either send it on `maint off` or ignore acks during maintenance. Please decide.
- **`ACKED` in the same call as a new alarm.** `alarm_ack` first runs the pending timer. If
  that raises a new alarm, the same ack acknowledges it at once (`TO_HIGH` then `ACKED`,
  same timestamp). The ack clearing that alarm is existing behaviour. I only added the
  notification.
- **Value of `ACKED`.** It is the most recent sample value, as requested. That can be a
  normal-range value (after a return to normal) or the raw value of a sensor-fault sample,
  including `nan`. It is not the value that raised the alarm.
- Items 2 and 3 were requested by site operators. Please send the refusals back to site
  Nord through the product owner.
