/* alarm.c - out-of-range alarming for analog input points (see alarm.h).
 *
 * Bare-metal C11: no heap, no stdio, no libm calls (only the isnan() macro).
 *
 * Behaviour (modelled on the BACnet OUT_OF_RANGE event algorithm):
 *
 * Limits and time delay
 *   NORMAL -> HIGH    value >  high_limit                for time_delay_s
 *   NORMAL -> LOW     value <  low_limit                 for time_delay_s
 *   HIGH   -> LOW     value <  low_limit                 for time_delay_s
 *   LOW    -> HIGH    value >  high_limit                for time_delay_s
 *   HIGH   -> NORMAL  value <  high_limit - deadband     for time_delay_s
 *   LOW    -> NORMAL  value >  low_limit  + deadband     for time_delay_s
 *   "For time_delay_s" means the condition held on every sample from the first
 *   sample that showed it up to 'now' (a sample's value is held until the next
 *   sample). time_delay_s == 0 means immediately.
 *
 *   Every call evaluates the timers, not only alarm_sample/alarm_tick. If a
 *   call arrives late, conditions that matured before 'now' are processed first,
 *   in time order, with the value held at that time; then the call's own input
 *   (sample, ack, maintenance change) is applied at 'now'; then anything due at
 *   'now' is processed. So the sequence of notifications does not depend on
 *   how often alarm_tick is called; only their timestamps do.
 *
 * Sensor faults
 *   sensor_fault == true or a NaN value -> FAULT immediately (no time delay).
 *   The first good sample after a fault -> NORMAL immediately (TO_NORMAL), and
 *   the limits are evaluated again from that sample, with the time delay
 *   (BACnet fault-clear behaviour). +inf / -inf are ordinary values above /
 *   below the limits.
 *
 * Notifications, acknowledgement, escalation
 *   Each state change produces TO_<state>, stamped with the call's 'now' and
 *   the value held at the time. HIGH, LOW and FAULT are alarms; each alarm
 *   occurrence (every change into an alarm state) needs its own
 *   acknowledgement. alarm_ack acknowledges the current alarm and produces no
 *   notification; in NORMAL it does nothing. If the current alarm is still
 *   unacknowledged escalate_after_s after it was notified, one ESCALATE is
 *   produced (once per occurrence). escalate_after_s == 0 disables escalation.
 *
 * Maintenance mode
 *   Detection keeps running and alarm_state() reports the true state, and
 *   acknowledgements are accepted, but no notifications (TO_* or ESCALATE) are
 *   produced. When maintenance is switched off, the front-end is brought up to
 *   date: an alarm that has not been notified yet is notified then (its
 *   escalation time starts then), and TO_NORMAL is sent if the front-end still
 *   shows an alarm that has cleared. An alarm notified before maintenance keeps
 *   its original escalation time, so an escalation that fell due during
 *   maintenance is sent when maintenance ends.
 *
 * Configuration / robustness
 *   A negative or NaN deadband is treated as 0. If high_limit < low_limit the
 *   two are swapped. A NaN limit never triggers. 'now' is expected to be
 *   monotonic; if it goes backwards, no time is taken to have passed (the
 *   notification still carries the caller's 'now'). Calls on an alarm_t that
 *   was never initialised do nothing and alarm_state() reports ALARM_FAULT.
 */
#include "alarm.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

#define ALARM_MAGIC 0x4D524C41u /* "ALRM" */

/* Conditions tracked with a start time, as bit indexes into 'active'. */
enum { C_ABOVE, C_BELOW, C_CLR_HI, C_CLR_LO, C_COUNT };
#define BIT(c) (1u << (c))

typedef struct {
    uint32_t magic;
    float high, low, deadband;
    uint32_t delay, escalate;
    uint32_t last_now;         /* latest time seen; internal time never goes back */
    float value;               /* last sampled value, held until the next sample */
    uint32_t since[C_COUNT];   /* when each active condition started */
    uint32_t notified_at;      /* when the current alarm was notified */
    uint8_t state;             /* alarm_state_t: the true state */
    uint8_t notified;          /* alarm_state_t: the last state notified */
    uint8_t active;            /* BIT(C_*) of the conditions holding now */
    bool maint;                /* maintenance mode */
    bool announced;            /* current state has been notified */
    bool acked;                /* current alarm has been acknowledged */
    bool escalated;            /* current alarm has been escalated */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

/* Output for one API call. */
typedef struct {
    alarm_note_t *out;
    int n;
    uint32_t stamp; /* the caller's 'now', written into notifications */
    uint32_t t;     /* 'now' clamped to be monotonic, used for timing */
} sink_t;

/* Transitions driven by a condition that has held for the time delay. For a
 * given state the alarm rule comes first, so it wins a tie. */
static const struct {
    uint8_t from, cond, to;
} rules[] = {
    {ALARM_NORMAL, C_ABOVE, ALARM_HIGH},
    {ALARM_NORMAL, C_BELOW, ALARM_LOW},
    {ALARM_HIGH, C_BELOW, ALARM_LOW},
    {ALARM_HIGH, C_CLR_HI, ALARM_NORMAL},
    {ALARM_LOW, C_ABOVE, ALARM_HIGH},
    {ALARM_LOW, C_CLR_LO, ALARM_NORMAL},
};

/* The state lives in the caller's alarm_t; copy it in and out rather than
 * casting, so no object is accessed through an incompatible type. */
static bool load(const alarm_t *a, impl_t *s) {
    if (a == NULL) return false;
    memcpy(s, a, sizeof *s);
    return s->magic == ALARM_MAGIC;
}

static void store(alarm_t *a, const impl_t *s) { memcpy(a, s, sizeof *s); }

static bool begin(const alarm_t *a, impl_t *s, sink_t *k, uint32_t now, alarm_note_t *out) {
    k->out = out;
    k->n = 0;
    k->stamp = now;
    if (!load(a, s)) return false;
    if (now > s->last_now) s->last_now = now;
    k->t = s->last_now;
    return true;
}

static void emit(sink_t *k, alarm_event_t ev, float value) {
    if (k->out == NULL) return;
    if (k->n == ALARM_MAX_NOTES) {
        /* Not reachable with the rules above (at most 4 per call); if it ever
         * were, keep the newest notifications, which carry the current state. */
        memmove(&k->out[0], &k->out[1], (ALARM_MAX_NOTES - 1) * sizeof k->out[0]);
        k->n--;
    }
    k->out[k->n].time = k->stamp;
    k->out[k->n].event = ev;
    k->out[k->n].value = value;
    k->n++;
}

static alarm_event_t event_for(uint8_t state) {
    switch (state) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Tell the front-end about the current state. */
static void announce(impl_t *s, sink_t *k) {
    emit(k, event_for(s->state), s->value);
    s->notified = s->state;
    s->notified_at = k->t;
    s->announced = true;
}

static void enter(impl_t *s, uint8_t to, sink_t *k) {
    s->state = to;
    s->announced = false;
    s->acked = false;
    s->escalated = false;
    if (!s->maint) announce(s, k);
}

/* Start or stop the condition timers for a good sample taken at time t. */
static void track(impl_t *s, float v, uint32_t t) {
    unsigned holds = 0;
    if (v > s->high) holds |= BIT(C_ABOVE);
    if (v < s->low) holds |= BIT(C_BELOW);
    if (v < s->high - s->deadband) holds |= BIT(C_CLR_HI);
    if (v > s->low + s->deadband) holds |= BIT(C_CLR_LO);
    for (unsigned c = 0; c < C_COUNT; c++)
        if ((holds & BIT(c)) && !(s->active & BIT(c))) s->since[c] = t;
    s->active = (uint8_t)holds;
}

/* Earliest transition out of the current state, and when its condition matures. */
static bool next_transition(const impl_t *s, uint64_t *when, uint8_t *to) {
    bool found = false;
    for (size_t i = 0; i < sizeof rules / sizeof rules[0]; i++) {
        if (rules[i].from != s->state || !(s->active & BIT(rules[i].cond))) continue;
        uint64_t due = (uint64_t)s->since[rules[i].cond] + s->delay;
        if (!found || due < *when) {
            *when = due;
            *to = rules[i].to;
            found = true;
        }
    }
    return found;
}

static bool escalation_due(const impl_t *s, uint64_t *when) {
    if (s->escalate == 0 || s->maint || s->state == ALARM_NORMAL || !s->announced || s->acked ||
        s->escalated)
        return false;
    *when = (uint64_t)s->notified_at + s->escalate;
    return true;
}

/* Process, in time order, transitions and escalations due before 'now'
 * (or at 'now' too when 'inclusive'). */
static void advance(impl_t *s, sink_t *k, bool inclusive) {
    const uint64_t now = k->t;
    for (int guard = 0; guard < 8; guard++) {
        uint64_t t_tr = 0, t_esc = 0;
        uint8_t to = ALARM_NORMAL;
        bool tr = next_transition(s, &t_tr, &to) && (t_tr < now || (inclusive && t_tr == now));
        bool esc = escalation_due(s, &t_esc) && (t_esc < now || (inclusive && t_esc == now));
        if (tr && (!esc || t_tr <= t_esc)) {
            enter(s, to, k);
        } else if (esc) {
            emit(k, EV_ESCALATE, s->value);
            s->escalated = true;
        } else {
            return;
        }
    }
}

static int finish(alarm_t *a, impl_t *s, sink_t *k) {
    advance(s, k, true);
    store(a, s);
    return k->n;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (a == NULL) return;
    impl_t s;
    memset(&s, 0, sizeof s);
    s.magic = ALARM_MAGIC;
    s.high = INFINITY; /* no configuration: never alarm */
    s.low = -INFINITY;
    if (cfg != NULL) {
        s.high = cfg->high_limit;
        s.low = cfg->low_limit;
        s.deadband = cfg->deadband >= 0.0f ? cfg->deadband : 0.0f; /* also catches NaN */
        s.delay = cfg->time_delay_s;
        s.escalate = cfg->escalate_after_s;
        if (s.high < s.low) {
            float tmp = s.high;
            s.high = s.low;
            s.low = tmp;
        }
    }
    s.last_now = now;
    s.state = ALARM_NORMAL;
    s.notified = ALARM_NORMAL;
    s.announced = true;
    s.acked = true;
    memset(a, 0, sizeof *a);
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k;
    if (!begin(a, &s, &k, now, out)) return 0;
    advance(&s, &k, false);
    s.value = value;
    if (sensor_fault || isnan(value)) {
        s.active = 0;
        if (s.state != ALARM_FAULT) enter(&s, ALARM_FAULT, &k);
    } else {
        if (s.state == ALARM_FAULT) enter(&s, ALARM_NORMAL, &k);
        track(&s, value, k.t);
    }
    return finish(a, &s, &k);
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k;
    if (!begin(a, &s, &k, now, out)) return 0;
    advance(&s, &k, false);
    return finish(a, &s, &k);
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k;
    if (!begin(a, &s, &k, now, out)) return 0;
    advance(&s, &k, false);
    if (s.state != ALARM_NORMAL) s.acked = true;
    return finish(a, &s, &k);
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t s;
    sink_t k;
    if (!begin(a, &s, &k, now, out)) return 0;
    advance(&s, &k, false);
    if (on) {
        s.maint = true;
    } else if (s.maint) {
        s.maint = false;
        if (s.state != ALARM_NORMAL ? !s.announced : s.notified != ALARM_NORMAL) announce(&s, &k);
        s.announced = true;
    }
    return finish(a, &s, &k);
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t s;
    if (!load(a, &s)) return ALARM_FAULT;
    return (alarm_state_t)s.state;
}
