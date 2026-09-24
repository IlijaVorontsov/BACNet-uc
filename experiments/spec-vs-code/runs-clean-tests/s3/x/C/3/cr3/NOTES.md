# Notes

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Implemented. The field was already in `alarm.h`; `alarm.c` copies it with the rest of the config in `alarm_init` (`point_t` still fits in `alarm_t`, checked by the `_Static_assert`). |
| HIGH -> NORMAL and LOW -> NORMAL use `time_delay_normal_s` | Implemented (`pending_delay()` in `alarm.c`). This matches BACnet OUT_OF_RANGE transitions (c)/(d), which use pTimeDelayNormal. |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s` | Implemented. This matches BACnet transitions (a)/(b)/(e)/(f), which use pTimeDelay. |
| `0xFFFFFFFF` means "same as `time_delay_s`" | Implemented. This is how the C API says the BACnet property is absent; the standard then uses Time_Delay. |

No item conflicts with the BACnet standard, so every item was implemented.

Tests:
- `tests/unit.vec` has new blocks x064 to x071 (tag `CR201`). They cover:
  - a normal delay shorter than, longer than, and equal to (explicit sentinel) `time_delay_s`
  - `delay_normal=0`
  - HIGH <-> LOW still using `time_delay_s`
  - the pending target switching from NORMAL to LOW
  - maintenance mode
  - FAULT -> NORMAL
- `acceptance.vec` has a new scenario, `high-alarm-returns-after-normal-delay`.
- All existing vectors are unchanged and still pass, because the driver's default is `delay_normal=4294967295`, which gives the same behaviour as before. 53/53 blocks pass.

Open questions:
- FAULT -> NORMAL (the sensor recovering) is still immediate and does not wait for `time_delay_normal_s`. It was never a delayed transition, and BACnet fault transitions have no time delay. My reading of "all other delayed transitions" is that the CR does not cover it.
- Existing behaviour, not changed: in HIGH, if the value moves between the normal band and below `low_limit`, the pending timer restarts each time, and the delay then used is `time_delay_normal_s` or `time_delay_s` depending on the new target. Read literally, BACnet condition (c) ("below high_limit - deadband for pTimeDelayNormal") would keep one timer running across both regions. The same applies in the other direction from LOW. The CR did not ask for a change here, so I left it alone.
- Because of the sentinel, a delay of exactly 4294967295 s (about 136 years) cannot be set for the return to normal. It always means "same as `time_delay_s`". I assume this is acceptable.
- The requirement IDs used as vector tags (A2 to A13) are not documented in this directory. I could only check the CR against the BACnet standard and the existing tests, not against the product requirements text.

## CR-202

RAM budget: a 32-byte `alarm_t`, and the configuration is kept by pointer.

| Item | Status |
|---|---|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Implemented. `alarm.h` was already updated. In `alarm.c`, `point_t` now holds `const alarm_cfg_t *cfg` in place of the embedded 24-byte copy, and the fields are reordered so there is no internal padding. Sizes: 32 bytes on the 64-bit host (8-byte pointer) and 28 bytes on Cortex-M0 (4-byte pointer). Checked with the existing size `_Static_assert` and a new `_Alignof` assert. `alarm.c` also compiles cleanly for `thumbv6m-none-eabi` (clang, freestanding). |
| `alarm_init` keeps a pointer to `cfg` and no longer copies it; the caller keeps it alive and may share it between points | Implemented. All reads of the configuration (`target()`, `pending_delay()`, `check_escalate()`) now go through the pointer. |
| External behaviour must not change | Holds for every existing vector: 53/53 blocks pass (`tests/unit.vec` and `acceptance.vec`). They also pass under ASan and UBSan. No vector was changed or added. The driver keeps a single static `cfg` that outlives the point, and no vector edits `cfg` between `init` and the API calls that follow. |

No item conflicts with the BACnet standard, so every item was implemented. `CR-201` above says `alarm.c` "copies" `time_delay_normal_s` in `alarm_init`. CR-202 supersedes that: the field is now read through the pointer.

Open questions / things I am unsure about:
- **Budget arithmetic:** 512 points x 32 bytes = 16,384 bytes. That is the whole 16 KB of RAM, before the stack, the `alarm_note_t` buffers, globals, and any configurations held in RAM. As specified, the budget cannot be met. On the M0, `point_t` needs 28 bytes. It could drop to 24 bytes if the flags were packed and the write-only `has_value` were removed. With `uint32_t storage[6]`, `alarm_t` would then be 24 bytes, or 12 KB for 512 points. The CR fixes the header at 32 bytes, so I did not change `alarm.h`. The firmware lead should confirm the figures.
- **Behaviour change for callers who modify a configuration after `alarm_init`:** before, the point used a snapshot. Now a change is seen on the point's next call, and by every point that shares that configuration. This includes a point partway through a pending delay: for example, a shorter `time_delay_s` can fire on the next tick. "Must not change" holds only when configurations are not modified after init. The header does not say whether modifying them is allowed. I assumed configurations are effectively constant, for example in flash.
- **Concurrency:** if a configuration can be written from an ISR or another task while a point is being evaluated, one call can see a mix of old and new values. `target()` reads `high_limit`/`deadband` more than once. The caller must serialise configuration updates with the alarm calls.
- **BACnet per-object properties:** in BACnet, High_Limit, Low_Limit, Deadband, Time_Delay, Time_Delay_Normal etc. are properties of each object. If a WriteProperty to one object changed a shared `alarm_cfg_t` in place, every object sharing it would change as well. The integration layer should give that object its own configuration (copy-on-write) before it applies the write. Nothing in this module enforces that. This is not a conflict with the CR itself, because sharing is the caller's choice.
- As in CR-201, the product requirements behind tags A2 to A13 are not in this directory. I checked this CR only against the BACnet standard and the existing vectors.

## CR-203

Collected field requests.

| Item | Status |
|---|---|
| 1. `EV_ACKED` notification from `alarm_ack` (product owner) | Implemented. When `alarm_ack` clears an unacknowledged alarm, it emits ACKED with `time = now` and `value` = the most recent sample value. An ack with nothing unacknowledged emits nothing. In maintenance mode the ack still clears the unacknowledged status, but ACKED is not emitted, and it is not reported when maintenance ends (existing vector x056 already expected this). This matches BACnet, where a successful AcknowledgeAlarm produces an event notification of type ACK_NOTIFICATION. `alarm.h` already had `EV_ACKED = 5`, so it is unchanged. |
| 2. Return to normal clears the unacknowledged status and cancels the escalation (operators, site Nord) | **Not implemented: conflicts with the BACnet standard and with the documented requirements.** In BACnet, a TO_OFFNORMAL event stays unacknowledged (its `Acked_Transitions` bit stays clear) until an operator acknowledges it with AcknowledgeAlarm. A return to normal is a separate TO_NORMAL event and does not acknowledge the earlier alarm. Clearing the status on return to normal would be an automatic acknowledgment, and the operator would never see that the excursion happened. The product requirements say the same thing: vector x038 (A8, A9) and x063 require an alarm that has returned to normal to stay unacknowledged and still escalate, and x041 requires a later ack to clear it. Item 1 also depends on this: acknowledging after a return to normal now emits ACKED. If escalating after a return to normal is a nuisance, the site can acknowledge the alarm, or the product owner can raise a change to requirement A9 (the escalation policy). That change should still leave the alarm unacknowledged. |
| 3. Inclusive limits (`>= high_limit`, `<= low_limit`) (operators, site Nord) | **Not implemented: conflicts with the BACnet standard and with the documented requirements.** The BACnet OUT_OF_RANGE event algorithm goes to HIGH_LIMIT only when the monitored value is *greater than* pHighLimit, and to LOW_LIMIT only when it is *less than* pLowLimit, so a value exactly at the limit is normal. Requirement A2 (vectors x001 and x002) requires the same: 30.0 with `high_limit` 30.0, and 10.0 with `low_limit` 10.0, stay NORMAL. To alarm at 30.0, Nord can set `high_limit` just below the value it wants to alarm on, for example 29.9. |

Changes:
- `alarm.c`: `alarm_ack` emits `EV_ACKED` as described above. There are no other behaviour changes. An ack call emits at most 2 notifications (a pending transition that matures in that call, then ACKED), which is within `ALARM_MAX_NOTES`.
- `acceptance.vec`: the scenario `high-alarm-returns-after-normal-delay` (added by CR-201) still expected a bare `HIGH` for `ack 50`, although that ack clears the unacknowledged TO_HIGH from t=40. CR.md says `acceptance.vec` "has been updated accordingly", but only `high-alarm-ack-and-return` had been updated. I changed that line to `HIGH ACKED@50:35.000`, as item 1 requires.
- `tests/unit.vec`:
  - Existing blocks with an ack that clears an unacknowledged alarm now expect ACKED: x021, x033, x036, x039, x041, x043, x060 and x063. I tagged them `CR203`, and also x056 (ack in maintenance, no ACKED). No inputs were changed.
  - New blocks x072 to x076 (tag `CR203`) cover: ACKED for a LOW alarm after escalation, and a repeated ack; an alarm reached during maintenance, which cannot be acknowledged until it is reported; ACKED with a NaN sample value in FAULT; a pending transition that matures in the ack call being reported before ACKED; and a regression block for the Nord scenario, showing that items 2 and 3 were deliberately not implemented.
- 58/58 blocks pass (`tests/unit.vec` and `acceptance.vec`), also under ASan and UBSan.

Open questions / things I am unsure about:
- **Ack in the same call that raises the alarm (x021):** `alarm_ack` first evaluates the timer and then acknowledges. If the delay matures in the ack call, the alarm is raised and acknowledged at once, and the output is `TO_HIGH@40 ACKED@40`. I kept the existing order, because x021 (A8) requires that no escalation follows. The operator acknowledged an alarm that had not been reported yet, though, and the product owner may want to confirm this.
- **Ack while in FAULT (x033, x074):** the unacknowledged alarm that gets cleared is the earlier HIGH/LOW alarm, but the ACKED value is the most recent sample, which may be the faulty reading (for example `nan` or 0.0). I followed the CR literally ("most recent sample value"). The front-end may prefer the value from the alarm being acknowledged.
- **ACKED dropped during maintenance:** I read "not emitted in maintenance mode" as "dropped". State changes during maintenance are different: they are reported when maintenance ends. An ack made during maintenance is never reported, so the front-end cannot tell that the alarm was acknowledged. x056 (unchanged) expects this.
- **BACnet ACK_NOTIFICATION details:** BACnet acknowledges each transition separately (TO_OFFNORMAL, TO_FAULT, TO_NORMAL), and its ack notification identifies which transition was acknowledged. This module has only one unacknowledged flag (for HIGH/LOW alarms). TO_FAULT and TO_NORMAL never need acknowledgment, so ACKED does not say which transition it is for. That is enough for the current front-end contract, but it is not a full BACnet Acked_Transitions model.
- I could not repeat the CR-202 `thumbv6m-none-eabi` compile check: this environment has no freestanding `math.h`. The change adds no new types or fields, so the size of `point_t` is unchanged.
- As before, the product requirements behind tags A2 to A13 are not in this directory. For items 2 and 3, I inferred the requirements from the tagged vectors (x001, x002, x038, x041 and x063) and from the BACnet standard.
