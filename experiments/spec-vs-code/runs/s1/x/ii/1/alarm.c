/* alarm.c - out-of-range alarming for analog input points.
 *
 * Behaviour (modelled on the BACnet OUT_OF_RANGE event algorithm):
 *
 * Detection
 *   - Off-normal conditions: value > high_limit (HIGH), value < low_limit (LOW).
 *   - Return conditions:     value < high_limit - deadband (from HIGH),
 *                            value > low_limit + deadband  (from LOW).
 *     Comparisons are strict. A negative or NaN deadband is treated as 0.
 *   - Every transition between NORMAL/HIGH/LOW (including HIGH <-> LOW directly)
 *     requires its condition to hold continuously for time_delay_s. Between
 *     samples the last value is assumed to hold, so a delay can expire on a
 *     tick (or any other call). A sample that breaks a condition restarts it.
 *   - A sample with sensor_fault set, or a non-finite value (NaN/+-inf), moves
 *     the point to FAULT immediately. The first good sample after that returns
 *     it to NORMAL immediately; condition timers restart from that sample.
 *
 * Notifications
 *   - A TO_<state> note is issued when the state differs from the state last
 *     reported to the front-end. If several transitions happen inside one call
 *     only the resulting state is reported.
 *   - In maintenance mode the detector keeps running (alarm_state() shows the
 *     real state) but no notes of any kind are issued. When maintenance ends,
 *     one TO_<state> note is issued if the state differs from the one reported
 *     before maintenance.
 *
 * Acknowledgement and escalation
 *   - Each reported transition into HIGH, LOW or FAULT starts a new alarm that
 *     needs acknowledgement. alarm_ack() acknowledges the reported alarm; it is
 *     a no-op when the reported state is NORMAL.
 *   - One ESCALATE note is issued when an alarm has been unacknowledged for
 *     escalate_after_s seconds. Time spent in maintenance mode does not count.
 *     A return to NORMAL or a new alarm cancels a pending escalation.
 *     escalate_after_s == 0 disables escalation.
 *
 * Call semantics
 *   - Each call first applies its own input (value, ack, maintenance switch),
 *     then evaluates everything that is due at 'now'. So an ack or a sample
 *     arriving exactly at a deadline takes effect before the deadline fires.
 *   - Note values are the most recent sample value.
 *   - Elapsed times are computed without overflow; if 'now' goes backwards the
 *     elapsed time is taken as 0.
 */
#include "alarm.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

enum { C_HIGH, C_LOW, C_RET_HIGH, C_RET_LOW, C_COUNT };

typedef struct {
    alarm_cfg_t cfg;
    float value;                  /* most recent sample value */
    uint32_t cond_since[C_COUNT]; /* start of the current run of each condition */
    uint32_t esc_accum;           /* unacked alarm time counted before esc_resume */
    uint32_t esc_resume;          /* start of the current counting interval */
    uint8_t cond_on;              /* bit c set: condition c currently true */
    uint8_t state;                /* alarm_state_t: detector state */
    uint8_t reported;             /* alarm_state_t: state last reported */
    bool fault;                   /* most recent sample was a sensor fault */
    bool maint;
    bool acked;
    bool escalated;
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "impl_t does not fit in alarm_t");

typedef struct {
    alarm_note_t *out;
    int n;
} notes_t;

/* alarm_t is opaque storage; copy in and out to stay clear of aliasing rules. */
static void load(impl_t *s, const alarm_t *a) { memcpy(s, a->storage, sizeof *s); }
static void store(alarm_t *a, const impl_t *s) { memcpy(a->storage, s, sizeof *s); }

static uint32_t elapsed(uint32_t now, uint32_t since) { return now >= since ? now - since : 0u; }

static bool cond_held(const impl_t *s, int c, uint32_t now) {
    return (s->cond_on & (1u << c)) && elapsed(now, s->cond_since[c]) >= s->cfg.time_delay_s;
}

static void emit(notes_t *nt, uint32_t now, alarm_event_t ev, float value) {
    if (nt->out && nt->n < ALARM_MAX_NOTES) {
        nt->out[nt->n].time = now;
        nt->out[nt->n].event = ev;
        nt->out[nt->n].value = value;
        nt->n++;
    }
}

static void update_conditions(impl_t *s, float v, uint32_t now) {
    const float hi = s->cfg.high_limit, lo = s->cfg.low_limit, db = s->cfg.deadband;
    const bool c[C_COUNT] = {
        [C_HIGH] = v > hi,
        [C_LOW] = v < lo,
        [C_RET_HIGH] = v < hi - db,
        [C_RET_LOW] = v > lo + db,
    };
    for (int i = 0; i < C_COUNT; i++) {
        uint8_t bit = (uint8_t)(1u << i);
        if (!c[i]) {
            s->cond_on &= (uint8_t)~bit;
        } else if (!(s->cond_on & bit)) {
            s->cond_on |= bit;
            s->cond_since[i] = now;
        }
    }
}

static void detect(impl_t *s, uint32_t now) {
    /* At most FAULT -> NORMAL -> HIGH/LOW can happen in one evaluation. */
    for (int i = 0; i < 4; i++) {
        alarm_state_t next = (alarm_state_t)s->state;
        if (s->fault) {
            next = ALARM_FAULT;
        } else {
            switch ((alarm_state_t)s->state) {
            case ALARM_FAULT:
                next = ALARM_NORMAL;
                break;
            case ALARM_NORMAL:
                if (cond_held(s, C_HIGH, now)) next = ALARM_HIGH;
                else if (cond_held(s, C_LOW, now)) next = ALARM_LOW;
                break;
            case ALARM_HIGH:
                if (cond_held(s, C_LOW, now)) next = ALARM_LOW;
                else if (cond_held(s, C_RET_HIGH, now)) next = ALARM_NORMAL;
                break;
            case ALARM_LOW:
                if (cond_held(s, C_HIGH, now)) next = ALARM_HIGH;
                else if (cond_held(s, C_RET_LOW, now)) next = ALARM_NORMAL;
                break;
            default:
                next = ALARM_NORMAL;
                break;
            }
        }
        if (next == (alarm_state_t)s->state) break;
        s->state = (uint8_t)next;
    }
}

static alarm_event_t event_for(alarm_state_t st) {
    switch (st) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

static void notify(impl_t *s, uint32_t now, notes_t *nt) {
    if (s->maint) return;
    if (s->state != s->reported) {
        s->reported = s->state;
        emit(nt, now, event_for((alarm_state_t)s->state), s->value);
        /* A new alarm (or none): reset acknowledgement and escalation. */
        s->acked = false;
        s->escalated = false;
        s->esc_accum = 0;
        s->esc_resume = now;
    }
    if (s->reported != ALARM_NORMAL && !s->acked && !s->escalated && s->cfg.escalate_after_s > 0) {
        uint64_t unacked = (uint64_t)s->esc_accum + elapsed(now, s->esc_resume);
        if (unacked >= s->cfg.escalate_after_s) {
            s->escalated = true;
            emit(nt, now, EV_ESCALATE, s->value);
        }
    }
}

static int finish(alarm_t *a, impl_t *s, uint32_t now, alarm_note_t *out) {
    notes_t nt = {out, 0};
    detect(s, now);
    notify(s, now, &nt);
    store(a, s);
    return nt.n;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (!a) return;
    impl_t s;
    memset(&s, 0, sizeof s);
    if (cfg) s.cfg = *cfg;
    if (!(s.cfg.deadband > 0.0f)) s.cfg.deadband = 0.0f; /* also catches NaN */
    s.state = ALARM_NORMAL;
    s.reported = ALARM_NORMAL;
    s.esc_resume = now;
    memset(a, 0, sizeof *a);
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (!a) return 0;
    impl_t s;
    load(&s, a);
    s.value = value;
    if (sensor_fault || !isfinite(value)) {
        s.fault = true;
        s.cond_on = 0;
    } else {
        s.fault = false;
        update_conditions(&s, value, now);
    }
    return finish(a, &s, now, out);
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (!a) return 0;
    impl_t s;
    load(&s, a);
    return finish(a, &s, now, out);
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (!a) return 0;
    impl_t s;
    load(&s, a);
    if (s.reported != ALARM_NORMAL) s.acked = true;
    return finish(a, &s, now, out);
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (!a) return 0;
    impl_t s;
    load(&s, a);
    if (on && !s.maint) {
        /* Pause the escalation clock (saturating add). */
        uint64_t acc = (uint64_t)s.esc_accum + elapsed(now, s.esc_resume);
        s.esc_accum = acc > UINT32_MAX ? UINT32_MAX : (uint32_t)acc;
        s.maint = true;
    } else if (!on && s.maint) {
        s.maint = false;
        s.esc_resume = now;
    }
    return finish(a, &s, now, out);
}

alarm_state_t alarm_state(const alarm_t *a) {
    if (!a) return ALARM_NORMAL;
    impl_t s;
    load(&s, a);
    return (alarm_state_t)s.state;
}
