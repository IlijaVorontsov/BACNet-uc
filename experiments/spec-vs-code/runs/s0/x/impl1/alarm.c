/* alarm.c - out-of-range alarming for analog input points (see SPEC.md).
 *
 * Bare-metal C11, no heap. All per-point state lives inside alarm_t::storage.
 * The private state is copied in/out with memcpy so that the opaque storage is
 * never accessed through an incompatible lvalue type (strict aliasing).
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define NO_TARGET 0xFFu

typedef struct {
    alarm_cfg_t cfg;          /* copy of the configuration (A1) */
    uint32_t max_now;         /* largest 'now' seen so far, incl. init (A12) */
    uint32_t pend_start;      /* start time of the pending transition (A5a) */
    uint32_t alarm_time;      /* start of the current alarm episode (A8) */
    float last_value;         /* most recent sample value, may be NaN (A7) */
    uint8_t state;            /* alarm_state_t */
    uint8_t last_note_state;  /* state of the last emitted TO_x notification (A10) */
    uint8_t pending;          /* a limit transition is pending */
    uint8_t pend_target;      /* its target state */
    uint8_t has_sample;       /* at least one sample was accepted (A13) */
    uint8_t unacked;          /* current episode is unacknowledged (A8) */
    uint8_t escalated;        /* ESCALATE already emitted in this episode (A9) */
    uint8_t maint;            /* maintenance mode (A10) */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "impl_t does not fit in alarm_t");

typedef struct {
    alarm_note_t *out;
    int n;
} sink_t;

static void load(impl_t *s, const alarm_t *a) { memcpy(s, a, sizeof *s); }
static void store(alarm_t *a, const impl_t *s) { memcpy(a, s, sizeof *s); }

static void emit(sink_t *k, uint32_t now, alarm_event_t ev, float value) {
    if (k->n >= ALARM_MAX_NOTES) return;
    if (k->out) {
        k->out[k->n].time = now;
        k->out[k->n].event = ev;
        k->out[k->n].value = value;
    }
    k->n++;
}

static alarm_event_t to_event(uint8_t st) {
    switch (st) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Emit the TO_x notification for state 'st'; TO_HIGH/TO_LOW start a new alarm
 * episode (A8). TO_NORMAL and TO_FAULT leave the acknowledgement state alone. */
static void notify_state(impl_t *s, sink_t *k, uint32_t now, uint8_t st) {
    emit(k, now, to_event(st), s->last_value);
    s->last_note_state = st;
    if (st == ALARM_HIGH || st == ALARM_LOW) {
        s->unacked = 1;
        s->alarm_time = now;
        s->escalated = 0;
    }
}

/* A state transition (A7); silent and episode-less in maintenance (A10). */
static void transition(impl_t *s, sink_t *k, uint32_t now, uint8_t to) {
    s->state = to;
    if (!s->maint) notify_state(s, k, now, to);
}

/* Target of value v in the current state (A2, A3, A4). */
static uint8_t target_of(const impl_t *s, float v) {
    const float hi = s->cfg.high_limit;
    const float lo = s->cfg.low_limit;
    const bool high_cond = v > hi;
    const bool low_cond = v < lo;
    switch (s->state) {
    case ALARM_NORMAL:
        if (high_cond) return ALARM_HIGH;
        if (low_cond) return ALARM_LOW;
        return NO_TARGET;
    case ALARM_HIGH:
        if (low_cond) return ALARM_LOW;
        if (v < (float)(hi - s->cfg.deadband)) return ALARM_NORMAL;
        return NO_TARGET;
    case ALARM_LOW:
        if (high_cond) return ALARM_HIGH;
        if (v > (float)(lo + s->cfg.deadband)) return ALARM_NORMAL;
        return NO_TARGET;
    default:
        return NO_TARGET; /* FAULT: timers do not run (A6a) */
    }
}

/* Evaluate the timer at 'now' (A5a): at most one limit transition. */
static void eval_timer(impl_t *s, sink_t *k, uint32_t now) {
    if (!s->pending || s->state == ALARM_FAULT) return;
    if (now - s->pend_start >= s->cfg.time_delay_s) {
        s->pending = 0;
        transition(s, k, now, s->pend_target);
    }
}

/* End-of-call escalation check (A9). */
static void escalation_check(impl_t *s, sink_t *k, uint32_t now) {
    if (s->unacked && !s->escalated && !s->maint && s->cfg.escalate_after_s > 0 &&
        now - s->alarm_time >= s->cfg.escalate_after_s) {
        s->escalated = 1;
        emit(k, now, EV_ESCALATE, s->last_value);
    }
}

/* A12: accept the call's time, or reject it when time went backwards. */
static bool accept_time(impl_t *s, uint32_t now) {
    if (now < s->max_now) return false;
    s->max_now = now;
    return true;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    impl_t s;
    memset(&s, 0, sizeof s);
    s.cfg = *cfg;
    s.max_now = now;
    s.state = ALARM_NORMAL;
    s.last_note_state = ALARM_NORMAL;
    s.pend_target = NO_TARGET;
    s.last_value = 0.0f;
    memset(a, 0, sizeof *a);
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    if (!accept_time(&s, now)) return 0;

    s.last_value = value; /* the new value replaces the previous one */
    s.has_sample = 1;

    if (sensor_fault || isnan(value)) {
        /* A6: immediate FAULT, pending cancelled; nothing more if already FAULT. */
        s.pending = 0;
        if (s.state != ALARM_FAULT) transition(&s, &k, now, ALARM_FAULT);
    } else {
        if (s.state == ALARM_FAULT) {
            /* A6a: recovery to NORMAL, then evaluate as a normal sample. */
            s.pending = 0;
            transition(&s, &k, now, ALARM_NORMAL);
        }
        uint8_t t = target_of(&s, value);
        if (t == NO_TARGET) {
            s.pending = 0;
        } else if (!s.pending || s.pend_target != t) {
            s.pending = 1;
            s.pend_target = t;
            s.pend_start = now;
        }
        eval_timer(&s, &k, now);
    }

    escalation_check(&s, &k, now);
    store(a, &s);
    return k.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    if (!accept_time(&s, now)) return 0;
    if (s.has_sample) eval_timer(&s, &k, now);
    escalation_check(&s, &k, now);
    store(a, &s);
    return k.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    if (!accept_time(&s, now)) return 0;
    if (s.has_sample) eval_timer(&s, &k, now);
    s.unacked = 0; /* no-op (ignored) when nothing is unacknowledged */
    escalation_check(&s, &k, now);
    store(a, &s);
    return k.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    if (!accept_time(&s, now)) return 0;
    if (s.has_sample) eval_timer(&s, &k, now);
    if (on && !s.maint) {
        s.maint = 1;
    } else if (!on && s.maint) {
        s.maint = 0;
        if (s.state != s.last_note_state) notify_state(&s, &k, now, s.state); /* catch-up */
    }
    escalation_check(&s, &k, now);
    store(a, &s);
    return k.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t s;
    load(&s, a);
    return (alarm_state_t)s.state;
}
