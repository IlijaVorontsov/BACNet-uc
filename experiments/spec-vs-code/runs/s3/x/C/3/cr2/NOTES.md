## CR-201

Separate return-to-normal delay (BACnet Time_Delay_Normal).

| Item | Status |
|------|--------|
| Add `uint32_t time_delay_normal_s` to `alarm_cfg_t` | Implemented. The field was already in the supplied `alarm.h`; `alarm_init` copies it with the rest of the config. |
| HIGH/LOW -> NORMAL uses `time_delay_normal_s` | Implemented (`delay_for()` in `alarm.c`, used by `eval_timer`). |
| NORMAL -> HIGH/LOW and HIGH <-> LOW keep using `time_delay_s` | Implemented, unchanged behaviour. |
| `0xFFFFFFFF` = "same as `time_delay_s`" | Implemented (`DELAY_NORMAL_SAME_AS_DELAY` in `alarm.c`). |

Conflict check: no conflict found. This matches the BACnet OUT_OF_RANGE event
algorithm. Transitions to NORMAL use pTimeDelayNormal, transitions to an
offnormal state (including HIGH_LIMIT <-> LOW_LIMIT) use pTimeDelay, and when
Time_Delay_Normal is absent pTimeDelayNormal = Time_Delay. The sentinel stands
for "absent". There is no requirements document in this directory, so I
checked against the standard and the existing tests only.

Tests: added `tests/unit.vec` x101-x109 (tag CR201) and the acceptance scenario
`high-alarm-quick-return-to-normal`. The driver defaults `delay_normal` to
4294967295, so all existing vectors are unchanged and pass.

Open points:
- Precedence while in HIGH with the value below `low_limit` (and in LOW with
  the value above `high_limit`): the existing rule is kept. The pending target
  is the opposite alarm, timed with `time_delay_s`, and the timer restarts
  whenever the target changes (see x013). So a shorter `time_delay_normal_s`
  does not produce an intermediate TO_NORMAL in that case. A literal reading
  of the BACnet algorithm could produce TO_NORMAL first, because
  "value < high_limit - deadband" also holds. Tell me if you want that.
- FAULT -> NORMAL stays immediate and is not delayed by either delay. This
  matches BACnet, where fault transitions do not use the time delays.
- A real delay of exactly 4294967295 s cannot be configured, because that
  value is the sentinel. In practice this does not matter.
- The sentinel constant is private to `alarm.c`, because I left the supplied
  `alarm.h` as is. It could be made a public macro in `alarm.h` if
  integrators need it.

## CR-202

RAM budget: 32-byte `alarm_t`, configuration kept by pointer.

| Item | Status |
|------|--------|
| `alarm_t` shrinks to 32 bytes (`uint64_t storage[4]`) | Implemented. `alarm.h` was already updated. `point_t` in `alarm.c` is 28 bytes on a Cortex-M0 (checked with `clang --target=thumbv6m-none-eabi`) and 32 bytes on the 64-bit host. Compile-time checks cover both its size and its alignment. I removed the `has_value` field because nothing read it. |
| `alarm_init` keeps a pointer to `cfg` and does not copy it | Implemented. `point_t.cfg` is now `const alarm_cfg_t *`, and every call reads the limits and delays through it. The module never writes through the pointer. The line in CR-201 that says "`alarm_init` copies it" no longer applies. |
| Caller guarantees the `alarm_cfg_t` outlives the point, and points can share it | Accepted as the new API contract. `alarm.h` already says this. The driver passes a static `cfg`, so it meets the contract. |
| External behavior must not change | Met, as long as the configuration is not modified after `alarm_init` (see open points). All 54 vectors pass unchanged: 50 in `tests/unit.vec` and 4 in `acceptance.vec`. |

Conflict check: I found no conflict with the BACnet OUT_OF_RANGE algorithm or with
the existing tests. How the configuration is stored is an implementation detail.

Open points / things I am unsure about:
- **The RAM budget does not work as stated.** 512 points x 32 bytes = 16,384 bytes,
  which is the whole 16 KB of RAM. That leaves nothing for the stack, the shared
  `alarm_cfg_t` tables (24 bytes each), the BACnet stack or the note buffers. On the
  M0 the implementation uses only 28 of the 32 bytes. The other 4 bytes are padding
  from `alarm.h` (`uint64_t` storage, 8-byte alignment), which is 2 KB over 512 points.
  I left the header alone because the CR says it is final. Some options: size the
  storage per target (e.g. a pointer plus `uint32_t[6]`, 28 bytes on M0), or use a
  1-byte index into a config table and pack the states and flags (about 20 bytes).
  Even then, the alarm points alone would take most of RAM. Please confirm the point
  count and the RAM figure.
- **Modifying a configuration after init changes behavior.** Before, a change to the
  caller's `alarm_cfg_t` had no effect until `alarm_init` ran again, which also
  reset the point. Now the change takes effect at the next call, for every point
  that shares that configuration. That includes a pending time delay that is
  already running and escalation of an alarm that is already raised. This matches
  BACnet, where a write to High_Limit etc. applies right away. It does mean
  "behavior unchanged" holds only if configurations are treated as read-only.
  Three consequences for the integration layer:
  (a) A BACnet WriteProperty to one object's limit must not modify a configuration
  that other objects share. That object needs its own copy, which currently
  requires `alarm_init` and so resets its state. An `alarm_set_cfg()` could be
  added if this is needed.
  (b) Changing a configuration from another task or an ISR while an `alarm_*` call
  runs can mix old and new values between fields.
  (c) No test vector covers this, because the driver has only one point and no
  vector re-sends `cfg` after `init`. I did not add one, because the intended
  semantics have not been decided.
- A NULL `cfg` was undefined behavior before and still is. The only difference is
  that the fault now happens on the first call that reads the configuration
  instead of inside `alarm_init`.
