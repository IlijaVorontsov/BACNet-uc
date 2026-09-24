## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal). No item conflicts with the
BACnet standard: in the OUT_OF_RANGE event algorithm, transitions to NORMAL use
pTimeDelayNormal, and transitions to HIGH_LIMIT/LOW_LIMIT (including HIGH <-> LOW) use
pTimeDelay. When Time_Delay_Normal is absent, Time_Delay is used, which is what the
0xFFFFFFFF value stands for here.

| Item | Status |
|---|---|
| `uint32_t time_delay_normal_s` in `alarm_cfg_t` | Done. It was already in `alarm.h`, which I did not change. `alarm_init` copies it along with the rest of the config. `point_t` still fits in `alarm_t`, and the static assert still holds. |
| HIGH/LOW -> NORMAL uses `time_delay_normal_s` | Implemented in `alarm.c`: the new `delay_for()` function is used by `eval_timer()`. This covers samples, ticks, acks and maintenance calls. |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s` | Implemented, in `delay_for()`. |
| `0xFFFFFFFF` = "same as `time_delay_s`" | Implemented. The value is resolved each time the delay is checked, not once at init. |

Tests: I added `tests/unit.vec` blocks x064 to x069 (tag CR201) and the acceptance
scenario `high-alarm-quick-return-to-normal`. They cover a shorter and a longer normal
delay, HIGH <-> LOW still using `time_delay_s` when `delay_normal=0`, the explicit
0xFFFFFFFF value, the timer restarting when the value bounces back, and FAULT -> NORMAL.
All 51 vectors pass. 5 of the new blocks fail without the change. The other 2 check
behaviour that should not change: the explicit 0xFFFFFFFF value, and FAULT -> NORMAL.

Things I am unsure about or left unchanged:
- FAULT -> NORMAL stays immediate, as before. `time_delay_normal_s` does not apply to it,
  because the CR only names HIGH/LOW -> NORMAL. This matches BACnet, where fault
  transitions have no time delay.
- In HIGH, a value below `low_limit` also meets the return-to-normal condition
  (below `high_limit - deadband`). The same overlap exists in LOW, for a value above
  `high_limit`. The existing precedence is kept: the point goes to the other limit
  after `time_delay_s`. So if `time_delay_normal_s < time_delay_s`, it does not drop to
  NORMAL first. A strict reading of the BACnet algorithm, where conditions (c) and (e)
  overlap, could allow the NORMAL transition first. The standard sets no precedence and
  the existing behaviour (see x013) was not in scope, so I did not change it. Product
  should confirm.
- The pending timer still restarts whenever the pending target changes (existing
  behaviour). The new delay does not change this.
- Because of the 0xFFFFFFFF value, a real Time_Delay_Normal of 4294967295 s cannot be
  configured. If this is exposed as a writable BACnet property, the BACnet layer should
  treat 4294967295 as "not present" or reject it.

## CR-202

RAM budget: `alarm_t` is now 32 bytes and `alarm_init` keeps a pointer to the configuration
instead of copying it. No item conflicts with the BACnet standard or with the documented
behaviour, so I implemented all of them. There are some caveats below.

| Item | Status |
|---|---|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Done. `alarm.h` was already updated and I did not change it. In `alarm.c`, `point_t` now holds `const alarm_cfg_t *cfg` instead of a 24-byte copy. It is 32 bytes on a 64-bit host (exactly the limit) and 28 bytes on Cortex-M0 (thumbv6m) and on 32-bit x86. The existing `sizeof` static assert holds on all three: the host build, plus compile-only checks for thumbv6m and i386. I added an `_Alignof` static assert, because the struct is overlaid on `alarm_t`. |
| `alarm_init` keeps a pointer and does not copy | Done. `alarm_init` stores `cfg`. `target()`, `delay_for()` and `check_escalate()` read the configuration through the pointer on every call. |
| Caller guarantees the `alarm_cfg_t` outlives the point, and configurations are shared | Documented in `alarm.h` (already there) and in the comment on `point_t` in `alarm.c`. The module does not check it. |
| External behaviour must not change | Met for every input sequence the API could express before. All 51 vectors pass (`tests/unit.vec` and `acceptance.vec`) with no vector changed. I added no vectors, because nothing observable changed through the existing commands. |

This supersedes the CR-201 note "`alarm_init` copies it along with the rest of the config".
`time_delay_normal_s` is now read through the pointer, like every other field.

Things I am unsure about, or that need a decision outside this module:
- **The RAM budget does not close.** 512 points x 32 bytes = 16384 bytes, which is the whole
  16 KB of RAM. Nothing is left for the shared configurations (24 bytes each), the stack or
  the BACnet stack. The implementation needs 28 bytes on Cortex-M0, but the caller allocates
  `sizeof(alarm_t)` = 32. To fit, `alarm_t` must shrink further, for example to 24 bytes
  by packing the state and flags into one word, or by replacing the pointer with a small
  config index. The number of points could also go down. That needs a header/API decision
  from the firmware lead, so I did not change `alarm.h`.
- **The configuration is read live.** With a copy, changes to the caller's `alarm_cfg_t`
  after `alarm_init` were ignored. Now they take effect at the point's next call, for every
  point sharing that configuration. A pending timer is not restarted: the new
  `time_delay_s`/`time_delay_normal_s` is compared against the original `pending_start`,
  so a transition can fire on the next call. No existing test or requirement covers
  changing the configuration after init. I did not pin this behaviour down with a vector.
  Product should decide whether live changes are supported, or whether the configuration
  must stay constant while points use it. Changing it with a re-init of the affected
  points is the equivalent of the old behaviour.
- **Concurrency.** If the configuration can be written from another context (a
  BACnet WriteProperty task or an ISR) while an `alarm_*` call runs, the call may see a
  half-updated configuration. The caller must serialize those writes with the alarm calls.
- **BACnet object model.** Each BACnet object has its own High_Limit, Low_Limit, Deadband,
  Time_Delay and Time_Delay_Normal. If the BACnet layer maps these properties onto a shared
  `alarm_cfg_t`, a WriteProperty to one object would change every object that shares it.
  That would violate the standard. The BACnet layer must give the object its own
  configuration before writing (copy-on-write). This does not affect this module.
- `alarm_init` with a NULL `cfg` used to crash at init. Now it crashes at the first
  sample/tick/ack/maintenance call. Passing NULL was never allowed.

## CR-203

I implemented item 1. I did not implement items 2 and 3, because each conflicts with the
BACnet standard and with documented, tested behaviour. Details are below.

| Item | Status |
|---|---|
| 1. `EV_ACKED` notification on `alarm_ack` | **Implemented** in `alarm_ack()` in `alarm.c`. When the call clears an unacknowledged alarm, it emits `ACKED` with `time = now` and `value` = the most recent sample value. In maintenance mode the alarm is still acknowledged (existing behaviour, x056), but no `ACKED` is emitted. This matches BACnet, where acknowledging an alarm produces an ACK_NOTIFICATION. `alarm.h` already had `EV_ACKED = 5`, and I did not change it. |
| 2. Return to normal clears "unacknowledged" and cancels escalation (site Nord) | **Not implemented: conflict.** See below. |
| 3. Inclusive limits (`>= high_limit`, `<= low_limit`) (site Nord) | **Not implemented: conflict.** See below. |

**Why item 2 conflicts.** In BACnet, acknowledgement is tracked per transition
(`Acked_Transitions`: TO-OFFNORMAL, TO-FAULT, TO-NORMAL). A TO-OFFNORMAL transition stays
unacknowledged until an operator acknowledges it (AcknowledgeAlarm). A later return to
normal does not acknowledge it. The product's documented behaviour (requirements A8/A9)
says the same thing: an alarm that was never acknowledged keeps escalating after the value
returns to normal. The vectors pin this down: `x038` (`NORMAL ESCALATE@110`), `x063`
(`NORMAL ESCALATE@400`) and `x041` (acknowledging after return to normal stops the
escalation). If the module cleared the status automatically, nobody would know that
operators never saw the alarm. Operators can stop the escalation by acknowledging the
alarm, and they now get an `ACKED` confirmation. If the product owner wants returned
alarms not to escalate, while keeping them unacknowledged, that changes requirement A9. It
needs a decision from the product owner, not a site request.

**Why item 3 conflicts.** The BACnet OUT_OF_RANGE event algorithm uses strict comparisons.
It goes offnormal when the monitored value is *greater than* High_Limit, or *less than*
Low_Limit. Requirement A2 documents the same thing, and the vectors test it: `x001` (30.0
with high 30 stays NORMAL), `x002` (10.0 with low 10 stays NORMAL) and `x008`. If site Nord
needs 30.0 to alarm, the BACnet way is to configure a slightly lower High_Limit, for
example 29.9, or a slightly higher Low_Limit. Keep float resolution in mind when choosing
the value.

Tests: 55/55 vectors pass (`tests/unit.vec` and `acceptance.vec`). The acceptance scenario
`high-alarm-ack-and-return` had already been updated, and it now passes.
- I updated the expected output of 8 existing `tests/unit.vec` blocks, and added the tag
  `CR203` to each: x021, x033, x036, x039, x041, x043, x060 and x063. The only change in
  each is the new `ACKED` note on the `ack` line. Nothing else in their output changed.
- I added x070 to x073 (tag CR203). They cover: acknowledging a LOW alarm, where the value
  is the latest sample and not the alarm value; acknowledging after `ESCALATE`, where a
  second ack emits nothing; acknowledging in maintenance mode, where nothing is emitted,
  nothing is emitted later at `maint off`, and there is no escalation; and an alarm raised
  during maintenance, which is acknowledgeable only after `maint off` notifies it. x072
  (the maintenance suppression case) passes without the change. The other new blocks, and
  all 8 updated ones, fail without it.
- `driver.c`, `run_vectors.py`, `alarm.h` and `acceptance.vec` are unchanged.

Things I am unsure about:
- **ACKED during maintenance is dropped, not deferred.** The CR says ACKED "is not emitted
  in maintenance mode". So an ack during maintenance clears the alarm silently, and
  `maint off` does not report it later. State changes are handled differently: they are
  reported at `maint off`. The front-end therefore never sees an ACKED for an alarm it was
  told about before maintenance started. It still sees that the alarm stops escalating.
  Product should confirm this is intended.
- **Value while in FAULT.** "Most recent sample value" includes the value of a
  sensor-fault sample. For example, x033 reports `FAULT ACKED@20:0.000`, and a NaN sample
  would give `nan`. This matches how `ESCALATE` already reports its value (x032).
- **Order within one call.** `alarm_ack` still runs the pending timer first. If an alarm
  becomes due at the moment of the ack, the call emits `TO_HIGH`/`TO_LOW` followed by
  `ACKED` (x021). The operator therefore acknowledges an alarm they have not seen yet. This
  was already the behaviour; only the notification is new. BACnet acknowledgements name
  the event's timestamp, so this cannot happen there, but `alarm_ack()` takes no timestamp
  argument. Changing this would need an API change.
- **Mapping to BACnet.** A BACnet ACK_NOTIFICATION carries the acknowledged transition and
  its timestamp, not a present value. The front-end or BACnet layer has to map `ACKED`
  onto the TO-OFFNORMAL transition. This module does not record which transition was
  acknowledged (HIGH or LOW), because it is always the most recent one.
