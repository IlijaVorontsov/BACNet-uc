/* alarm.c - out-of-range alarming for analog input points (see SPEC.md v1.0)
 *
 * Bare-metal C11, no heap. The per-point state lives in the caller-provided
 * alarm_t; it is loaded into a local working copy at the start of each call and
 * stored back at the end (memcpy avoids strict-aliasing problems with the opaque
 * uint64_t storage).
 */
#include "alarm.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

/* "No pending transition" marker for impl_t.pend. */
#define PEND_NONE 0xFFu

typedef struct {
    alarm_cfg_t cfg;          /* copied at init */
    uint32_t last_now;        /* largest 'now' seen so far (A12) */
    uint32_t pend_start;      /* start time of the pending transition (A5a) */
    uint32_t alarm_time;      /* start time of the current alarm episode (A8) */
    float value;              /* most recent sample value (A7) */
    uint8_t state;            /* alarm_state_t */
    uint8_t pend;             /* pending target state, or PEND_NONE */
    uint8_t notified_state;   /* state of the last emitted TO_x notification (A10) */
    bool unacked;             /* current episode not yet acknowledged (A8) */
    bool escalated;           /* ESCALATE already emitted in this episode (A9) */
    bool maint;               /* maintenance mode (A10) */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

/* Notification sink for one API call. */
typedef struct {
    alarm_note_t *out;
    int n;
    uint32_t now;
} sink_t;

static void load(impl_t *p, const alarm_t *a) { memcpy(p, a->storage, sizeof *p); }
static void store(alarm_t *a, const impl_t *p) { memcpy(a->storage, p, sizeof *p); }

static void emit(const impl_t *p, sink_t *s, alarm_event_t ev) {
    if (s->n >= ALARM_MAX_NOTES) return; /* cannot happen: at most 2 notes per call */
    s->out[s->n].time = s->now;
    s->out[s->n].event = ev;
    s->out[s->n].value = p->value;
    s->n++;
}

static alarm_event_t to_event(uint8_t st) {
    switch (st) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Emit the TO_x notification for the current state; TO_HIGH/TO_LOW start a new
 * alarm episode (A8). Only called when not in maintenance. */
static void notify_state(impl_t *p, sink_t *s) {
    emit(p, s, to_event(p->state));
    p->notified_state = p->state;
    if (p->state == ALARM_HIGH || p->state == ALARM_LOW) {
        p->unacked = true;
        p->alarm_time = s->now;
        p->escalated = false;
    }
}

/* State transition (A7, A10: silent in maintenance). */
static void transition(impl_t *p, sink_t *s, uint8_t to) {
    p->state = to;
    if (!p->maint) notify_state(p, s);
}

/* Target state for value v in the current state (A2-A4); PEND_NONE if none. */
static uint8_t target(const impl_t *p, float v) {
    const float hi = p->cfg.high_limit;
    const float lo = p->cfg.low_limit;
    switch (p->state) {
    case ALARM_NORMAL:
        if (v > hi) return ALARM_HIGH;
        if (v < lo) return ALARM_LOW;
        return PEND_NONE;
    case ALARM_HIGH: {
        const float ret = hi - p->cfg.deadband;
        if (v < lo) return ALARM_LOW;
        if (v < ret) return ALARM_NORMAL;
        return PEND_NONE;
    }
    case ALARM_LOW: {
        const float ret = lo + p->cfg.deadband;
        if (v > hi) return ALARM_HIGH;
        if (v > ret) return ALARM_NORMAL;
        return PEND_NONE;
    }
    default:
        return PEND_NONE; /* FAULT: timers do not run */
    }
}

/* Evaluate the delay timer at now (A5, A5a): at most one limit transition. */
static void eval_timer(impl_t *p, sink_t *s) {
    if (p->state == ALARM_FAULT || p->pend == PEND_NONE) return;
    if (s->now - p->pend_start >= p->cfg.time_delay_s) {
        uint8_t to = p->pend;
        p->pend = PEND_NONE;
        transition(p, s, to);
    }
}

/* End-of-call escalation check (A9). */
static void check_escalation(impl_t *p, sink_t *s) {
    if (p->unacked && !p->escalated && !p->maint && p->cfg.escalate_after_s > 0 &&
        s->now - p->alarm_time >= p->cfg.escalate_after_s) {
        emit(p, s, EV_ESCALATE);
        p->escalated = true;
    }
}

/* Common prologue: reject time going backwards (A12). Returns false to ignore. */
static bool begin(impl_t *p, const alarm_t *a, alarm_note_t *out, uint32_t now, sink_t *s) {
    if (a == NULL || out == NULL) return false;
    load(p, a);
    if (now < p->last_now) return false;
    p->last_now = now;
    s->out = out;
    s->n = 0;
    s->now = now;
    return true;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (a == NULL || cfg == NULL) return;
    impl_t p;
    memset(&p, 0, sizeof p);
    p.cfg = *cfg;
    p.last_now = now;
    p.value = 0.0f;
    p.state = ALARM_NORMAL;
    p.pend = PEND_NONE;
    p.notified_state = ALARM_NORMAL; /* front-end baseline: NORMAL */
    p.unacked = false;
    p.escalated = false;
    p.maint = false;
    memset(a, 0, sizeof *a);
    store(a, &p);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault,
                 alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s;
    if (!begin(&p, a, out, now, &s)) return 0;

    p.value = value;
    if (sensor_fault || isnan(value)) {
        /* A6: immediate FAULT, pending cancelled; repeated faults do nothing. */
        p.pend = PEND_NONE;
        if (p.state != ALARM_FAULT) transition(&p, &s, ALARM_FAULT);
    } else {
        /* A6a: first valid sample in FAULT recovers to NORMAL, then is evaluated
         * as a normal sample in NORMAL. */
        if (p.state == ALARM_FAULT) transition(&p, &s, ALARM_NORMAL);
        uint8_t t = target(&p, value);
        if (t == PEND_NONE) {
            p.pend = PEND_NONE;
        } else if (t != p.pend) {
            p.pend = t;
            p.pend_start = now;
        }
        eval_timer(&p, &s);
    }
    check_escalation(&p, &s);
    store(a, &p);
    return s.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s;
    if (!begin(&p, a, out, now, &s)) return 0;
    eval_timer(&p, &s);
    check_escalation(&p, &s);
    store(a, &p);
    return s.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s;
    if (!begin(&p, a, out, now, &s)) return 0;
    eval_timer(&p, &s);
    p.unacked = false; /* no-op when nothing is unacknowledged */
    check_escalation(&p, &s);
    store(a, &p);
    return s.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s;
    if (!begin(&p, a, out, now, &s)) return 0;
    eval_timer(&p, &s);
    if (on && !p.maint) {
        p.maint = true;
    } else if (!on && p.maint) {
        p.maint = false;
        /* A10: catch-up notification so the front-end shows the true state. */
        if (p.state != p.notified_state) notify_state(&p, &s);
    }
    check_escalation(&p, &s);
    store(a, &p);
    return s.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    if (a == NULL) return ALARM_FAULT;
    impl_t p;
    load(&p, a);
    return (alarm_state_t)p.state;
}
