/* alarm.c - out-of-range alarming for analog input points (see SPEC.md).
 *
 * Bare-metal C11, no heap. The per-point state lives in the opaque alarm_t
 * storage; it is copied into a typed local struct at the start of every call
 * and written back at the end (memcpy keeps this free of aliasing issues).
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

/* "No pending transition" / "no target". */
#define TGT_NONE 0xFFu

typedef struct {
    alarm_cfg_t cfg;        /* copied configuration */
    uint32_t last_now;      /* largest 'now' seen so far (A12) */
    uint32_t pend_start;    /* start time of the pending transition (A5a) */
    uint32_t alarm_time;    /* start time of the current alarm episode (A8) */
    float value;            /* most recent sample value (A7) */
    uint8_t state;          /* alarm_state_t */
    uint8_t pending;        /* target alarm_state_t, or TGT_NONE */
    uint8_t notified_state; /* state of the last emitted transition notification (A10) */
    bool has_sample;        /* at least one sample seen (A13) */
    bool unacked;           /* current episode not acknowledged (A8) */
    bool escalated;         /* ESCALATE already sent in this episode (A9) */
    bool maint;             /* maintenance mode (A10) */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

/* Collects the notifications of one call. */
typedef struct {
    alarm_note_t *out;
    int n;
} sink_t;

static void load(impl_t *p, const alarm_t *a) { memcpy(p, a->storage, sizeof *p); }
static void store(alarm_t *a, const impl_t *p) { memcpy(a->storage, p, sizeof *p); }

static void emit(const impl_t *p, sink_t *s, uint32_t now, alarm_event_t ev) {
    if (p->maint || s->out == NULL || s->n >= ALARM_MAX_NOTES) return;
    s->out[s->n].time = now;
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

/* Emit a TO_x notification for state 'st' (A7). TO_HIGH / TO_LOW start a new
 * alarm episode (A8). Only called when not in maintenance. */
static void notify_state(impl_t *p, sink_t *s, uint32_t now, uint8_t st) {
    emit(p, s, now, to_event(st));
    p->notified_state = st;
    if (st == ALARM_HIGH || st == ALARM_LOW) {
        p->unacked = true;
        p->alarm_time = now;
        p->escalated = false;
    }
}

/* A state transition. In maintenance the state changes silently (A10). */
static void transition(impl_t *p, sink_t *s, uint32_t now, uint8_t st) {
    p->state = st;
    if (!p->maint) notify_state(p, s, now, st);
}

/* Target state for value v in the current state (A2-A4), or TGT_NONE. */
static uint8_t target(const impl_t *p, float v) {
    const alarm_cfg_t *c = &p->cfg;
    bool hi = v > c->high_limit;
    bool lo = v < c->low_limit;
    switch (p->state) {
    case ALARM_NORMAL:
        if (hi) return ALARM_HIGH;
        if (lo) return ALARM_LOW;
        return TGT_NONE;
    case ALARM_HIGH:
        if (lo) return ALARM_LOW;
        if (v < c->high_limit - c->deadband) return ALARM_NORMAL;
        return TGT_NONE;
    case ALARM_LOW:
        if (hi) return ALARM_HIGH;
        if (v > c->low_limit + c->deadband) return ALARM_NORMAL;
        return TGT_NONE;
    default:
        return TGT_NONE;
    }
}

/* Evaluate the delay timer at 'now' (A5a): at most one limit transition. */
static void eval_timer(impl_t *p, sink_t *s, uint32_t now) {
    if (p->state == ALARM_FAULT || p->pending == TGT_NONE) return;
    if (now - p->pend_start < p->cfg.time_delay_s) return;
    uint8_t st = p->pending;
    p->pending = TGT_NONE;
    transition(p, s, now, st);
}

/* End-of-call escalation check (A9). */
static void check_escalation(impl_t *p, sink_t *s, uint32_t now) {
    if (!p->unacked || p->escalated || p->maint) return;
    if (p->cfg.escalate_after_s == 0) return;
    if (now - p->alarm_time < p->cfg.escalate_after_s) return;
    p->escalated = true;
    emit(p, s, now, EV_ESCALATE);
}

/* A12: calls with a 'now' earlier than any seen so far are ignored. */
static bool accept_time(impl_t *p, uint32_t now) {
    if (now < p->last_now) return false;
    p->last_now = now;
    return true;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (a == NULL) return;
    impl_t p;
    memset(&p, 0, sizeof p);
    if (cfg != NULL) p.cfg = *cfg;
    p.last_now = now;
    p.value = 0.0f;
    p.state = ALARM_NORMAL;
    p.pending = TGT_NONE;
    p.notified_state = ALARM_NORMAL;
    memset(a, 0, sizeof *a);
    store(a, &p);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t p;
    load(&p, a);
    if (!accept_time(&p, now)) return 0;
    sink_t s = {out, 0};

    p.value = value;
    p.has_sample = true;

    if (sensor_fault || isnan(value)) {
        /* A6: immediate FAULT from any state; repeated faults do nothing. */
        p.pending = TGT_NONE;
        if (p.state != ALARM_FAULT) transition(&p, &s, now, ALARM_FAULT);
    } else {
        if (p.state == ALARM_FAULT) {
            /* A6a: recovery to NORMAL, then evaluate as a sample in NORMAL. */
            p.pending = TGT_NONE;
            transition(&p, &s, now, ALARM_NORMAL);
        }
        uint8_t tgt = target(&p, value);
        if (tgt == TGT_NONE) {
            p.pending = TGT_NONE;
        } else if (tgt != p.pending) {
            p.pending = tgt;
            p.pend_start = now;
        }
        eval_timer(&p, &s, now);
    }

    check_escalation(&p, &s, now);
    store(a, &p);
    return s.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t p;
    load(&p, a);
    if (!accept_time(&p, now)) return 0;
    sink_t s = {out, 0};

    eval_timer(&p, &s, now);
    check_escalation(&p, &s, now);
    store(a, &p);
    return s.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t p;
    load(&p, a);
    if (!accept_time(&p, now)) return 0;
    sink_t s = {out, 0};

    eval_timer(&p, &s, now);
    p.unacked = false; /* no-op when nothing is unacknowledged */
    check_escalation(&p, &s, now);
    store(a, &p);
    return s.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t p;
    load(&p, a);
    if (!accept_time(&p, now)) return 0;
    sink_t s = {out, 0};

    eval_timer(&p, &s, now);
    if (on && !p.maint) {
        p.maint = true;
    } else if (!on && p.maint) {
        p.maint = false;
        /* A10: one catch-up notification if the front-end shows a stale state. */
        if (p.state != p.notified_state) notify_state(&p, &s, now, p.state);
    }
    check_escalation(&p, &s, now);
    store(a, &p);
    return s.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    if (a == NULL) return ALARM_NORMAL;
    impl_t p;
    load(&p, a);
    return (alarm_state_t)p.state;
}
