/* alarm.c - out-of-range alarming for analog input points.
 *
 * Behaviour (decisions beyond what alarm.h states are marked "Rule"):
 *
 * Limits and deadband
 *   - Offnormal: value > high_limit -> HIGH, value < low_limit -> LOW (strict).
 *   - Return:    HIGH stays HIGH while value >= high_limit - deadband,
 *                LOW  stays LOW  while value <= low_limit + deadband.
 *     A negative or NaN deadband is treated as 0.
 *
 * Time delay
 *   - The state the value asks for (the "target") must hold continuously for
 *     time_delay_s before the point changes state. This applies to HIGH, LOW,
 *     HIGH<->LOW and back to NORMAL. If the target changes, the delay restarts.
 *   - Rule (sample-and-hold): the last sample is assumed to persist until the next
 *     one. Every call first advances time to 'now' (firing any transition or
 *     escalation that became due, in the order it fell due, stamped with this
 *     call's 'now' and the held value) and only then applies its own input.
 *     So the state sequence and the TO_* notes do not depend on whether
 *     alarm_tick was called in between (only note times do). Escalation does:
 *     its deadline counts from when an alarm was announced to the front end.
 *
 * Sensor faults
 *   - sensor_fault, or a non-finite value (NaN/inf), moves the point to FAULT
 *     immediately (no delay) and cancels any pending transition.
 *   - Rule: the first good sample leaves FAULT immediately with TO_NORMAL; from
 *     NORMAL the value is then judged with the usual time delay.
 *
 * Acknowledgement and escalation
 *   - Every announced TO_HIGH / TO_LOW / TO_FAULT is an alarm that needs an ack.
 *     alarm_ack acknowledges all alarms announced before the call and emits no
 *     note. Rule: an alarm announced by that same ack call (its delay expired at
 *     that moment) is not acknowledged by it; the operator has not seen it yet.
 *   - ESCALATE is emitted once when an announced alarm has gone escalate_after_s
 *     seconds without an ack (>=; 0 means together with the alarm).
 *     Rule: it is latched like a BACnet unacked transition: returning to NORMAL
 *     does not cancel it, only an ack does. Rule: an alarm announced while an
 *     earlier one is still waiting to escalate shares the earlier deadline, so a
 *     point flapping between states cannot postpone escalation forever. An alarm
 *     announced after an escalation starts a new deadline. ESCALATE carries the
 *     held (current) value.
 *
 * Maintenance
 *   - While on, the state keeps following the value (alarm_state is live) but no
 *     notification of any kind is emitted, and the escalation clock is paused.
 *   - Rule: on leaving maintenance the front end is resynchronised: if the state
 *     differs from the last state announced, one note for the current state is
 *     emitted (an alarm announced this way needs an ack as usual). Transitions
 *     that happened and ended inside maintenance are never announced.
 *
 * Robustness
 *   - Times are compared by unsigned difference, so uint32 wrap-around works.
 *   - Rule: a 'now' earlier than one already seen is treated as the latest time
 *     seen (no spurious expiries); notes still carry the caller's 'now'.
 *   - Calls on an object that was never alarm_init'ed do nothing and return 0;
 *     alarm_state reports FAULT for it. No call writes more than
 *     ALARM_MAX_NOTES notes; at most 4 can arise (e.g. TO_HIGH, ESCALATE,
 *     TO_FAULT, ESCALATE with escalate_after_s = 0), so none is dropped.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define ALARM_MAGIC 0x414C524Du
#define NO_TARGET 0xFFu

typedef struct {
    uint32_t magic;
    alarm_cfg_t cfg;
    uint32_t last_t;     /* latest time seen */
    uint32_t pend_since; /* when the pending target began to hold */
    uint32_t esc_mark;   /* when the escalation clock last (re)started running */
    uint32_t esc_credit; /* escalation seconds accrued before the last pause */
    float value;         /* held sample value */
    uint8_t state;       /* live alarm_state_t */
    uint8_t announced;   /* last state announced to the front end */
    uint8_t pending;     /* target waiting out the time delay, or NO_TARGET */
    bool fault;          /* held sample was faulty */
    bool have_value;
    bool maint;
    bool esc_pending; /* an announced alarm is unacked and not yet escalated */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

typedef struct {
    impl_t s;
    uint32_t now; /* caller's time, stamped on notes */
    uint32_t t;   /* effective (never decreasing) time */
    alarm_note_t *out;
    int n;
    bool uncovered; /* an alarm announced in this call is not yet escalated */
} ctx_t;

static void emit(ctx_t *c, alarm_event_t ev) {
    if (c->n >= ALARM_MAX_NOTES) return;
    c->out[c->n].time = c->now;
    c->out[c->n].event = ev;
    c->out[c->n].value = c->s.value;
    c->n++;
}

static void announce(ctx_t *c, uint8_t st) {
    static const alarm_event_t ev[] = {EV_TO_NORMAL, EV_TO_HIGH, EV_TO_LOW, EV_TO_FAULT};
    emit(c, ev[st]);
    c->s.announced = st;
    if (st != ALARM_NORMAL) {
        if (!c->s.esc_pending) {
            c->s.esc_pending = true;
            c->s.esc_credit = 0;
            c->s.esc_mark = c->t;
        }
        c->uncovered = true;
    }
}

static void transition(ctx_t *c, uint8_t to) {
    c->s.state = to;
    c->s.pending = NO_TARGET;
    if (!c->s.maint) announce(c, to);
}

static uint32_t esc_elapsed(const ctx_t *c) {
    uint32_t e = c->s.esc_credit;
    if (!c->s.maint) {
        uint32_t run = c->t - c->s.esc_mark;
        e = (run > UINT32_MAX - e) ? UINT32_MAX : e + run;
    }
    return e;
}

/* Fire everything that is due at time t, in the order it became due. */
static void advance(ctx_t *c) {
    impl_t *s = &c->s;
    for (int i = 0; i < 4; i++) { /* at most escalate, transition, escalate */
        uint32_t pend_age = c->t - s->pend_since;
        bool pd = s->pending != NO_TARGET && pend_age >= s->cfg.time_delay_s;
        uint32_t esc_age = esc_elapsed(c);
        bool ed = s->esc_pending && !s->maint && esc_age >= s->cfg.escalate_after_s;
        if (!pd && !ed) return;
        if (ed && (!pd || esc_age - s->cfg.escalate_after_s >= pend_age - s->cfg.time_delay_s)) {
            emit(c, EV_ESCALATE);
            s->esc_pending = false;
            c->uncovered = false;
        } else {
            transition(c, s->pending);
        }
    }
}

static uint8_t target(const impl_t *s) {
    const alarm_cfg_t *k = &s->cfg;
    float v = s->value;
    if (v > k->high_limit) return ALARM_HIGH;
    if (v < k->low_limit) return ALARM_LOW;
    if (s->state == ALARM_HIGH && !(v < k->high_limit - k->deadband)) return ALARM_HIGH;
    if (s->state == ALARM_LOW && !(v > k->low_limit + k->deadband)) return ALARM_LOW;
    return ALARM_NORMAL;
}

/* Re-judge the held input after it changed. Delayed transitions fire in advance(). */
static void evaluate(ctx_t *c) {
    impl_t *s = &c->s;
    if (s->fault) {
        if (s->state != ALARM_FAULT) transition(c, ALARM_FAULT);
        s->pending = NO_TARGET;
        return;
    }
    if (s->state == ALARM_FAULT) transition(c, ALARM_NORMAL);
    uint8_t tgt = target(s);
    if (tgt == s->state) {
        s->pending = NO_TARGET;
    } else if (tgt != s->pending) {
        s->pending = tgt;
        s->pend_since = c->t;
    }
}

static bool begin(ctx_t *c, alarm_t *a, uint32_t now, alarm_note_t *out) {
    if (!a || !out) return false;
    memcpy(&c->s, a->storage, sizeof c->s);
    if (c->s.magic != ALARM_MAGIC) return false;
    c->now = now;
    c->t = ((uint32_t)(now - c->s.last_t) >= 0x80000000u) ? c->s.last_t : now;
    c->s.last_t = c->t;
    c->out = out;
    c->n = 0;
    c->uncovered = false;
    advance(c);
    return true;
}

static int finish(ctx_t *c, alarm_t *a) {
    advance(c);
    memcpy(a->storage, &c->s, sizeof c->s);
    return c->n;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    impl_t s;
    if (!a) return;
    memset(&s, 0, sizeof s);
    s.magic = ALARM_MAGIC;
    if (cfg) s.cfg = *cfg;
    if (!(s.cfg.deadband >= 0.0f)) s.cfg.deadband = 0.0f;
    s.last_t = now;
    s.state = s.announced = ALARM_NORMAL;
    s.pending = NO_TARGET;
    memset(a->storage, 0, sizeof a->storage);
    memcpy(a->storage, &s, sizeof s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!begin(&c, a, now, out)) return 0;
    c.s.value = value;
    c.s.fault = sensor_fault || !isfinite(value);
    c.s.have_value = true;
    evaluate(&c);
    return finish(&c, a);
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!begin(&c, a, now, out)) return 0;
    return finish(&c, a);
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!begin(&c, a, now, out)) return 0;
    /* Alarms announced before this call are acknowledged; one announced by this
     * very call (and not escalated yet) keeps waiting, with a fresh deadline. */
    c.s.esc_pending = c.uncovered;
    if (c.uncovered) {
        c.s.esc_credit = 0;
        c.s.esc_mark = c.t;
    }
    return finish(&c, a);
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!begin(&c, a, now, out)) return 0;
    if (on && !c.s.maint) {
        c.s.esc_credit = esc_elapsed(&c);
        c.s.maint = true;
    } else if (!on && c.s.maint) {
        c.s.maint = false;
        c.s.esc_mark = c.t;
        if (c.s.state != c.s.announced) announce(&c, c.s.state);
    }
    return finish(&c, a);
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t s;
    if (!a) return ALARM_FAULT;
    memcpy(&s, a->storage, sizeof s);
    if (s.magic != ALARM_MAGIC || s.state > ALARM_FAULT) return ALARM_FAULT;
    return (alarm_state_t)s.state;
}
