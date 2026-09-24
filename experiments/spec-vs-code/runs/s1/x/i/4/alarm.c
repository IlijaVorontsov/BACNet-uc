/* alarm.c - out-of-range alarming for analog input points.
 *
 * Bare-metal C11, no heap, no floating-point library calls beyond <math.h>
 * classification macros. All per-point state lives inside alarm_t.
 *
 * Behaviour (modelled on the BACnet OUT_OF_RANGE event algorithm):
 *
 *  Limits      NORMAL -> HIGH when value > high_limit, NORMAL -> LOW when
 *              value < low_limit (both strict). HIGH -> NORMAL when
 *              value < high_limit - deadband, LOW -> NORMAL when
 *              value > low_limit + deadband (strict). HIGH <-> LOW directly
 *              when the opposite limit is violated; if that and the return
 *              to normal become due at the same instant, the direct
 *              transition wins.
 *
 *  Time delay  Every limit transition (including the return to normal)
 *              requires its condition to hold continuously for
 *              time_delay_s seconds, measured from the first sample that
 *              showed it. A sample that does not show the condition restarts
 *              the timer, even if the previous sample's condition would
 *              already have been due: a fresh measurement overrides the
 *              assumption that the old value persisted. Between samples the
 *              last value is held, so alarm_tick() fires due transitions.
 *              delay = 0 means the transition happens on the sample itself.
 *
 *  Faults      A sample with sensor_fault set, or a non-finite value
 *              (NaN, +/-inf), moves the point to FAULT immediately (no
 *              delay) and restarts all limit timers. The first good sample
 *              moves FAULT -> NORMAL immediately; limit timers start at that
 *              sample (so with delay 0 the same call may also report HIGH or
 *              LOW). An invalid configuration (NaN limit, low > high,
 *              negative or non-finite deadband, NULL cfg) makes the point
 *              FAULT permanently until re-initialised; it is reported by the
 *              first call after alarm_init.
 *
 *  Ack         alarm_ack() acknowledges the current HIGH/LOW/FAULT
 *              occurrence (no notification). It has no effect in NORMAL and
 *              is never carried over to a later occurrence: every new
 *              transition into HIGH, LOW or FAULT needs its own ack.
 *
 *  Escalation  If an occurrence of HIGH, LOW or FAULT is still active and
 *              unacknowledged escalate_after_s seconds after it was reported
 *              to the front-end, one EV_ESCALATE is sent (once per
 *              occurrence). escalate_after_s = 0 disables escalation. An
 *              occurrence that ends (return to normal, other alarm state)
 *              before its deadline is not escalated.
 *
 *  Maintenance While on, the state machine, timers and acks keep working but
 *              no notification (transition or escalation) is sent. When it
 *              is switched off, the front-end is brought up to date: if the
 *              state differs from the last one reported, or an alarm
 *              occurrence began during maintenance, a TO_<state>
 *              notification for the current state is sent. Alarms that began
 *              during maintenance start their escalation clock at that
 *              moment; an alarm reported before maintenance keeps its
 *              original clock, and an escalation that fell due during
 *              maintenance is sent when maintenance ends.
 *
 *  Ordering    Within one call, events that fell due strictly before 'now'
 *              are processed first in chronological order, then the call's
 *              own input (ack, maintenance switch, fault/recovery), then
 *              events due exactly at 'now'. At the same instant a state
 *              change is processed before an escalation, so an alarm that
 *              ends or is acknowledged exactly at its deadline is not
 *              escalated. Every notification carries the 'now' of the call
 *              and the most recent sample value (NaN before the first
 *              sample).
 *
 *  Time        'now' is a wrapping uint32 second counter; all intervals are
 *              computed with modular subtraction, so wrap-around is handled.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define ALARM_MAGIC 0x414C524Du /* "ALRM": detects use before alarm_init */

enum { C_HIGH, C_LOW, C_RET_HIGH, C_RET_LOW, C_COUNT };

typedef struct {
    uint32_t since; /* time of the first sample of the current run */
    uint8_t active;
} cond_t;

typedef struct {
    uint32_t magic;
    float high;
    float low;
    float deadband;
    uint32_t delay;
    uint32_t escalate;
    float last_value;
    uint32_t clock_start; /* when the current off-normal occurrence was reported */
    cond_t cond[C_COUNT];
    uint8_t state;          /* alarm_state_t */
    uint8_t reported_state; /* last state reported to the front-end */
    uint8_t reported;       /* current state/occurrence has been reported */
    uint8_t acked;
    uint8_t escalated;
    uint8_t maintenance;
    uint8_t cfg_error;
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

typedef struct {
    alarm_note_t *out;
    int n;
    uint32_t now;
} ctx_t;

/* alarm_t is copied in and out with memcpy to stay clear of strict-aliasing
 * issues; it is only 128 bytes. */
static bool load(const alarm_t *a, impl_t *s) {
    if (a == NULL) return false;
    memcpy(s, a->storage, sizeof *s);
    return s->magic == ALARM_MAGIC;
}

static void store(alarm_t *a, const impl_t *s) { memcpy(a->storage, s, sizeof *s); }

static bool emit(ctx_t *c, const impl_t *s, alarm_event_t ev) {
    if (c->out == NULL || c->n >= ALARM_MAX_NOTES) return false;
    c->out[c->n].time = c->now;
    c->out[c->n].event = ev;
    c->out[c->n].value = s->last_value;
    c->n++;
    return true;
}

static alarm_event_t event_for(uint8_t state) {
    switch (state) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Report the current state to the front-end if it has not been reported yet
 * and notifications are allowed. */
static void sync(impl_t *s, ctx_t *c) {
    if (s->maintenance || s->reported) return;
    if (s->state != s->reported_state || s->state != ALARM_NORMAL) {
        if (!emit(c, s, event_for(s->state))) return; /* retried on a later call */
    }
    s->reported_state = s->state;
    s->reported = 1;
    if (s->state != ALARM_NORMAL) s->clock_start = c->now;
}

static void transition(impl_t *s, ctx_t *c, uint8_t to) {
    s->state = to;
    if (to != ALARM_NORMAL) {
        s->acked = 0;
        s->escalated = 0;
    }
    s->reported = 0;
    sync(s, c);
}

static void set_cond(impl_t *s, int i, bool holds, uint32_t now) {
    if (!holds) {
        s->cond[i].active = 0;
    } else if (!s->cond[i].active) {
        s->cond[i].active = 1;
        s->cond[i].since = now;
    }
}

static void update_conditions(impl_t *s, float v, uint32_t now) {
    double dv = v;
    set_cond(s, C_HIGH, dv > (double)s->high, now);
    set_cond(s, C_LOW, dv < (double)s->low, now);
    set_cond(s, C_RET_HIGH, dv < (double)s->high - (double)s->deadband, now);
    set_cond(s, C_RET_LOW, dv > (double)s->low + (double)s->deadband, now);
}

static void clear_conditions(impl_t *s) {
    for (int i = 0; i < C_COUNT; i++) s->cond[i].active = 0;
}

/* Is the timed event due at 'now'? 'overdue' receives how long ago it fell due. */
static bool due(uint32_t now, uint32_t start, uint32_t period, bool strict, uint32_t *overdue) {
    uint32_t elapsed = now - start;
    if (elapsed < period) return false;
    *overdue = elapsed - period;
    return !(strict && *overdue == 0);
}

/* Process timed events (limit transitions and escalation) that are due at
 * 'now', earliest first. With 'strict', only events due strictly before now. */
static void process_due(impl_t *s, ctx_t *c, bool strict) {
    static const struct {
        uint8_t from, cond, to;
    } rules[] = {
        /* listed in priority order for events due at the same instant */
        {ALARM_NORMAL, C_HIGH, ALARM_HIGH},     {ALARM_NORMAL, C_LOW, ALARM_LOW},
        {ALARM_HIGH, C_LOW, ALARM_LOW},         {ALARM_LOW, C_HIGH, ALARM_HIGH},
        {ALARM_HIGH, C_RET_HIGH, ALARM_NORMAL}, {ALARM_LOW, C_RET_LOW, ALARM_NORMAL},
    };
    /* Transitions cannot cycle (the conditions involved are mutually
     * exclusive), so a chain is at most two transitions plus one escalation. */
    for (int iter = 0; iter < 8; iter++) {
        bool found = false, is_esc = false;
        uint32_t best = 0, over;
        uint8_t to = 0;
        if (!s->cfg_error) {
            for (unsigned r = 0; r < sizeof rules / sizeof rules[0]; r++) {
                const cond_t *cd = &s->cond[rules[r].cond];
                if (rules[r].from != s->state || !cd->active) continue;
                if (!due(c->now, cd->since, s->delay, strict, &over)) continue;
                if (!found || over > best) {
                    found = true;
                    best = over;
                    to = rules[r].to;
                }
            }
        }
        if (!s->maintenance && s->state != ALARM_NORMAL && s->reported && !s->acked && !s->escalated &&
            s->escalate > 0 && due(c->now, s->clock_start, s->escalate, strict, &over)) {
            /* a state change at the same instant takes precedence */
            if (!found || over > best) {
                found = true;
                is_esc = true;
            }
        }
        if (!found) break;
        if (is_esc) {
            if (!emit(c, s, EV_ESCALATE)) break; /* retried on a later call */
            s->escalated = 1;
        } else {
            transition(s, c, to);
        }
    }
}

static int finish(alarm_t *a, impl_t *s, ctx_t *c) {
    sync(s, c);
    store(a, s);
    return c->n;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    (void)now;
    if (a == NULL) return;
    impl_t s;
    memset(a, 0, sizeof *a);
    memset(&s, 0, sizeof s);
    s.magic = ALARM_MAGIC;
    s.last_value = NAN;
    s.state = ALARM_NORMAL;
    s.reported_state = ALARM_NORMAL;
    s.reported = 1;
    bool valid = false;
    if (cfg != NULL) {
        s.high = cfg->high_limit;
        s.low = cfg->low_limit;
        s.deadband = cfg->deadband;
        s.delay = cfg->time_delay_s;
        s.escalate = cfg->escalate_after_s;
        valid = !isnan(s.high) && !isnan(s.low) && s.low <= s.high && isfinite(s.deadband) && s.deadband >= 0.0f;
    }
    if (!valid) {
        s.cfg_error = 1;
        s.state = ALARM_FAULT;
        s.reported = 0; /* reported by the first call */
    }
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    if (!load(a, &s)) return 0;
    ctx_t c = {out, 0, now};
    bool fault = sensor_fault || !isfinite(value);
    s.last_value = value;
    if (fault) clear_conditions(&s);
    else update_conditions(&s, value, now);
    process_due(&s, &c, true);
    if (fault) {
        if (s.state != ALARM_FAULT) transition(&s, &c, ALARM_FAULT);
    } else if (s.state == ALARM_FAULT && !s.cfg_error) {
        transition(&s, &c, ALARM_NORMAL);
    }
    process_due(&s, &c, false);
    return finish(a, &s, &c);
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    if (!load(a, &s)) return 0;
    ctx_t c = {out, 0, now};
    process_due(&s, &c, false);
    return finish(a, &s, &c);
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    if (!load(a, &s)) return 0;
    ctx_t c = {out, 0, now};
    process_due(&s, &c, true);
    if (s.state != ALARM_NORMAL) s.acked = 1;
    process_due(&s, &c, false);
    return finish(a, &s, &c);
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    if (!load(a, &s)) return 0;
    ctx_t c = {out, 0, now};
    process_due(&s, &c, true);
    s.maintenance = on ? 1 : 0;
    sync(&s, &c); /* on leaving maintenance: bring the front-end up to date */
    process_due(&s, &c, false);
    return finish(a, &s, &c);
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t s;
    if (!load(a, &s)) return ALARM_FAULT;
    return (alarm_state_t)s.state;
}
