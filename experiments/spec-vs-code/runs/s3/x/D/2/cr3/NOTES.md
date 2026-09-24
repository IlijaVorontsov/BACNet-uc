# NOTES

## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

Conflict check: none found. The change matches ASHRAE 135 OUT_OF_RANGE (13.3.6):
HIGH_LIMIT/LOW_LIMIT to NORMAL is timed by pTimeDelayNormal, while NORMAL to an offnormal
state and HIGH_LIMIT to/from LOW_LIMIT are timed by pTimeDelay. When Time_Delay_Normal
is absent, Time_Delay is used, and the `0xFFFFFFFF` sentinel stands for that. The CR
does not change any [POL] rule; A5a only now points to the per-transition delay. The
product owner raised the CR.

| Item | Status |
|---|---|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Already in the supplied `alarm.h`. `alarm_init` copies it with the rest of `cfg`. `point_t` is still well within `sizeof(alarm_t)` (static assert passes). |
| HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` | Implemented. `pending_delay()` in `alarm.c` is used by `eval_timer()`. |
| NORMAL→HIGH/LOW and HIGH↔LOW keep `time_delay_s` | Implemented, and covered by tests (x066, x067). |
| `0xFFFFFFFF` = "same as `time_delay_s`" | Implemented (`USE_TIME_DELAY` in `alarm.c`). This is also the driver's default (x068). |
| Driver `cfg ... delay_normal=N` | Already in the supplied `driver.c`, which I did not modify. |

Other artifacts updated:
- `SPEC.md` is now v1.1. A5 defines which delay applies to which transition, including the sentinel. A5a says "its delay (A5)" instead of `time_delay_s`. A6a now states that FAULT→NORMAL recovery stays immediate and is not timed by either delay.
- `tests/unit.vec` has new vectors x064 to x071 (tag `CR201`). They cover shorter and longer return delays, the unchanged forward and HIGH↔LOW delays, the sentinel (both explicit and default), cancellation of a pending return (A5a), immediate fault recovery, and escalation while a long return delay is pending. x064, x065, x066, x069 and x071 fail against the old logic. `make` builds cleanly, and `run_vectors.py tests/unit.vec acceptance.vec` gives 52/52.

Open points:
- **FAULT→NORMAL:** I assumed the CR does not cover this transition, because it is not a timed transition (A6a [POL], immediate). The spec now states this explicitly. If the product owner also wants Time_Delay_Normal to apply to fault recovery, that would change the [POL] rule A6a and needs a separate decision.
- **Maintenance catch-up:** the catch-up notification (A10) is unchanged, because it is not a timed transition.
- **Sentinel limits:** because of the sentinel, a real Time_Delay_Normal of 4294967295 s (about 136 years) cannot be set. An object/property layer that exposes the optional Time_Delay_Normal property has to map "absent" to `0xFFFFFFFF`. That layer is outside this module.
- **Supplied header:** `alarm.h` does not name the sentinel. I used a private define in `alarm.c` so I would not have to edit the supplied header. It could be moved to `alarm.h` if integrators need it.

## CR-202

RAM budget: `alarm_t` is 32 bytes, and `alarm_init` keeps a pointer to the configuration.

Conflict check: none found. No [STD] or [POL] rule in SPEC.md says who owns the
configuration. The CR changes memory layout and the `alarm_init` contract, not alarm
behavior. The firmware lead raised the CR.

| Item | Status |
|---|---|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Already in the supplied `alarm.h`, which I did not modify. I reordered `point_t` in `alarm.c` (widest fields first, no internal padding) so it fits: 32 bytes on the 64-bit host and 28 bytes on Cortex-M0. I checked the M0 size with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0`. Before this change `point_t` was 48 bytes and the build failed. The code now also has a static assert on alignment next to the one on size. |
| `alarm_init` keeps a pointer instead of copying the configuration | Implemented. `point_t.cfg` is now a `const alarm_cfg_t *`. |
| Caller keeps the configuration alive, and configurations are shared between points | Supported. The module only reads through the const pointer and never writes to the configuration. Nothing in the module can check the lifetime. The contract is now in SPEC.md §6. |
| External behavior must not change | Verified. `make` builds cleanly. `run_vectors.py tests/unit.vec acceptance.vec` passes 52/52 with no vector changed, including under ASan/UBSan. I added no new vectors. The driver has one point and one static configuration, so it cannot test sharing. Copy and pointer behave differently only if the configuration is changed after `alarm_init`, and that is now outside the contract (see below). |

Other artifacts updated:
- `SPEC.md` is now v1.2. New §6 "Configuration and memory" covers the 32-byte `alarm_t`, the pointer and its lifetime, and sharing. It also says that changing a configuration in place is not specified.

Open points:
- **The RAM arithmetic does not work.** 512 points × 32 B = 16,384 B, which is all 16 KB of RAM. The alarm table alone would use every byte, leaving nothing for the stack, the BACnet stack, buffers, or any configuration kept in RAM. On M0 the module needs only 28 B per point (14,336 B in total). The header fixes `alarm_t` at 32 B, so 2 KB of that table is padding. Please check the budget. Getting below 32 B needs a change to `alarm.h`, for example a smaller per-target `storage`, bit-packed flags, or a configuration index instead of a pointer. That is a separate decision, and I did not do it.
- **Configurations changed in place (BACnet WriteProperty).** In BACnet, High_Limit, Low_Limit, Deadband, Time_Delay and Time_Delay_Normal are properties of each object. If the object layer handles a WriteProperty by writing into a shared `alarm_cfg_t`, every other point sharing it changes too, and those objects would report limits nobody wrote to them. The object layer has to give an object its own configuration before changing one of its limits. Also, before CR-202 a changed configuration took effect only through `alarm_init`. Now an in-place change reaches the point at its next call. For example, a pending transition that was computed with the old limits can still fire on a tick, and a new delay applies to a transition that is already pending. I left this unspecified (SPEC.md §6) and did not define live-update semantics. If live limit writes are needed, that needs a decision.
- **Configurations in flash:** shared configurations can be `const` objects in flash and use no RAM, because the module only reads them.

## CR-203

Collected field requests. I implemented item 1 and declined items 2 and 3, because each of them conflicts with a rule in SPEC.md.

| Item | Status |
|---|---|
| 1. Emit `EV_ACKED` when `alarm_ack` clears an unacknowledged alarm (product owner) | **Implemented.** Conflict check: A8 [POL] said the ack sends "no notification". A [POL] rule can change with product-owner sign-off, and the product owner raised this item. There is no [STD] conflict, because BACnet also reports an acknowledgement with an event notification (ACK_NOTIFICATION). In `alarm_ack` in `alarm.c`, ACKED is emitted with `time = now` and `value` = the most recent sample value. The order in the call is: timer evaluation, then the ack, then the escalation check. ACKED is not emitted in maintenance mode, and it is not caught up when maintenance is turned off. `EV_ACKED` was already in the supplied `alarm.h`, and the driver already prints it. `acceptance.vec` passes. |
| 2. Returning to normal clears the unacknowledged status and cancels escalation (operators, site Nord) | **Not implemented, conflict.** A8 [POL] says: "Returning to NORMAL does not clear the unacknowledged status — the episode still has to be acknowledged and can still escalate." Its rationale is safety policy SP-3: every excursion must be seen by a person, even a transient one. A [POL] rule needs product-owner sign-off, and this request came from operators without it. It also goes against BACnet, where a return to normal does not acknowledge the earlier to-offnormal transition (Acked_Transitions stays unacknowledged until an AcknowledgeAlarm arrives). Vectors x038 and x041 (tag `CR3clear`) cover the current behavior and stay as they are. What the operators can do: acknowledge the episode, which stops escalation and now also shows up at the front-end as ACKED. Changing SP-3 itself would be a decision for the product owner and the safety owner. |
| 3. Inclusive limits `>= high_limit`, `<= low_limit` (operators, site Nord) | **Not implemented, conflict.** A2 [STD] requires strict limits (`value > high_limit`, `value < low_limit`). This follows the BACnet OUT_OF_RANGE algorithm (ASHRAE 135 clause 13.3.6), which our BTL listing for intrinsic reporting depends on. Vectors x001 and x002 (tag `CR3incl`) cover the current behavior and stay as they are. What the operators can do: if a value of exactly 30.0 has to alarm, configure `high_limit` a little below it, for example 29.9. That stays within the standard. |

Other artifacts updated:
- `SPEC.md` is now v1.3. A8 defines ACKED (time, value, in any state including NORMAL and FAULT, and no ACKED when nothing is unacknowledged). A7 lists ACKED and ESCALATE as the notifications that are not transitions. A10 says the ack clears the status in maintenance but emits no ACKED and has no catch-up. A11 gives the order as transitions, then ACKED, then ESCALATE. A2 and A8 now note that CR-203 items 3 and 2 were declined.
- `tests/unit.vec`: eight existing vectors had an ack that clears an unacknowledged episode, so they now expect ACKED: x021, x033, x036, x039, x041, x043, x060 and x063. I changed only those output lines and added the tag `CR203`. New vectors x072 to x076 (tag `CR203`) cover: no ACKED in maintenance and no catch-up afterwards, an ack after ESCALATE with the latest sample value followed by a repeated ack, a NaN value in FAULT, an ack after the catch-up TO_HIGH from maintenance, and TO_NORMAL followed by ACKED in one call. `make` builds without warnings, and `run_vectors.py tests/unit.vec acceptance.vec` gives 57/57, also under ASan/UBSan.
- `alarm.h`, `driver.c` and `run_vectors.py` are unchanged. `alarm_ack` emits at most 2 notifications (one timer transition plus ACKED; no ESCALATE can follow the ack), which is within `ALARM_MAX_NOTES`.

Open points / unsure:
- **Ack during maintenance:** the status is cleared silently. I followed the CR ("not emitted in maintenance mode") and added no catch-up. As a result the front-end never receives ACKED for that episode and may keep showing it as unacknowledged. If the front-end needs a catch-up, A10 has to change, and that needs a decision.
- **ACKED value:** the CR asks for the most recent sample value. The front-end therefore sees the current value, not the alarm value. That value can be a normal reading after a return to normal, or NaN in FAULT (x041, x074).
- **Ack in the same call as a timer transition:** `alarm_ack` evaluates the timer first (A5a). If a delayed TO_HIGH or TO_LOW becomes due on that call, that ack acknowledges the new episode straight away, and the output is TO_HIGH then ACKED (x021). The operator could not have seen that episode before acknowledging it. This behavior was there before CR-203, and I did not change it. It may deserve a look with regard to SP-3.
