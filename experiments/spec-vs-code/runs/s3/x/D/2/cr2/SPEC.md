# alarm — Specification v1.2

Module: out-of-range alarming for analog input points of our BACnet controller
(bare-metal C11, no heap). API: `alarm.h`.

Rules are tagged **[STD]** (modeled on the BACnet OUT_OF_RANGE event algorithm,
ASHRAE 135 clause 13.3.6 — required for our BTL listing of intrinsic reporting) or
**[POL]** (our own product/safety policy; rationale given — do not change a [POL] rule
without product-owner sign-off).

## 1. States and conditions

- **A1 [STD]** States: NORMAL, HIGH, LOW, FAULT. After `alarm_init` the state is
  NORMAL, nothing is pending, nothing is unacknowledged.
- **A2 [STD] Strict limits.** High condition: `value > high_limit`. Low condition:
  `value < low_limit`. A value exactly at a limit is *not* offnormal.
- **A3 [STD] Deadband on return.** From HIGH the return-to-normal condition is
  `value < high_limit − deadband`; from LOW it is `value > low_limit + deadband`
  (both strict).
- **A4 [STD] Direct HIGH↔LOW.** From HIGH, the low condition leads to LOW directly
  (without passing NORMAL); from LOW, the high condition leads to HIGH directly. If both
  a direct transition and a return to normal are possible, the direct one wins.

  The *target* for a value in state S is therefore:
  NORMAL: high cond → HIGH, else low cond → LOW, else none.
  HIGH: low cond → LOW, else return cond → NORMAL, else none.
  LOW: high cond → HIGH, else return cond → NORMAL, else none.

## 2. Time delay

- **A5 [STD]** A transition happens only after its target condition has held
  continuously for its time delay:
  - HIGH→NORMAL and LOW→NORMAL use `time_delay_normal_s` (BACnet Time_Delay_Normal);
    the value `0xFFFFFFFF` means "same as `time_delay_s`" (Time_Delay_Normal absent).
  - All other delayed transitions (NORMAL→HIGH, NORMAL→LOW, HIGH→LOW, LOW→HIGH) use
    `time_delay_s` (BACnet Time_Delay). (CR-201)
- **A5a [POL] Timer semantics.** Time only advances through API calls.
  - `alarm_sample(now, v)`: first the new value replaces the previous one. If `v` has no
    target, the pending transition is cancelled. If its target differs from the pending
    one, a new pending transition starts at `now`. If it is the same target, the pending
    transition keeps its start time. Then the timer is evaluated at `now`.
  - Evaluating the timer at `now`: if a transition is pending and
    `now − start ≥` its delay (A5), it happens at `now` (so a delay of 0 means
    immediately on the sample).
  - `alarm_tick`, `alarm_ack` and `alarm_set_maintenance` evaluate the timer at `now`
    first (the last sample value is assumed to persist), then do their own action.
  - A pending condition that stops holding is cancelled even if its delay had expired
    between two calls: the transition only happens if a call observes it (e.g. pending
    HIGH since t=10 with delay 30, next call is a normal sample at t=50 → no alarm).
  - At most one limit transition happens per evaluation.

## 3. Faults

- **A6 [POL] Sensor faults.** A sample with `sensor_fault = true` or a NaN value moves
  the point to FAULT immediately (no delay), from any state, emitting TO_FAULT; the
  pending transition is cancelled. Further fault samples while in FAULT do nothing.
  ±Infinity is a valid (non-fault) value.
- **A6a [POL] Recovery.** The first valid sample in FAULT moves the point to NORMAL
  immediately (TO_NORMAL; neither `time_delay_s` nor `time_delay_normal_s` applies)
  and is then evaluated as a normal sample in NORMAL (so with `time_delay_s = 0` it can
  cause a second transition, e.g. TO_NORMAL then TO_HIGH, in the same call). Timers do
  not run in FAULT. TO_FAULT is not an alarm: it does not change the acknowledgement
  state.

## 4. Notifications, acknowledgement, escalation

- **A7 [POL]** Every state transition emits one notification (TO_NORMAL, TO_HIGH,
  TO_LOW, TO_FAULT) with `time = now` and `value` = the most recent sample value
  (including fault samples, so it can be NaN).
- **A8 [POL] Acknowledgement.** TO_HIGH and TO_LOW start a new *alarm episode*: the
  point becomes unacknowledged, the alarm time is `now`, escalation is re-armed.
  `alarm_ack` clears the unacknowledged status (no notification); an ack when nothing is
  unacknowledged is ignored. **Returning to NORMAL does not clear the unacknowledged
  status** — the episode still has to be acknowledged and can still escalate.
  *Rationale:* safety policy SP-3: every excursion must be seen by a person, even a
  transient one (freezer/cold-room incident 2023).
- **A9 [POL] Escalation.** At the end of every call, if the point is unacknowledged,
  not yet escalated in this episode, not in maintenance, `escalate_after_s > 0` and
  `now − alarm time ≥ escalate_after_s`, emit ESCALATE (value = most recent sample value)
  — once per episode. `escalate_after_s = 0` disables escalation. In `alarm_ack` the
  acknowledgement is applied *before* this check, so a late ack does not escalate.
- **A10 [POL] Maintenance mode.** `alarm_set_maintenance(on)`: the state machine keeps
  running (states and timers change normally, `alarm_state` reports the real state),
  but no notification of any kind is emitted, transitions do not start alarm episodes and
  escalation does not fire. On `alarm_set_maintenance(off)` (when it was on): if the
  current state differs from the state of the last emitted transition notification,
  emit one catch-up notification for the current state (TO_x, value = most recent
  sample); a catch-up TO_HIGH/TO_LOW starts a new alarm episode. Then the escalation
  check runs. Turning maintenance on/off when already on/off changes nothing.
  *Rationale:* technicians must not flood the front-end during service, but the
  front-end must end up showing the true state.
- **A11 [POL]** Notifications of one call are in the order they were generated:
  transitions first, then ESCALATE.

## 5. Robustness

- **A12 [POL] Time never goes backwards.** A call whose `now` is smaller than the
  largest `now` seen so far (including `alarm_init`) is ignored entirely: no state
  change, 0 notifications. Equal times are fine.
- **A13 [POL]** Before the first sample, `alarm_tick`, `alarm_ack` and
  `alarm_set_maintenance` cannot cause transitions.

## 6. Configuration and memory (CR-202)

This section is an interface contract, not an alarm rule.

- `alarm_t` is 32 bytes (`uint64_t storage[4]`) and holds all of a point's own state.
  There is no heap.
- `alarm_init` does not copy the configuration. It keeps a pointer to the caller's
  `alarm_cfg_t`. The caller keeps that configuration valid for as long as the point is
  used. One configuration can be shared by several points. The module only reads it.
- The rules above assume that a point's configuration does not change after
  `alarm_init`. Before CR-202 the copy made this true automatically. Changing a
  configuration in place is not specified: the change would reach every point that
  shares it, at each point's next call. To move a point to a different configuration,
  call `alarm_init` again with that configuration. As before, this restarts the point
  in NORMAL (A1).
