/* alarm.c - out-of-range alarming for analog input points.
 *
 * Bare-metal C11, no heap, no floating-point library calls beyond <math.h>
 * classification macros.
 *
 * Behaviour (the parts the header does not pin down are decided here):
 *
 * Limits / deadband / time delay (BACnet OUT_OF_RANGE style)
 *   - NORMAL -> HIGH when value > high_limit, NORMAL -> LOW when value < low_limit.
 *   - HIGH clears when value < high_limit - deadband, LOW clears when
 *     value > low_limit + deadband.  HIGH <-> LOW is possible directly.
 *   - Every limit transition (into and out of alarm) happens only after the value
 *     has continuously indicated the same new state for time_delay_s seconds.
 *     If the indicated state changes, the timer restarts.  delay 0 = immediate.
 *   - A point never leaves HIGH while its value is still above high_limit (and
 *     likewise for LOW), so misconfigured limits (low > high) cannot oscillate.
 *   - A negative or NaN deadband is treated as 0.  A NaN limit disables that limit.
 *
 * Samples are "sample and hold": a value holds until the next sample.  Every
 * call at time 'now' first processes timed events (delayed transitions,
 * escalation) that fell due strictly before 'now', then applies its own input
 * (sample / ack / maintenance change), then processes events due at exactly
 * 'now'.  So the outcome does not depend on how often alarm_tick() is called
 * (only the timestamps on notes do: a note carries the 'now' of the call that
 * produced it).  When two timed events fall due together, the transition wins
 * and an escalation of an alarm that ends at that instant is not sent.
 *
 * Sensor faults
 *   - sensor_fault == true, or a non-finite value (NaN, +/-inf), puts the point
 *     in FAULT immediately (no time delay) from any state.
 *   - The first valid sample leaves FAULT immediately to NORMAL (TO_NORMAL, as in
 *     BACnet); an out-of-range value then starts a fresh time delay.
 *
 * Acknowledgement and escalation
 *   - Every entry into HIGH, LOW or FAULT is a new alarm occurrence that is
 *     unacknowledged.  alarm_ack() acknowledges the occurrence the front-end has
 *     been told about (never one announced in the same call, never one that was
 *     not announced because of maintenance).  Ack in NORMAL is a no-op.
 *   - EV_ESCALATE is sent once per occurrence when the point has been in that
 *     alarm, announced and unacknowledged, for escalate_after_s seconds (counted
 *     from the announcement).  escalate_after_s == 0 escalates immediately.
 *     An alarm that returns to NORMAL is not escalated.
 *
 * Maintenance mode
 *   - The state machine keeps running (alarm_state() shows the real state) but
 *     no notifications are produced.
 *   - Ack still works on an alarm that was announced before maintenance.
 *   - On leaving maintenance the front-end is brought up to date: if the state
 *     differs from what was last announced, or a new alarm occurrence started
 *     during maintenance, TO_<state> is sent (fresh, unacknowledged, escalation
 *     clock starts).  An alarm that came and went during maintenance is not
 *     reported.  An announced alarm whose escalation fell due during maintenance
 *     and is still active and unacknowledged is escalated on exit.
 *
 * Time
 *   - 'now' is a free-running uint32 seconds counter; wraparound is handled.
 *     A step of more than 2^31 s is taken as the clock going backwards and is
 *     ignored (no time passes) instead of firing every timer at once.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define ALARM_MAGIC 0x414C524Du /* "ALRM" */

typedef struct {
    uint32_t magic;
    alarm_cfg_t cfg;      /* sanitised copy */
    uint32_t last_raw;    /* latest 'now' accepted */
    uint64_t clk;         /* internal monotonic seconds since init */
    uint64_t pend_since;  /* since when the held value has indicated 'pend' */
    uint64_t announce_t;  /* when the current occurrence was announced */
    uint32_t occ;         /* occurrence counter */
    float value;          /* last sampled value (held) */
    uint8_t state;        /* alarm_state_t */
    uint8_t pend;         /* state the held value indicates; == state if none */
    uint8_t notified;     /* last state announced to the front-end */
    uint8_t maint;        /* maintenance mode */
    uint8_t announced;    /* current occurrence has been announced */
    uint8_t acked;        /* current occurrence acknowledged */
    uint8_t escalated;    /* current occurrence escalated */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

typedef struct {
    alarm_note_t *out;
    int n;
    uint32_t now;
} sink_t;

/* Access the opaque storage through memcpy to stay clear of aliasing rules. */
static int load(const alarm_t *a, impl_t *p) {
    if (!a) return 0;
    memcpy(p, a->storage, sizeof *p);
    return p->magic == ALARM_MAGIC;
}

static void store(alarm_t *a, const impl_t *p) { memcpy(a->storage, p, sizeof *p); }

static void emit(const impl_t *p, sink_t *s, alarm_event_t ev) {
    if (!s->out || s->n >= ALARM_MAX_NOTES) return;
    s->out[s->n].time = s->now;
    s->out[s->n].event = ev;
    s->out[s->n].value = p->value;
    s->n++;
}

static alarm_event_t event_for(uint8_t st) {
    switch (st) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

static void advance_clock(impl_t *p, uint32_t now) {
    uint32_t step = now - p->last_raw; /* modulo 2^32: handles wraparound */
    if (step <= 0x7FFFFFFFu) {
        p->clk += step;
        p->last_raw = now;
    }
}

/* State the value indicates, given the current (non-fault) state. */
static uint8_t target_of(const impl_t *p, float v) {
    const alarm_cfg_t *c = &p->cfg;
    switch (p->state) {
    case ALARM_HIGH:
        if (!(v < c->high_limit - c->deadband)) return ALARM_HIGH;
        return v < c->low_limit ? ALARM_LOW : ALARM_NORMAL;
    case ALARM_LOW:
        if (!(v > c->low_limit + c->deadband)) return ALARM_LOW;
        return v > c->high_limit ? ALARM_HIGH : ALARM_NORMAL;
    case ALARM_NORMAL:
        if (v > c->high_limit) return ALARM_HIGH;
        if (v < c->low_limit) return ALARM_LOW;
        return ALARM_NORMAL;
    default:
        return p->state;
    }
}

static void announce(impl_t *p, sink_t *s) {
    emit(p, s, event_for(p->state));
    p->notified = p->state;
    p->announced = 1;
    p->acked = 0;
    p->escalated = 0;
    p->announce_t = p->clk;
}

static void enter(impl_t *p, sink_t *s, uint8_t st) {
    p->state = st;
    p->pend = st;
    p->occ++;
    p->announced = 0;
    p->acked = 0;
    p->escalated = 0;
    if (!p->maint) announce(p, s);
}

/* Process timed events due before the clock (inclusive: also those due now),
 * earliest first. */
static void run_timers(impl_t *p, sink_t *s, int inclusive) {
    for (int guard = 0; guard < 8; guard++) {
        int tr = 0, es = 0;
        uint64_t tr_due = 0, es_due = 0;
        if (p->state != ALARM_FAULT && p->pend != p->state) {
            tr_due = p->pend_since + p->cfg.time_delay_s;
            tr = inclusive ? tr_due <= p->clk : tr_due < p->clk;
        }
        if (p->state != ALARM_NORMAL && p->announced && !p->acked && !p->escalated && !p->maint) {
            es_due = p->announce_t + p->cfg.escalate_after_s;
            es = inclusive ? es_due <= p->clk : es_due < p->clk;
        }
        if (es && (!tr || es_due < tr_due)) {
            p->escalated = 1;
            emit(p, s, EV_ESCALATE);
        } else if (tr) {
            enter(p, s, p->pend);
        } else {
            break;
        }
    }
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (!a) return;
    impl_t p;
    memset(&p, 0, sizeof p);
    p.magic = ALARM_MAGIC;
    if (cfg) {
        p.cfg = *cfg;
    } else {
        p.cfg.high_limit = NAN;
        p.cfg.low_limit = NAN;
    }
    if (!(p.cfg.deadband >= 0.0f)) p.cfg.deadband = 0.0f;
    p.last_raw = now;
    p.value = NAN;
    p.state = p.pend = p.notified = ALARM_NORMAL;
    p.announced = 1;
    memset(a, 0, sizeof *a);
    store(a, &p);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    if (!load(a, &p)) return 0;
    sink_t s = {out, 0, now};
    advance_clock(&p, now);
    run_timers(&p, &s, 0);
    p.value = value;
    if (sensor_fault || !isfinite(value)) {
        if (p.state != ALARM_FAULT) enter(&p, &s, ALARM_FAULT);
    } else {
        if (p.state == ALARM_FAULT) enter(&p, &s, ALARM_NORMAL);
        uint8_t t = target_of(&p, value);
        if (t != p.pend) {
            p.pend = t;
            p.pend_since = p.clk;
        }
    }
    run_timers(&p, &s, 1);
    store(a, &p);
    return s.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    if (!load(a, &p)) return 0;
    sink_t s = {out, 0, now};
    advance_clock(&p, now);
    run_timers(&p, &s, 1);
    store(a, &p);
    return s.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    if (!load(a, &p)) return 0;
    sink_t s = {out, 0, now};
    advance_clock(&p, now);
    uint32_t occ_before = p.occ;
    run_timers(&p, &s, 0);
    /* Only an occurrence the operator could have seen before this call. */
    if (p.occ == occ_before && p.state != ALARM_NORMAL && p.announced) p.acked = 1;
    run_timers(&p, &s, 1);
    store(a, &p);
    return s.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    if (!load(a, &p)) return 0;
    sink_t s = {out, 0, now};
    advance_clock(&p, now);
    run_timers(&p, &s, 0);
    if (on) {
        p.maint = 1;
    } else if (p.maint) {
        p.maint = 0;
        if (!p.announced) {
            if (p.state != ALARM_NORMAL || p.notified != ALARM_NORMAL) announce(&p, &s);
            else p.announced = 1; /* alarm came and went during maintenance */
        }
    }
    run_timers(&p, &s, 1);
    store(a, &p);
    return s.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t p;
    if (!load(a, &p)) return ALARM_FAULT;
    return (alarm_state_t)p.state;
}
