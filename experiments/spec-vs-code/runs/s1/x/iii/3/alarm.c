/* alarm.c - out-of-range alarming for analog input points (see SPEC.md, alarm.h)
 *
 * Bare-metal C11, no heap. The per-point state lives inside the caller-provided
 * alarm_t; it is copied in/out with memcpy to stay clear of strict-aliasing issues.
 */
#include "alarm.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

/* Pending-transition target. */
enum { TGT_NONE = 0, TGT_NORMAL, TGT_HIGH, TGT_LOW };

typedef struct {
    alarm_cfg_t cfg;
    uint32_t max_now;       /* largest 'now' seen (A12) */
    uint32_t pend_start;    /* start time of the pending transition */
    uint32_t alarm_time;    /* start time of the current alarm episode (A8) */
    float last_value;       /* most recent sample value (A7) */
    uint8_t state;          /* alarm_state_t */
    uint8_t pend_target;    /* TGT_* */
    uint8_t last_notified;  /* state of the last emitted TO_x notification (A10) */
    bool has_sample;        /* at least one sample seen (A13) */
    bool unacked;           /* current episode not acknowledged (A8) */
    bool escalated;         /* ESCALATE already emitted in this episode (A9) */
    bool maint;             /* maintenance mode (A10) */
} alarm_impl_t;

_Static_assert(sizeof(alarm_impl_t) <= sizeof(alarm_t), "alarm_impl_t does not fit in alarm_t");

/* Notification sink for one API call. */
typedef struct {
    alarm_note_t *out;
    int n;
} sink_t;

static void load(alarm_impl_t *s, const alarm_t *a) { memcpy(s, a->storage, sizeof *s); }
static void store(alarm_t *a, const alarm_impl_t *s) { memcpy(a->storage, s, sizeof *s); }

static void emit(sink_t *k, uint32_t now, alarm_event_t ev, float value) {
    if (k->n >= ALARM_MAX_NOTES) return; /* cannot happen: at most 3 notes per call */
    if (k->out != NULL) {
        k->out[k->n].time = now;
        k->out[k->n].event = ev;
        k->out[k->n].value = value;
    }
    k->n++;
}

static alarm_event_t to_event(uint8_t state) {
    switch (state) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* A8: TO_HIGH / TO_LOW start a new alarm episode. */
static void start_episode(alarm_impl_t *s, uint32_t now) {
    s->unacked = true;
    s->alarm_time = now;
    s->escalated = false;
}

/* Emit the TO_x notification for the current state (A7) and apply its side effects. */
static void notify_state(alarm_impl_t *s, sink_t *k, uint32_t now) {
    emit(k, now, to_event(s->state), s->last_value);
    s->last_notified = s->state;
    if (s->state == ALARM_HIGH || s->state == ALARM_LOW) start_episode(s, now);
}

/* Perform a state transition; in maintenance it is silent (A10). */
static void transition(alarm_impl_t *s, sink_t *k, uint32_t now, uint8_t new_state) {
    s->state = new_state;
    if (!s->maint) notify_state(s, k, now);
}

/* A2..A4: target for value v in the current (non-FAULT) state. */
static uint8_t target_for(const alarm_impl_t *s, float v) {
    const alarm_cfg_t *c = &s->cfg;
    const bool high = v > c->high_limit;
    const bool low = v < c->low_limit;
    switch (s->state) {
    case ALARM_NORMAL:
        if (high) return TGT_HIGH;
        if (low) return TGT_LOW;
        return TGT_NONE;
    case ALARM_HIGH:
        if (low) return TGT_LOW;
        if (v < c->high_limit - c->deadband) return TGT_NORMAL;
        return TGT_NONE;
    case ALARM_LOW:
        if (high) return TGT_HIGH;
        if (v > c->low_limit + c->deadband) return TGT_NORMAL;
        return TGT_NONE;
    default:
        return TGT_NONE;
    }
}

static uint8_t target_state(uint8_t tgt) {
    switch (tgt) {
    case TGT_HIGH: return ALARM_HIGH;
    case TGT_LOW: return ALARM_LOW;
    default: return ALARM_NORMAL;
    }
}

/* A5a: evaluate the timer at 'now'; at most one limit transition. */
static void eval_timer(alarm_impl_t *s, sink_t *k, uint32_t now) {
    if (!s->has_sample || s->state == ALARM_FAULT || s->pend_target == TGT_NONE) return;
    if (now - s->pend_start >= s->cfg.time_delay_s) {
        const uint8_t tgt = s->pend_target;
        s->pend_target = TGT_NONE;
        transition(s, k, now, target_state(tgt));
    }
}

/* A9: escalation check at the end of every (non-ignored) call. */
static void check_escalation(alarm_impl_t *s, sink_t *k, uint32_t now) {
    if (s->unacked && !s->escalated && !s->maint && s->cfg.escalate_after_s > 0 &&
        now - s->alarm_time >= s->cfg.escalate_after_s) {
        emit(k, now, EV_ESCALATE, s->last_value);
        s->escalated = true;
    }
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (a == NULL || cfg == NULL) return;
    alarm_impl_t s;
    memset(&s, 0, sizeof s);
    s.cfg = *cfg;
    s.max_now = now;
    s.last_value = 0.0f;
    s.state = ALARM_NORMAL;
    s.pend_target = TGT_NONE;
    s.last_notified = ALARM_NORMAL;
    memset(a, 0, sizeof *a);
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    alarm_impl_t s;
    load(&s, a);
    if (now < s.max_now) return 0; /* A12 */
    s.max_now = now;
    sink_t k = {out, 0};

    s.last_value = value;
    s.has_sample = true;

    if (sensor_fault || isnan(value)) {
        /* A6: immediate FAULT from any state; repeated faults do nothing. */
        s.pend_target = TGT_NONE;
        if (s.state != ALARM_FAULT) transition(&s, &k, now, ALARM_FAULT);
    } else {
        if (s.state == ALARM_FAULT) {
            /* A6a: recovery to NORMAL, then evaluate as a normal sample. */
            s.pend_target = TGT_NONE;
            transition(&s, &k, now, ALARM_NORMAL);
        }
        const uint8_t tgt = target_for(&s, value);
        if (tgt == TGT_NONE) {
            s.pend_target = TGT_NONE;
        } else if (tgt != s.pend_target) {
            s.pend_target = tgt;
            s.pend_start = now;
        }
        eval_timer(&s, &k, now);
    }

    check_escalation(&s, &k, now);
    store(a, &s);
    return k.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    alarm_impl_t s;
    load(&s, a);
    if (now < s.max_now) return 0; /* A12 */
    s.max_now = now;
    sink_t k = {out, 0};

    eval_timer(&s, &k, now);
    check_escalation(&s, &k, now);
    store(a, &s);
    return k.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    alarm_impl_t s;
    load(&s, a);
    if (now < s.max_now) return 0; /* A12 */
    s.max_now = now;
    sink_t k = {out, 0};

    eval_timer(&s, &k, now);
    s.unacked = false; /* A8: no notification; no-op if nothing unacknowledged */
    check_escalation(&s, &k, now);
    store(a, &s);
    return k.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    alarm_impl_t s;
    load(&s, a);
    if (now < s.max_now) return 0; /* A12 */
    s.max_now = now;
    sink_t k = {out, 0};

    eval_timer(&s, &k, now);
    if (on && !s.maint) {
        s.maint = true;
    } else if (!on && s.maint) {
        s.maint = false;
        /* A10: one catch-up notification so the front-end shows the true state. */
        if (s.state != s.last_notified) notify_state(&s, &k, now);
    }
    check_escalation(&s, &k, now);
    store(a, &s);
    return k.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    if (a == NULL) return ALARM_NORMAL;
    alarm_impl_t s;
    load(&s, a);
    return (alarm_state_t)s.state;
}
