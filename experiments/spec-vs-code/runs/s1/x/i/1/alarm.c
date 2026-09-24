/* alarm.c - out-of-range alarming for analog input points (C11, no heap).
 *
 * Behaviour (modelled on the BACnet OUT_OF_RANGE event algorithm):
 *
 * Limits / deadband / time delay
 *   - Alarm conditions: value > high_limit (HIGH), value < low_limit (LOW).
 *   - Return conditions: from HIGH value < high_limit - deadband,
 *                        from LOW  value > low_limit + deadband.
 *   - Every transition between NORMAL/HIGH/LOW requires its condition to hold
 *     continuously for time_delay_s (0 = immediate). The last sample is held
 *     until the next one, so a delay can also expire on alarm_tick.
 *   - HIGH <-> LOW may happen directly. A return to NORMAL is not reported
 *     while the value sits in the opposite alarm band: the direct transition
 *     is awaited instead, so TO_NORMAL never carries an out-of-range value.
 *   - A sample at time T supersedes the held value at T: timers expiring at
 *     exactly T are judged with the new sample.
 *
 * Faults
 *   - sensor_fault, or a non-finite value (NaN/Inf), moves the point to FAULT
 *     immediately (no delay). The first good sample returns it to NORMAL
 *     immediately (TO_NORMAL), after which the limits are evaluated afresh,
 *     time delay included.
 *   - An invalid configuration (NaN limit, deadband negative/NaN/Inf,
 *     low_limit > high_limit) latches the point in FAULT until re-init.
 *     alarm_init cannot emit notes, so TO_FAULT is sent by the next call.
 *
 * Acknowledgement / escalation
 *   - Every transition into HIGH, LOW or FAULT is a new alarm occurrence that
 *     needs acknowledgement; returning to NORMAL clears it.
 *   - ESCALATE is sent once per occurrence if it is still active and
 *     unacknowledged escalate_after_s after it was notified.
 *     escalate_after_s == 0 disables escalation.
 *
 * Maintenance
 *   - State tracking continues, but no notifications are sent and the
 *     escalation clock is paused. When maintenance ends, the current state is
 *     re-announced if it differs from what was last notified, or if a new
 *     alarm occurrence started during maintenance (its escalation clock then
 *     starts at that moment); otherwise the paused escalation clock resumes.
 *
 * Time
 *   - 'now' is compared with wrap-around arithmetic; a 'now' earlier than the
 *     latest one seen is treated as no time having passed. Notes carry the
 *     'now' argument of the call that produced them.
 *   - Events that became due between two calls are processed in time order,
 *     and all of them are reported by the later call.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define ALARM_MAGIC 0x414C524Du

enum { C_ABOVE_HIGH, C_BELOW_LOW, C_BELOW_HIGH_RTN, C_ABOVE_LOW_RTN, C_COUNT };

enum {
    F_MAINT = 1u << 0,
    F_UNACKED = 1u << 1,
    F_ESCALATED = 1u << 2,
    F_DIRTY = 1u << 3, /* a transition happened while in maintenance */
    F_CFG_ERR = 1u << 4
};

typedef struct {
    uint32_t magic;
    alarm_cfg_t cfg;
    uint32_t last_t;           /* latest effective time seen */
    float value;               /* last sampled value */
    uint32_t since[C_COUNT];   /* condition continuously true since */
    uint32_t esc_start;        /* escalation clock start (notification time) */
    uint32_t esc_banked;       /* escalation time elapsed before maintenance */
    uint8_t cond_valid;        /* bit k set: condition k currently true */
    uint8_t state;             /* alarm_state_t */
    uint8_t reported;          /* state last announced to the front-end */
    uint8_t flags;
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

typedef struct {
    impl_t s;
    uint32_t now;   /* as passed by the caller (stamped on notes) */
    uint32_t t;     /* effective, never-decreasing time */
    alarm_note_t *out;
    int n;
} ctx_t;

enum { K_NONE, K_TRANSITION, K_ESCALATE };

static bool load(ctx_t *c, alarm_t *a, uint32_t now, alarm_note_t *out) {
    if (!a) return false;
    memcpy(&c->s, a, sizeof c->s);
    if (c->s.magic != ALARM_MAGIC) return false;
    c->now = now;
    c->t = ((int32_t)(now - c->s.last_t) < 0) ? c->s.last_t : now;
    c->s.last_t = c->t;
    c->out = out;
    c->n = 0;
    return true;
}

static int store(ctx_t *c, alarm_t *a) {
    memcpy(a, &c->s, sizeof c->s);
    return c->n;
}

static bool emit(ctx_t *c, alarm_event_t ev) {
    if (!c->out || c->n >= ALARM_MAX_NOTES) return false;
    c->out[c->n].time = c->now;
    c->out[c->n].event = ev;
    c->out[c->n].value = c->s.value;
    c->n++;
    return true;
}

static alarm_event_t to_event(uint8_t st) {
    switch (st) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

/* Tell the front-end about the current state. */
static void announce(ctx_t *c) {
    impl_t *s = &c->s;
    if (!emit(c, to_event(s->state))) return; /* retried by flush() */
    s->reported = s->state;
    if (s->state != ALARM_NORMAL) s->esc_start = c->t;
}

static void flush(ctx_t *c) {
    if (!(c->s.flags & F_MAINT) && c->s.state != c->s.reported) announce(c);
}

static void transition(ctx_t *c, uint8_t to) {
    impl_t *s = &c->s;
    s->state = to;
    s->flags &= (uint8_t)~(F_UNACKED | F_ESCALATED);
    if (to != ALARM_NORMAL) {
        s->flags |= F_UNACKED;
        s->esc_banked = 0;
    }
    if (s->flags & F_MAINT) s->flags |= F_DIRTY;
    else announce(c);
}

static bool cond_holds(const impl_t *s, int k, float v) {
    const alarm_cfg_t *g = &s->cfg;
    switch (k) {
    case C_ABOVE_HIGH: return v > g->high_limit;
    case C_BELOW_LOW: return v < g->low_limit;
    case C_BELOW_HIGH_RTN: return v < g->high_limit - g->deadband;
    default: return v > g->low_limit + g->deadband;
    }
}

static bool cond_now(const impl_t *s, int k) { return (s->cond_valid >> k) & 1u; }

static void update_conditions(impl_t *s, uint32_t t, float v) {
    for (int k = 0; k < C_COUNT; k++) {
        if (cond_holds(s, k, v)) {
            if (!cond_now(s, k)) {
                s->since[k] = t;
                s->cond_valid |= (uint8_t)(1u << k);
            }
        } else {
            s->cond_valid &= (uint8_t)~(1u << k);
        }
    }
}

/* How long ago (seconds, may be negative) an interval starting at 'start'
 * reached 'len' seconds. */
static int64_t overdue(uint32_t t, uint32_t start, uint32_t len) {
    return (int64_t)(uint32_t)(t - start) - (int64_t)len;
}

static bool is_due(int64_t od, bool inclusive) { return od > 0 || (inclusive && od == 0); }

/* Process every event due before t (or at t, if inclusive), oldest first.
 * On ties, the candidate considered first wins. */
static void advance(ctx_t *c, bool inclusive) {
    impl_t *s = &c->s;
    for (int iter = 0; iter < 8; iter++) {
        int kind = K_NONE;
        uint8_t target = ALARM_NORMAL;
        int64_t best = 0;

        /* Transition candidates in priority order: {condition, target}. */
        int cand[2][2];
        int nc = 0;
        switch (s->state) {
        case ALARM_NORMAL:
            cand[nc][0] = C_ABOVE_HIGH; cand[nc++][1] = ALARM_HIGH;
            cand[nc][0] = C_BELOW_LOW; cand[nc++][1] = ALARM_LOW;
            break;
        case ALARM_HIGH:
            cand[nc][0] = C_BELOW_LOW; cand[nc++][1] = ALARM_LOW;
            if (!cond_now(s, C_BELOW_LOW)) { cand[nc][0] = C_BELOW_HIGH_RTN; cand[nc++][1] = ALARM_NORMAL; }
            break;
        case ALARM_LOW:
            cand[nc][0] = C_ABOVE_HIGH; cand[nc++][1] = ALARM_HIGH;
            if (!cond_now(s, C_ABOVE_HIGH)) { cand[nc][0] = C_ABOVE_LOW_RTN; cand[nc++][1] = ALARM_NORMAL; }
            break;
        default: /* FAULT: left only via a good sample */
            break;
        }
        for (int i = 0; i < nc; i++) {
            int k = cand[i][0];
            if (!cond_now(s, k)) continue;
            int64_t od = overdue(c->t, s->since[k], s->cfg.time_delay_s);
            if (is_due(od, inclusive) && (kind == K_NONE || od > best)) {
                kind = K_TRANSITION;
                target = (uint8_t)cand[i][1];
                best = od;
            }
        }

        if (!(s->flags & (F_MAINT | F_ESCALATED)) && (s->flags & F_UNACKED) && s->cfg.escalate_after_s > 0 &&
            s->state != ALARM_NORMAL && s->reported == s->state) {
            int64_t od = overdue(c->t, s->esc_start, s->cfg.escalate_after_s);
            if (is_due(od, inclusive) && (kind == K_NONE || od > best)) kind = K_ESCALATE;
        }

        if (kind == K_NONE) return;
        if (kind == K_TRANSITION) {
            transition(c, target);
        } else {
            if (!emit(c, EV_ESCALATE)) return; /* retried on the next call */
            s->flags |= F_ESCALATED;
        }
    }
}

static bool cfg_valid(const alarm_cfg_t *g) {
    return !isnan(g->high_limit) && !isnan(g->low_limit) && g->low_limit <= g->high_limit &&
           isfinite(g->deadband) && g->deadband >= 0.0f;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (!a) return;
    impl_t s;
    memset(a, 0, sizeof *a);
    memset(&s, 0, sizeof s);
    s.magic = ALARM_MAGIC;
    if (cfg) s.cfg = *cfg;
    s.last_t = now;
    s.value = NAN;
    s.state = s.reported = ALARM_NORMAL;
    if (!cfg || !cfg_valid(cfg)) {
        s.flags = F_CFG_ERR | F_UNACKED;
        s.state = ALARM_FAULT; /* announced by the next call */
    }
    memcpy(a, &s, sizeof s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!load(&c, a, now, out)) return 0;
    impl_t *s = &c.s;

    advance(&c, false); /* the held value up to (not including) now */

    s->value = value;
    if (s->flags & F_CFG_ERR) {
        s->cond_valid = 0;
    } else if (sensor_fault || !isfinite(value)) {
        s->cond_valid = 0;
        if (s->state != ALARM_FAULT) transition(&c, ALARM_FAULT);
    } else {
        if (s->state == ALARM_FAULT) {
            s->cond_valid = 0;
            transition(&c, ALARM_NORMAL);
        }
        update_conditions(s, c.t, value);
    }

    advance(&c, true);
    flush(&c);
    return store(&c, a);
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!load(&c, a, now, out)) return 0;
    advance(&c, true);
    flush(&c);
    return store(&c, a);
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!load(&c, a, now, out)) return 0;
    advance(&c, true);
    flush(&c);
    if (c.s.state != ALARM_NORMAL) c.s.flags &= (uint8_t)~F_UNACKED;
    return store(&c, a);
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    ctx_t c;
    if (!load(&c, a, now, out)) return 0;
    impl_t *s = &c.s;
    advance(&c, true);
    flush(&c);
    bool in_maint = (s->flags & F_MAINT) != 0;
    bool pending_esc = s->state != ALARM_NORMAL && (s->flags & F_UNACKED) && !(s->flags & F_ESCALATED);
    if (on && !in_maint) {
        s->esc_banked = (pending_esc && s->reported == s->state) ? (uint32_t)(c.t - s->esc_start) : 0;
        s->flags |= F_MAINT;
        s->flags &= (uint8_t)~F_DIRTY;
    } else if (!on && in_maint) {
        bool new_occurrence = (s->flags & F_DIRTY) && s->state != ALARM_NORMAL;
        s->flags &= (uint8_t)~(F_MAINT | F_DIRTY);
        if (s->state != s->reported || new_occurrence) announce(&c);
        else if (pending_esc) s->esc_start = c.t - s->esc_banked;
    }
    return store(&c, a);
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t s;
    if (!a) return ALARM_FAULT;
    memcpy(&s, a, sizeof s);
    if (s.magic != ALARM_MAGIC) return ALARM_FAULT;
    return (alarm_state_t)s.state;
}
