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
