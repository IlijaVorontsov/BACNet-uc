# alarm — Specification v1.3

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
  continuously for its *delay*:
  - HIGH → NORMAL and LOW → NORMAL: `time_delay_normal_s` (BACnet Time_Delay_Normal);
    the value `0xFFFFFFFF` means "same as `time_delay_s`" (as if Time_Delay_Normal were
    absent).
  - All other delayed transitions (NORMAL → HIGH/LOW, HIGH ↔ LOW): `time_delay_s`
    (BACnet Time_Delay).

  The FAULT → NORMAL recovery (A6a) and TO_FAULT (A6) are not delayed.
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
  immediately (TO_NORMAL) and is then evaluated as a normal sample in NORMAL (so with
  `time_delay_s = 0` it can cause a second transition, e.g. TO_NORMAL then TO_HIGH, in
  the same call). Timers do not run in FAULT. TO_FAULT is not an alarm: it does not
  change the acknowledgement state.

## 4. Notifications, acknowledgement, escalation

- **A7 [POL]** Every state transition emits one notification (TO_NORMAL, TO_HIGH,
  TO_LOW, TO_FAULT) with `time = now` and `value` = the most recent sample value
  (including fault samples, so it can be NaN).
- **A8 [POL] Acknowledgement.** TO_HIGH and TO_LOW start a new *alarm episode*: the
  point becomes unacknowledged, the alarm time is `now`, escalation is re-armed.
  `alarm_ack` clears the unacknowledged status and emits one ACKED notification with
  `time = now` and `value` = the most recent sample value (so it can be NaN in FAULT);
  in maintenance mode the status is cleared without a notification (A10). An ack when
  nothing is unacknowledged is ignored (no notification). ACKED is not a transition: it
  does not change the state or the state of the last emitted transition notification
  (A10). **Returning to NORMAL does not clear the unacknowledged
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
  An `alarm_ack` during maintenance still clears the unacknowledged status, silently;
  no ACKED is sent for it later (the catch-up covers only the state).
  *Rationale:* technicians must not flood the front-end during service, but the
  front-end must end up showing the true state.
- **A11 [POL]** Notifications of one call are in the order they were generated:
  transitions first, then ACKED, then ESCALATE. (ACKED and ESCALATE never occur in
  the same call, because the ack is applied before the escalation check.)

## 5. Robustness

- **A12 [POL] Time never goes backwards.** A call whose `now` is smaller than the
  largest `now` seen so far (including `alarm_init`) is ignored entirely: no state
  change, 0 notifications. Equal times are fine.
- **A13 [POL]** Before the first sample, `alarm_tick`, `alarm_ack` and
  `alarm_set_maintenance` cannot cause transitions.

## 6. Memory and configuration (API contract, not a behavior rule)

- `alarm_t` is 32 bytes (`uint64_t storage[4]`); no heap is used.
- `alarm_init` keeps a pointer to the caller's `alarm_cfg_t`; it does not copy it. The
  caller keeps that configuration alive for as long as the point is used. One
  configuration may be shared by several points. The module only reads it (at every
  call) and never writes it.
- For a configuration that is not modified after `alarm_init`, keeping a pointer
  instead of a copy does not change behavior: rules A1–A13 apply as written (v1.2 was
  behaviorally identical to v1.1).
- If the caller modifies a configuration after `alarm_init`, the new values apply from
  the next call on, to every point that uses it; state, pending transition (and its
  start time) and alarm episode are kept. (Up to v1.1 such a modification had no effect
  on an initialized point.) The configuration must not be modified while a call on a
  point that uses it is running.

## Change history

- v1.1 (CR-201): separate return-to-normal delay `time_delay_normal_s`
  (BACnet Time_Delay_Normal) in A5/A5a.
- v1.2 (CR-202): RAM budget. `alarm_t` shrinks to 32 bytes and `alarm_init` keeps a
  pointer to the configuration instead of a copy (section 6). No change to A1–A13.
- v1.3 (CR-203): new notification ACKED (`EV_ACKED`) when `alarm_ack` clears an
  unacknowledged episode (A8, A11), approved by the product owner. Not changed:
  A2 strict limits ([STD], ASHRAE 135 13.3.6) and the A8 rule that returning to NORMAL
  does not clear the unacknowledged status (SP-3) — CR-203 items 2 and 3 were
  declined.
