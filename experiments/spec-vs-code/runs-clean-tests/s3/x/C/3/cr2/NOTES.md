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
