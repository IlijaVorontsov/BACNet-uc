/* alarm.c - out-of-range alarming for analog input points.
 *
 * Behaviour follows the BACnet OUT_OF_RANGE event algorithm (ASHRAE 135,
 * clause 13.3) where alarm.h leaves a choice open:
 *
 * Limits and deadband
 *   - HIGH when value > high_limit, LOW when value < low_limit (strict).
 *   - Deadband applies to the return only: HIGH -> NORMAL needs
 *     value < high_limit - deadband, LOW -> NORMAL needs value > low_limit + deadband.
 *   - HIGH <-> LOW is a direct transition (no NORMAL in between) when the
 *     opposite limit is exceeded. If both are due at the same instant the
 *     direct transition wins.
 *   - A negative or NaN deadband is treated as 0.
 *
 * Time delay
 *   - Every transition between NORMAL/HIGH/LOW needs its condition to hold
 *     continuously for time_delay_s (elapsed >= delay; 0 = immediate).
 *     Each condition is timed from the first sample on which it held; a
 *     sample on which it does not hold restarts it.
 *   - The last sample is assumed to hold until the next one, so alarm_tick()
 *     (or any other call) completes a transition whose delay has run out.
 *     A transition that falls due exactly at the time of a call is decided
 *     after that call's input (a sample at that instant can still cancel it).
 *
 * Sensor faults
 *   - sensor_fault == true, or a non-finite value (NaN, +/-inf), moves the
 *     point to FAULT immediately (no time delay) and cancels running delays.
 *   - The first good sample after a fault goes to NORMAL immediately, and
 *     the limits are then evaluated from NORMAL with the usual time delay.
 *
 * Acknowledgement and escalation
 *   - Every notified HIGH, LOW or FAULT is a new alarm that needs an ack.
 *     alarm_ack() acknowledges it (no-op otherwise); it emits no notification.
 *   - ESCALATE is sent once per alarm, escalate_after_s after the alarm was
 *     notified, if it is still unacknowledged and still active. A return to
 *     NORMAL or a new alarm cancels it. escalate_after_s == 0 disables it.
 *     At the same instant, state transitions are decided before escalation.
 *   - The ESCALATE value is the latest sample value.
 *
 * Maintenance mode
 *   - While on, the state machine keeps running (alarm_state() is the true
 *     state) but no notifications are sent, including escalation.
 *   - Switching it off re-synchronises the front-end: if the state differs
 *     from the last notified one, a notification for the current state is
 *     sent (a new alarm, escalation timed from then). If it is unchanged, the
 *     original alarm and its ack/escalation status carry on, so an escalation
 *     that fell due during maintenance is sent when maintenance ends.
 *   - Operators may acknowledge during maintenance.
 *
 * Time
 *   - All time arithmetic is wrap-safe modulo 2^32. A 'now' that is earlier
 *     than a previous call's is treated as that previous time (no spurious
 *     alarms from a clock step). Notification times are the 'now' passed in.
 *
 * No heap; the state lives in alarm_t and is accessed via memcpy (no type
 * punning of alarm_t::storage).
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

/* Conditions on the latest good sample (bit i <-> since[i]). */
enum {
    C_HI = 1u << 0,     /* value > high                 */
    C_LO = 1u << 1,     /* value < low                  */
    C_RET_HI = 1u << 2, /* value < high - deadband      */
    C_RET_LO = 1u << 3, /* value > low + deadband       */
    C_COUNT = 4
};

enum { F_MAINT = 1u << 0, F_ACKED = 1u << 1, F_ESCALATED = 1u << 2 };

typedef struct {
    alarm_cfg_t cfg;
    float value;           /* latest sample value, reported in notifications */
    uint32_t last_now;     /* latest time seen (for clock-step protection) */
    uint32_t alarm_time;   /* when the notified alarm was sent */
    uint32_t since[C_COUNT];
    uint8_t cond;          /* C_* holding for the latest sample */
    uint8_t state;         /* true state (alarm_state_t) */
    uint8_t notified;      /* state the front-end was last told about */
    uint8_t flags;         /* F_* */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");

typedef struct {
    alarm_note_t *out;
    int n;
} sink_t;

typedef struct {
    uint8_t cond;   /* condition that must hold ... */
    uint8_t block;  /* ... while this one does not */
    uint8_t target;
} rule_t;

/* Per-state transition rules, highest priority first. */
static const rule_t RULES[3][2] = {
    [ALARM_NORMAL] = {{C_HI, 0, ALARM_HIGH}, {C_LO, 0, ALARM_LOW}},
    [ALARM_HIGH] = {{C_LO, C_HI, ALARM_LOW}, {C_RET_HI, 0, ALARM_NORMAL}},
    [ALARM_LOW] = {{C_HI, C_LO, ALARM_HIGH}, {C_RET_LO, 0, ALARM_NORMAL}},
};

static void load(impl_t *s, const alarm_t *a) { memcpy(s, a->storage, sizeof *s); }
static void store(alarm_t *a, const impl_t *s) { memcpy(a->storage, s, sizeof *s); }

static void emit(sink_t *k, uint32_t now, alarm_event_t ev, float value) {
    if (k->out == NULL || k->n >= ALARM_MAX_NOTES) return;
    k->out[k->n].time = now;
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

/* Tell the front-end about the current state; an alarm state starts a new,
 * unacknowledged alarm. */
static void announce(impl_t *s, sink_t *k, uint32_t now) {
    s->notified = s->state;
    s->alarm_time = now;
    s->flags &= (uint8_t)~(F_ACKED | F_ESCALATED);
    emit(k, now, event_for(s->state), s->value);
}

static void enter(impl_t *s, sink_t *k, uint32_t now, uint8_t state) {
    s->state = state;
    if (!(s->flags & F_MAINT)) announce(s, k, now);
}

static int escalation_armed(const impl_t *s) {
    return s->cfg.escalate_after_s > 0 && s->notified != ALARM_NORMAL &&
           !(s->flags & (F_MAINT | F_ACKED | F_ESCALATED));
}

static int bit_index(uint8_t bit) {
    int i = 0;
    while (!(bit & 1u)) { bit >>= 1; i++; }
    return i;
}

/* Clamp a clock that stepped backwards to the latest time seen. */
static uint32_t advance(impl_t *s, uint32_t now) {
    if ((uint32_t)(now - s->last_now) >= 0x80000000u) return s->last_now;
    s->last_now = now;
    return now;
}

/* Process time-driven events (delayed transitions, escalation) in order of
 * their due time. 'inclusive' also processes events due exactly at t; the
 * pre-pass before applying a call's input leaves those for the post-pass. */
static void run_timers(impl_t *s, sink_t *k, uint32_t t, uint32_t now, int inclusive) {
    const uint32_t delay = s->cfg.time_delay_s;
    for (int guard = 0; guard < 8; guard++) {
        int found = 0, esc = 0;
        uint32_t best_over = 0;
        uint8_t target = 0;

        if (s->state <= ALARM_LOW) {
            for (int i = 0; i < 2; i++) {
                const rule_t *r = &RULES[s->state][i];
                if (!(s->cond & r->cond) || (s->cond & r->block)) continue;
                uint32_t el = t - s->since[bit_index(r->cond)];
                if (el < delay || (!inclusive && el == delay)) continue;
                uint32_t over = el - delay;
                if (!found || over > best_over) { found = 1; best_over = over; target = r->target; }
            }
        }
        if (escalation_armed(s)) {
            uint32_t e = s->cfg.escalate_after_s, el = t - s->alarm_time;
            if (el > e || (inclusive && el == e)) {
                uint32_t over = el - e;
                if (!found || over > best_over) { found = 1; esc = 1; }
            }
        }
        if (!found) return;
        if (esc) {
            s->flags |= F_ESCALATED;
            emit(k, now, EV_ESCALATE, s->value);
        } else {
            enter(s, k, now, target);
        }
    }
}

static uint8_t eval_conditions(const alarm_cfg_t *c, float v) {
    uint8_t m = 0;
    if (v > c->high_limit) m |= C_HI;
    if (v < c->low_limit) m |= C_LO;
    if (v < c->high_limit - c->deadband) m |= C_RET_HI;
    if (v > c->low_limit + c->deadband) m |= C_RET_LO;
    return m;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    if (a == NULL) return;
    impl_t s;
    memset(&s, 0, sizeof s);
    if (cfg != NULL) {
        s.cfg = *cfg;
    } else {
        s.cfg.high_limit = NAN; /* no limits: never alarms on value */
        s.cfg.low_limit = NAN;
    }
    if (!(s.cfg.deadband >= 0.0f)) s.cfg.deadband = 0.0f;
    s.value = NAN;
    s.last_now = now;
    s.state = ALARM_NORMAL;
    s.notified = ALARM_NORMAL;
    memset(a, 0, sizeof *a);
    store(a, &s);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    uint32_t t = advance(&s, now);
    run_timers(&s, &k, t, now, 0);

    s.value = value;
    if (sensor_fault || !isfinite(value)) {
        s.cond = 0;
        if (s.state != ALARM_FAULT) enter(&s, &k, now, ALARM_FAULT);
    } else {
        uint8_t m = eval_conditions(&s.cfg, value);
        for (int i = 0; i < C_COUNT; i++) {
            uint8_t bit = (uint8_t)(1u << i);
            if ((m & bit) && !(s.cond & bit)) s.since[i] = t;
        }
        s.cond = m;
        if (s.state == ALARM_FAULT) enter(&s, &k, now, ALARM_NORMAL);
    }

    run_timers(&s, &k, t, now, 1);
    store(a, &s);
    return k.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    uint32_t t = advance(&s, now);
    run_timers(&s, &k, t, now, 1);
    store(a, &s);
    return k.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    uint32_t t = advance(&s, now);
    run_timers(&s, &k, t, now, 0);
    if (s.notified != ALARM_NORMAL) s.flags |= F_ACKED;
    run_timers(&s, &k, t, now, 1);
    store(a, &s);
    return k.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    if (a == NULL) return 0;
    impl_t s;
    sink_t k = {out, 0};
    load(&s, a);
    uint32_t t = advance(&s, now);
    run_timers(&s, &k, t, now, 0);
    if (on) {
        s.flags |= F_MAINT;
    } else if (s.flags & F_MAINT) {
        s.flags &= (uint8_t)~F_MAINT;
        if (s.state != s.notified) announce(&s, &k, now);
    }
    run_timers(&s, &k, t, now, 1);
    store(a, &s);
    return k.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    if (a == NULL) return ALARM_FAULT;
    impl_t s;
    load(&s, a);
    return (alarm_state_t)s.state;
}
