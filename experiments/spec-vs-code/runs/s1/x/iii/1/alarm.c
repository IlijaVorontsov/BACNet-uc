/* alarm.c - out-of-range alarming for analog input points (see SPEC.md v1.0).
 *
 * Bare-metal C11, no heap. The per-point state lives inside the caller's
 * alarm_t; it is copied in and out with memcpy so that no strict-aliasing
 * assumptions are made about alarm_t's storage.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define NO_TARGET 0xFFu

typedef struct {
    alarm_cfg_t cfg;          /* copied configuration */
    uint32_t max_now;         /* largest 'now' seen so far (A12) */
    uint32_t pending_start;   /* start of the pending transition (A5a) */
    uint32_t alarm_time;      /* start of the current alarm episode (A8) */
    float last_value;         /* most recent sample value, may be NaN (A7) */
    uint8_t state;            /* alarm_state_t */
    uint8_t pending_target;   /* alarm_state_t or NO_TARGET */
    uint8_t last_notified;    /* state of the last emitted TO_x notification (A10) */
    bool unacked;             /* A8 */
    bool escalated;           /* A9: ESCALATE already sent in this episode */
    bool maintenance;         /* A10 */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "impl_t does not fit in alarm_t");

typedef struct {
    alarm_note_t *out;
    int n;
} notes_t;

static void load(const alarm_t *a, impl_t *s) { memcpy(s, a->storage, sizeof *s); }
static void store(alarm_t *a, const impl_t *s) { memcpy(a->storage, s, sizeof *s); }

static void emit(notes_t *nl, uint32_t now, alarm_event_t ev, float value) {
    if (nl->n < ALARM_MAX_NOTES) {
        nl->out[nl->n].time = now;
        nl->out[nl->n].event = ev;
        nl->out[nl->n].value = value;
        nl->n++;
    }
}

static alarm_event_t to_event(uint8_t state) {
    switch (state) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Emit the TO_x notification for 'state' (unless in maintenance) and, for
 * TO_HIGH/TO_LOW, start a new alarm episode (A7, A8, A10). */
static void notify_state(impl_t *s, uint8_t state, uint32_t now, notes_t *nl) {
    if (s->maintenance) return;
    emit(nl, now, to_event(state), s->last_value);
    s->last_notified = state;
    if (state == ALARM_HIGH || state == ALARM_LOW) {
        s->unacked = true;
        s->alarm_time = now;
        s->escalated = false;
    }
}

static void transition(impl_t *s, uint8_t to, uint32_t now, notes_t *nl) {
    s->state = to;
    notify_state(s, to, now, nl);
}

/* Target for value v in the current state (A2, A3, A4). */
static uint8_t target_for(const impl_t *s, float v) {
    const bool high = v > s->cfg.high_limit;
    const bool low = v < s->cfg.low_limit;
    switch (s->state) {
    case ALARM_NORMAL:
        if (high) return ALARM_HIGH;
        if (low) return ALARM_LOW;
        return NO_TARGET;
    case ALARM_HIGH: {
        const float ret = s->cfg.high_limit - s->cfg.deadband;
        if (low) return ALARM_LOW;
        if (v < ret) return ALARM_NORMAL;
        return NO_TARGET;
    }
    case ALARM_LOW: {
        const float ret = s->cfg.low_limit + s->cfg.deadband;
        if (high) return ALARM_HIGH;
        if (v > ret) return ALARM_NORMAL;
        return NO_TARGET;
    }
    default:
        return NO_TARGET;
    }
}

/* Evaluate the delay timer at 'now' (A5, A5a). At most one limit transition. */
static void eval_timer(impl_t *s, uint32_t now, notes_t *nl) {
    if (s->state == ALARM_FAULT) return; /* timers do not run in FAULT (A6a) */
    if (s->pending_target == NO_TARGET) return;
    if (now - s->pending_start >= s->cfg.time_delay_s) {
        const uint8_t to = s->pending_target;
        s->pending_target = NO_TARGET;
        transition(s, to, now, nl);
    }
}

/* End-of-call escalation check (A9). */
static void check_escalation(impl_t *s, uint32_t now, notes_t *nl) {
    if (s->unacked && !s->escalated && !s->maintenance && s->cfg.escalate_after_s > 0 &&
        now - s->alarm_time >= s->cfg.escalate_after_s) {
        emit(nl, now, EV_ESCALATE, s->last_value);
        s->escalated = true;
    }
}

/* A12: returns false (call ignored) if time went backwards. */
static bool advance_time(impl_t *s, uint32_t now) {
    if (now < s->max_now) return false;
    s->max_now = now;
    return true;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    impl_t s;
    memset(&s, 0, sizeof s);
    s.cfg = *cfg;
    s.max_now = now;
    s.pending_start = now;
    s.alarm_time = now;
    s.last_value = 0.0f;
    s.state = ALARM_NORMAL;
    s.pending_target = NO_TARGET;
    s.last_notified = ALARM_NORMAL;
    s.unacked = false;
    s.escalated = false;
    s.maintenance = false;
    memset(a, 0, sizeof *a);
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    notes_t nl = {out, 0};
    load(a, &s);
    if (!advance_time(&s, now)) return 0;

    s.last_value = value;
    if (sensor_fault || isnan(value)) {
        /* A6: immediate FAULT from any state; nothing more while in FAULT. */
        if (s.state != ALARM_FAULT) {
            s.pending_target = NO_TARGET;
            transition(&s, ALARM_FAULT, now, &nl);
        }
    } else {
        if (s.state == ALARM_FAULT) {
            /* A6a: recover to NORMAL, then evaluate as a sample in NORMAL. */
            s.pending_target = NO_TARGET;
            transition(&s, ALARM_NORMAL, now, &nl);
        }
        const uint8_t target = target_for(&s, value);
        if (target == NO_TARGET) {
            s.pending_target = NO_TARGET;
        } else if (target != s.pending_target) {
            s.pending_target = target;
            s.pending_start = now;
        }
        eval_timer(&s, now, &nl);
    }

    check_escalation(&s, now, &nl);
    store(a, &s);
    return nl.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    notes_t nl = {out, 0};
    load(a, &s);
    if (!advance_time(&s, now)) return 0;
    eval_timer(&s, now, &nl);
    check_escalation(&s, now, &nl);
    store(a, &s);
    return nl.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    notes_t nl = {out, 0};
    load(a, &s);
    if (!advance_time(&s, now)) return 0;
    eval_timer(&s, now, &nl);
    s.unacked = false; /* no-op when nothing is unacknowledged */
    check_escalation(&s, now, &nl);
    store(a, &s);
    return nl.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    notes_t nl = {out, 0};
    load(a, &s);
    if (!advance_time(&s, now)) return 0;
    eval_timer(&s, now, &nl);
    if (on && !s.maintenance) {
        s.maintenance = true;
    } else if (!on && s.maintenance) {
        s.maintenance = false;
        /* A10: one catch-up notification if the front-end shows a stale state. */
        if (s.state != s.last_notified) notify_state(&s, s.state, now, &nl);
    }
    check_escalation(&s, now, &nl);
    store(a, &s);
    return nl.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t s;
    load(a, &s);
    return (alarm_state_t)s.state;
}
