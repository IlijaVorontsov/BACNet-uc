/* alarm.c - out-of-range alarming for analog input points
 *
 * Implements SPEC.md "alarm - Specification v1.0" (API: alarm.h). Bare-metal
 * C11, no heap: all per-point state lives inside the caller's alarm_t.
 *
 * Contract summary
 * ----------------
 * - Each point is in one of NORMAL, HIGH, LOW, FAULT (A1). A value strictly
 *   above high_limit / below low_limit is offnormal (A2); returning to NORMAL
 *   needs the value strictly inside the limits by the deadband (A3); HIGH<->LOW
 *   may happen directly and wins over a return to normal (A4).
 * - A limit transition happens only after its target condition has held
 *   continuously for its time delay (A5): time_delay_normal_s for HIGH/LOW ->
 *   NORMAL (BACnet Time_Delay_Normal; 0xFFFFFFFF = same as time_delay_s),
 *   time_delay_s for every other limit transition (CR-201). Time only
 *   advances through API calls and a transition only happens when a call
 *   observes it (A5a).
 * - Sensor faults / NaN go to FAULT at once; the first valid sample recovers
 *   to NORMAL at once (A6, A6a).
 * - Every transition emits one notification (A7). TO_HIGH/TO_LOW open an alarm
 *   episode that must be acknowledged even if the value returns to normal
 *   (A8) and escalates once if left unacknowledged (A9). Maintenance mode
 *   silences everything and emits one catch-up notification when it ends
 *   (A10). Notifications are ordered: transitions, then ESCALATE (A11).
 * - Calls with a time earlier than the latest seen are ignored (A12). Nothing
 *   but alarm_sample can cause the first transition (A13).
 * - Every mutating call returns the number of notifications written to out[]
 *   (at most ALARM_MAX_NOTES).
 *
 * Rule tags (see SPEC.md)
 * -----------------------
 * [STD] A1-A5: modeled on the BACnet OUT_OF_RANGE event algorithm (ASHRAE 135
 *       clause 13.3.6); required for our BTL listing of intrinsic reporting.
 * [POL] A5a, A6, A6a, A7-A13: our own product/safety policy.
 *
 * DO NOT CHANGE a [POL] rule (or the code implementing it) without
 * product-owner sign-off. SPEC.md records an explicit rationale only for A8
 * (safety policy SP-3) and A10 (service without flooding the front-end); both
 * are quoted at the code below. No rationale is recorded for the other [POL]
 * rules - do not invent one; ask the product owner.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

/* "No pending transition" / "no target" marker; distinct from every
 * alarm_state_t value. */
#define NONE 0xFF

/* alarm_cfg_t.time_delay_normal_s value meaning "same as time_delay_s"
 * (CR-201; mirrors an absent Time_Delay_Normal property in BACnet). */
#define DELAY_NORMAL_SAME_AS_DELAY 0xFFFFFFFFu

/* Private layout of the opaque alarm_t (alarm.h: no heap, at most
 * sizeof(alarm_t) bytes). */
typedef struct {
    /* Copy of the configuration taken by alarm_init. */
    alarm_cfg_t cfg;
    /* A1 [STD]: current state (alarm_state_t). */
    uint8_t state;
    /* A10 [POL]: state of the last *emitted* transition notification; used to
     * decide whether leaving maintenance needs a catch-up notification. */
    uint8_t last_notified;
    /* A5 [STD] / A5a [POL]: target state of the pending transition, or NONE. */
    uint8_t pending;
    /* A8 [POL]: the current alarm episode is unacknowledged. */
    bool unacked;
    /* A9 [POL]: ESCALATE already emitted in the current episode. */
    bool escalated;
    /* A10 [POL]: maintenance mode is on. */
    bool maint;
    /* Set by the first alarm_sample. Not read: A13 already holds because
     * 'pending' stays NONE until a sample sets it. */
    bool has_value;
    /* A5 [STD] / A5a [POL]: 'now' at which the pending target condition started
     * to hold. */
    uint32_t pending_start;
    /* A8/A9 [POL]: 'now' at which the current alarm episode started. */
    uint32_t alarm_time;
    /* A12 [POL]: largest 'now' seen so far (including alarm_init). */
    uint32_t last_now;
    /* A7 [POL] / A5a [POL]: most recent sample value (may be NaN after a fault
     * sample); reported in every notification and assumed to persist between
     * samples. */
    float last_value;
} point_t;

_Static_assert(sizeof(point_t) <= sizeof(alarm_t), "point_t too large");

static point_t *P(alarm_t *a) { return (point_t *)(void *)a; }

/* Target state for value v in the current state, or NONE (the target table of
 * SPEC.md section 1).
 *
 * A2 [STD] Strict limits: high condition is v > high_limit, low condition is
 *   v < low_limit; a value exactly at a limit is not offnormal.
 * A3 [STD] Deadband on return (strict): from HIGH, v < high_limit - deadband;
 *   from LOW, v > low_limit + deadband.
 * A4 [STD] Direct HIGH<->LOW: from HIGH the low condition leads to LOW, from
 *   LOW the high condition leads to HIGH, without passing NORMAL. The direct
 *   transition is checked first, so it wins over a return to normal.
 * A6 [POL]: +/-Infinity is a valid value and simply compares beyond the
 *   limits; NaN never reaches this function (it is a fault, see alarm_sample).
 * A6a [POL]: no target in FAULT - timers do not run there. */
static uint8_t target(const point_t *p, float v)
{
    const alarm_cfg_t *c = &p->cfg;
    switch (p->state) {
    case ALARM_NORMAL:
        /* A2 [STD]: high condition checked first, then low condition. */
        if (v > c->high_limit)
            return ALARM_HIGH;
        if (v < c->low_limit)
            return ALARM_LOW;
        return NONE;
    case ALARM_HIGH:
        /* A4 [STD]: direct HIGH -> LOW wins over the return to normal. */
        if (v < c->low_limit)
            return ALARM_LOW;
        /* A3 [STD]: strict return condition with deadband. */
        if (v < c->high_limit - c->deadband)
            return ALARM_NORMAL;
        return NONE;
    case ALARM_LOW:
        /* A4 [STD]: direct LOW -> HIGH wins over the return to normal. */
        if (v > c->high_limit)
            return ALARM_HIGH;
        /* A3 [STD]: strict return condition with deadband. */
        if (v > c->low_limit + c->deadband)
            return ALARM_NORMAL;
        return NONE;
    default:
        /* ALARM_FAULT - A6a [POL]: timers do not run in FAULT. */
        return NONE;
    }
}

/* Append one notification to out[].
 * A7 [POL]: time = 'now' of the call, value = the value passed (always the most
 *   recent sample value).
 * A11 [POL]: notifications are appended in the order they are generated.
 * The rules never produce more than ALARM_MAX_NOTES per call; the bound check
 * only protects out[]. */
static void emit(alarm_note_t *out, int *n, uint32_t now, alarm_event_t ev, float v)
{
    if (*n < ALARM_MAX_NOTES) {
        out[*n].time = now;
        out[*n].event = ev;
        out[*n].value = v;
        (*n)++;
    }
}

/* A7 [POL]: state -> TO_x event. Indexed by alarm_state_t; relies on the
 * numeric values in alarm.h (NORMAL=0, HIGH=1, LOW=2, FAULT=3). */
static const alarm_event_t to_event[4] = {EV_TO_NORMAL, EV_TO_HIGH, EV_TO_LOW, EV_TO_FAULT};

/* Emit the TO_x notification for the current state and apply its side
 * effects. Used both for live transitions and for the A10 catch-up.
 *
 * A7 [POL]: exactly one notification per transition, time = now, value = most
 *   recent sample value (including a fault sample, so it can be NaN).
 * A10 [POL]: remember which state was last notified, for the catch-up when
 *   maintenance ends.
 * A8 [POL] Acknowledgement: TO_HIGH and TO_LOW start a new alarm episode - the
 *   point becomes unacknowledged, the alarm time is 'now', and escalation is
 *   re-armed (A9). TO_NORMAL deliberately does NOT clear 'unacked': returning
 *   to NORMAL leaves the episode to be acknowledged, and it can still escalate.
 *   Rationale (SPEC.md): safety policy SP-3 - every excursion must be seen by
 *   a person, even a transient one (freezer/cold-room incident 2023).
 * A6a [POL]: TO_FAULT is not an alarm - it does not touch the acknowledgement
 *   state. */
static void notify_state(point_t *p, uint32_t now, alarm_note_t *out, int *n)
{
    emit(out, n, now, to_event[p->state], p->last_value);
    p->last_notified = p->state;
    if (p->state == ALARM_HIGH || p->state == ALARM_LOW) {
        p->unacked = true;
        p->alarm_time = now;
        p->escalated = false;
    }
}

/* Perform a state transition.
 * A5a [POL]: a transition consumes the pending transition.
 * A7 [POL]: every transition emits one notification...
 * A10 [POL]: ...except in maintenance mode, where the state machine still runs
 *   (the state changes, alarm_state reports the real state) but no
 *   notification is emitted and no alarm episode is started (the A8 episode
 *   start lives in notify_state, which is skipped). */
static void transition(point_t *p, uint8_t to, uint32_t now, alarm_note_t *out, int *n)
{
    p->state = to;
    p->pending = NONE;
    if (!p->maint)
        notify_state(p, now, out, n);
}

/* Time delay that applies to a pending limit transition to 'to'.
 * A5 [STD] / CR-201: a return to NORMAL (from HIGH or LOW - the only states
 *   in which target() yields NORMAL) uses time_delay_normal_s (BACnet
 *   pTimeDelayNormal), or time_delay_s when it is DELAY_NORMAL_SAME_AS_DELAY
 *   (BACnet: Time_Delay_Normal absent). Every other limit transition
 *   (NORMAL -> HIGH/LOW, HIGH <-> LOW) uses time_delay_s (pTimeDelay).
 *   The immediate FAULT transitions (A6, A6a) do not use a delay at all. */
static uint32_t delay_for(const point_t *p, uint8_t to)
{
    if (to == ALARM_NORMAL && p->cfg.time_delay_normal_s != DELAY_NORMAL_SAME_AS_DELAY)
        return p->cfg.time_delay_normal_s;
    return p->cfg.time_delay_s;
}

/* Evaluate the timer at 'now'.
 * A5 [STD]: the pending transition happens only once its target condition has
 *   held continuously for its time delay (delay_for(): time_delay_normal_s for
 *   a return to NORMAL, time_delay_s otherwise).
 * A5a [POL]: it happens at 'now' when now - pending_start >= that delay (so a
 *   delay of 0 means immediately on the sample). At most one limit
 *   transition per evaluation: transition() clears 'pending'. The unsigned
 *   subtraction cannot wrap because A12 keeps 'now' monotonic.
 * A6a [POL]: timers do not run in FAULT.
 * A13 [POL]: before the first sample 'pending' is NONE, so tick/ack/maintenance
 *   calls cannot cause a transition. */
static void eval_timer(point_t *p, uint32_t now, alarm_note_t *out, int *n)
{
    if (p->state == ALARM_FAULT || p->pending == NONE)
        return;
    if (now - p->pending_start >= delay_for(p, p->pending))
        transition(p, p->pending, now, out, n);
}

/* Escalation check; called at the end of every accepted call, after all
 * transitions.
 * A9 [POL] Escalation: emit ESCALATE (value = most recent sample value) if the
 *   point is unacknowledged, not yet escalated in this episode, not in
 *   maintenance, escalate_after_s > 0 (0 disables escalation) and
 *   now - alarm_time >= escalate_after_s. Once per episode ('escalated' is
 *   re-armed only by a new episode, A8).
 * A10 [POL]: escalation does not fire in maintenance.
 * A11 [POL]: runs last, so ESCALATE follows the call's transitions. */
static void check_escalate(point_t *p, uint32_t now, alarm_note_t *out, int *n)
{
    if (p->unacked && !p->escalated && !p->maint && p->cfg.escalate_after_s > 0 &&
        now - p->alarm_time >= p->cfg.escalate_after_s) {
        emit(out, n, now, EV_ESCALATE, p->last_value);
        p->escalated = true;
    }
}

/* A12 [POL] Time never goes backwards: a call whose 'now' is smaller than the
 * largest 'now' seen so far (including alarm_init) is rejected, and the caller
 * returns 0 without any state change. Equal times are accepted. */
static bool accept_time(point_t *p, uint32_t now)
{
    if (now < p->last_now)
        return false;
    p->last_now = now;
    return true;
}

/* A1 [STD]: after alarm_init the state is NORMAL, nothing is pending, nothing
 * is unacknowledged (memset clears 'unacked', 'escalated', 'maint').
 * alarm.h: the configuration is copied.
 * A10 [POL]: last notified state starts as NORMAL, so leaving maintenance while
 *   still NORMAL needs no catch-up.
 * A12 [POL]: the init time counts as the first 'now' seen. */
void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now)
{
    point_t *p = P(a);
    memset(a, 0, sizeof *a);
    p->cfg = *cfg;
    p->state = ALARM_NORMAL;
    p->last_notified = ALARM_NORMAL;
    p->pending = NONE;
    p->last_now = now;
}

/* Feed one sample. */
int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    /* A12 [POL]: ignore calls that go back in time. */
    if (!accept_time(p, now))
        return 0;
    /* A5a [POL]: first the new value replaces the previous one.
     * A7 [POL]: this includes fault samples, so notifications may carry NaN. */
    p->last_value = value;
    p->has_value = true;
    /* A6 [POL] Sensor faults: sensor_fault or NaN moves the point to FAULT
     * immediately (no delay), from any state, emitting TO_FAULT; the pending
     * transition is cancelled. Further fault samples while in FAULT do nothing.
     * +/-Infinity is not NaN, so it is a valid (non-fault) value. */
    if (sensor_fault || isnan(value)) {
        if (p->state != ALARM_FAULT)
            transition(p, ALARM_FAULT, now, out, &n);
        p->pending = NONE;
    } else {
        /* A6a [POL] Recovery: the first valid sample in FAULT moves the point
         * to NORMAL immediately (TO_NORMAL) and is then evaluated below as a
         * normal sample in NORMAL, so with time_delay_s = 0 it can cause a
         * second transition in the same call (e.g. TO_NORMAL then TO_HIGH). */
        if (p->state == ALARM_FAULT)
            transition(p, ALARM_NORMAL, now, out, &n);
        /* A5a [POL] Timer semantics, applied to the target of A2-A4 [STD]:
         * - no target: the pending transition is cancelled - even if its delay
         *   had already expired since the previous call, because the
         *   transition only happens if a call observes it;
         * - a different target: a new pending transition starts at 'now';
         * - the same target: the pending transition keeps its start time
         *   (A5 [STD]: the condition has held continuously). */
        uint8_t t = target(p, value);
        if (t == NONE) {
            p->pending = NONE;
        } else if (t != p->pending) {
            p->pending = t;
            p->pending_start = now;
        }
        /* A5a [POL]: then the timer is evaluated at 'now'. */
        eval_timer(p, now, out, &n);
    }
    /* A9 [POL] / A11 [POL]: escalation check at the end, after transitions. */
    check_escalate(p, now, out, &n);
    return n;
}

/* Advance time without a new sample. */
int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    /* A12 [POL]: ignore calls that go back in time. */
    if (!accept_time(p, now))
        return 0;
    /* A5a [POL]: evaluate the timer at 'now'; the last sample value is assumed
     * to persist, so the pending target is not recomputed.
     * A13 [POL]: before the first sample nothing is pending. */
    eval_timer(p, now, out, &n);
    /* A9 [POL] / A11 [POL]: escalation check at the end, after transitions. */
    check_escalate(p, now, out, &n);
    return n;
}

/* Acknowledge the current alarm episode. */
int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    /* A12 [POL]: ignore calls that go back in time. */
    if (!accept_time(p, now))
        return 0;
    /* A5a [POL]: evaluate the timer at 'now' first (last sample value
     * persists), then do the ack. A13 [POL]: nothing pending before the first
     * sample. */
    eval_timer(p, now, out, &n);
    /* A8 [POL]: clear the unacknowledged status, with no notification; an ack
     * when nothing is unacknowledged is ignored. */
    if (p->unacked)
        p->unacked = false;
    /* A9 [POL]: the ack is applied *before* the escalation check, so a late ack
     * does not escalate. A11 [POL]: escalation after transitions. */
    check_escalate(p, now, out, &n);
    return n;
}

/* Turn maintenance mode on or off. */
int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    /* A12 [POL]: ignore calls that go back in time. */
    if (!accept_time(p, now))
        return 0;
    /* A5a [POL]: evaluate the timer at 'now' first (last sample value
     * persists), with the maintenance setting that was in force until now.
     * A13 [POL]: nothing pending before the first sample. */
    eval_timer(p, now, out, &n);
    /* A10 [POL] Maintenance mode. The state machine keeps running while it is
     * on, but no notification of any kind is emitted, transitions do not start
     * alarm episodes and escalation does not fire (see transition() and
     * check_escalate()). Turning it on/off when already on/off changes nothing.
     * On a real on->off switch: if the current state differs from the state of
     * the last emitted transition notification, emit one catch-up TO_x for the
     * current state (value = most recent sample); a catch-up TO_HIGH/TO_LOW
     * starts a new alarm episode (A8, via notify_state). Then the escalation
     * check runs.
     * Rationale (SPEC.md): technicians must not flood the front-end during
     * service, but the front-end must end up showing the true state. */
    if (on) {
        p->maint = true;
    } else if (p->maint) {
        p->maint = false;
        if (p->state != p->last_notified)
            notify_state(p, now, out, &n);
    }
    /* A9 [POL] / A11 [POL]: escalation check at the end, after the catch-up
     * (suppressed if maintenance is now on, A10). */
    check_escalate(p, now, out, &n);
    return n;
}

/* A1 [STD] / A10 [POL]: report the real current state (also during
 * maintenance). */
alarm_state_t alarm_state(const alarm_t *a)
{
    return (alarm_state_t)((const point_t *)(const void *)a)->state;
}
