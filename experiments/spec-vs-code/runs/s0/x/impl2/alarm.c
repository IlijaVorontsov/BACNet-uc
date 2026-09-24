/* alarm.c - out-of-range alarming for analog input points (see SPEC.md). */
#include "alarm.h"

#include <math.h>
#include <string.h>

/* Internal per-point state. It is kept inside alarm_t::storage and copied in and
 * out with memcpy so that no strict-aliasing assumptions are made about alarm_t. */
typedef struct {
    alarm_cfg_t cfg;
    uint32_t last_now;     /* largest 'now' seen so far (A12) */
    uint32_t pend_start;   /* start time of the pending transition */
    uint32_t alarm_time;   /* start time of the current alarm episode (A8) */
    float last_value;      /* most recent sample value, fault samples included (A7) */
    uint8_t state;         /* alarm_state_t */
    uint8_t has_pending;
    uint8_t pend_target;   /* alarm_state_t, valid when has_pending */
    uint8_t has_sample;
    uint8_t unacked;
    uint8_t escalated;     /* ESCALATE already emitted in this episode */
    uint8_t maint;
    uint8_t last_emitted;  /* state of the last emitted TO_x notification (A10) */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "impl_t does not fit into alarm_t");

typedef struct {
    alarm_note_t *out;
    int n;
    uint32_t now;
} ctx_t;

#define NO_TARGET 0xFFu

static void load(impl_t *p, const alarm_t *a) { memcpy(p, a, sizeof *p); }
static void store(alarm_t *a, const impl_t *p) { memcpy(a, p, sizeof *p); }

/* Appends a notification (suppressed entirely in maintenance mode, A10). */
static void emit(impl_t *p, ctx_t *c, alarm_event_t ev) {
    if (p->maint) return;
    if (c->out == NULL || c->n >= ALARM_MAX_NOTES) return;
    c->out[c->n].time = c->now;
    c->out[c->n].event = ev;
    c->out[c->n].value = p->last_value;
    c->n++;
}

static alarm_event_t to_event(uint8_t s) {
    switch (s) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Emits the TO_x notification for state s; TO_HIGH/TO_LOW start an alarm episode (A8).
 * Nothing happens in maintenance mode (A10). */
static void announce(impl_t *p, ctx_t *c, uint8_t s) {
    if (p->maint) return;
    emit(p, c, to_event(s));
    p->last_emitted = s;
    if (s == ALARM_HIGH || s == ALARM_LOW) {
        p->unacked = 1;
        p->alarm_time = c->now;
        p->escalated = 0;
    }
}

static void transition(impl_t *p, ctx_t *c, uint8_t s) {
    p->state = s;
    announce(p, c, s);
}

/* Target state for value v in the current state (A2-A4); NO_TARGET if none.
 * Threshold arithmetic is done in double so that 'limit -/+ deadband' is
 * (practically) exact for the configured float values. */
static uint8_t target_for(const impl_t *p, float v) {
    const double x = (double)v;
    const double hi = (double)p->cfg.high_limit;
    const double lo = (double)p->cfg.low_limit;
    const double db = (double)p->cfg.deadband;
    const int high_cond = x > hi;
    const int low_cond = x < lo;
    switch (p->state) {
    case ALARM_NORMAL:
        if (high_cond) return ALARM_HIGH;
        if (low_cond) return ALARM_LOW;
        return NO_TARGET;
    case ALARM_HIGH:
        if (low_cond) return ALARM_LOW;
        if (x < hi - db) return ALARM_NORMAL;
        return NO_TARGET;
    case ALARM_LOW:
        if (high_cond) return ALARM_HIGH;
        if (x > lo + db) return ALARM_NORMAL;
        return NO_TARGET;
    default:
        return NO_TARGET;
    }
}

/* Evaluates the delay timer at c->now (A5a): at most one limit transition. */
static void eval_timer(impl_t *p, ctx_t *c) {
    if (!p->has_sample || !p->has_pending || p->state == ALARM_FAULT) return;
    if (c->now - p->pend_start >= p->cfg.time_delay_s) {
        uint8_t t = p->pend_target;
        p->has_pending = 0;
        transition(p, c, t);
    }
}

/* End-of-call escalation check (A9). */
static void escalation_check(impl_t *p, ctx_t *c) {
    if (p->unacked && !p->escalated && !p->maint && p->cfg.escalate_after_s > 0 &&
        c->now - p->alarm_time >= p->cfg.escalate_after_s) {
        emit(p, c, EV_ESCALATE);
        p->escalated = 1;
    }
}

/* A12: returns 0 (and leaves the point untouched) if time went backwards. */
static int begin(impl_t *p, ctx_t *c, const alarm_t *a, uint32_t now, alarm_note_t *out) {
    load(p, a);
    if (now < p->last_now) return 0;
    p->last_now = now;
    c->out = out;
    c->n = 0;
    c->now = now;
    return 1;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    impl_t p;
    memset(a, 0, sizeof *a);
    memset(&p, 0, sizeof p);
    p.cfg = *cfg;
    p.last_now = now;
    p.state = ALARM_NORMAL;
    p.pend_target = NO_TARGET;
    p.last_value = 0.0f;
    p.last_emitted = ALARM_NORMAL;
    store(a, &p);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    ctx_t c;
    if (!begin(&p, &c, a, now, out)) return 0;

    p.last_value = value;
    p.has_sample = 1;

    if (sensor_fault || isnan(value)) {
        /* A6: immediate FAULT, pending cancelled; repeated faults do nothing. */
        p.has_pending = 0;
        if (p.state != ALARM_FAULT) transition(&p, &c, ALARM_FAULT);
    } else {
        if (p.state == ALARM_FAULT) {
            /* A6a: immediate recovery, then evaluate as a normal sample in NORMAL. */
            p.has_pending = 0;
            transition(&p, &c, ALARM_NORMAL);
        }
        uint8_t t = target_for(&p, value);
        if (t == NO_TARGET) {
            p.has_pending = 0;
        } else if (!p.has_pending || p.pend_target != t) {
            p.has_pending = 1;
            p.pend_target = t;
            p.pend_start = now;
        }
        eval_timer(&p, &c);
    }

    escalation_check(&p, &c);
    store(a, &p);
    return c.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    ctx_t c;
    if (!begin(&p, &c, a, now, out)) return 0;
    eval_timer(&p, &c);
    escalation_check(&p, &c);
    store(a, &p);
    return c.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    ctx_t c;
    if (!begin(&p, &c, a, now, out)) return 0;
    eval_timer(&p, &c);
    p.unacked = 0; /* no-op if nothing is unacknowledged */
    escalation_check(&p, &c);
    store(a, &p);
    return c.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    ctx_t c;
    if (!begin(&p, &c, a, now, out)) return 0;
    eval_timer(&p, &c);
    if (on && !p.maint) {
        p.maint = 1;
    } else if (!on && p.maint) {
        p.maint = 0;
        if (p.state != p.last_emitted) announce(&p, &c, p.state); /* catch-up */
    }
    escalation_check(&p, &c);
    store(a, &p);
    return c.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t p;
    load(&p, a);
    return (alarm_state_t)p.state;
}
