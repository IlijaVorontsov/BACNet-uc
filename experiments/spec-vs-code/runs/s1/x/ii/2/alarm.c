/* alarm.c - out-of-range alarming for analog input points.
 *
 * Bare-metal C11, no heap: all per-point state lives inside the caller's alarm_t.
 *
 * Behaviour (decisions the API/acceptance test leave open are marked [D]):
 *
 * Limits, deadband, time delay (BACnet OUT_OF_RANGE style)
 *   - NORMAL -> HIGH when value >  high_limit  for time_delay_s.
 *   - NORMAL -> LOW  when value <  low_limit   for time_delay_s.
 *   - HIGH -> NORMAL when value <  high_limit - deadband for time_delay_s.
 *   - LOW  -> NORMAL when value >  low_limit  + deadband for time_delay_s.
 *   - HIGH <-> LOW directly when the value crosses the opposite limit for time_delay_s.
 *   - Comparisons are strict: a value equal to a limit (or to limit -/+ deadband)
 *     does not change the state.
 *   - The condition must hold continuously: any sample that does not satisfy it
 *     restarts the delay. time_delay_s == 0 means "immediately".
 *   - [D] Samples are treated as sample-and-hold: the last value is assumed to
 *     persist until the next sample. alarm_tick() therefore completes a pending
 *     transition when the delay expires, and a sample whose timestamp is past an
 *     expired delay first completes the transition with the previous value
 *     (the value that actually violated the limit), then applies the new value.
 *     A sample exactly at the deadline is evaluated with its own value first.
 *     Net effect: inserting extra alarm_tick() calls never changes the outcome.
 *
 * Sensor faults
 *   - sensor_fault == true, or a non-finite value (NaN, +/-inf) [D], moves the
 *     point to FAULT immediately (no time delay), from any state.
 *   - [D] When the fault clears the point returns to NORMAL immediately
 *     (TO_NORMAL, as BACnet does when Reliability returns to NO_FAULT_DETECTED);
 *     if the value is out of range, the normal time-delay rule then applies
 *     starting at the clearing sample.
 *
 * Acknowledgement and escalation
 *   - HIGH, LOW and FAULT are alarms. Each transition into one of them is a new
 *     alarm occurrence that starts unacknowledged.
 *   - alarm_ack() acknowledges the current occurrence; it is a no-op in NORMAL.
 *     It emits nothing itself.
 *   - If an occurrence is still unacknowledged escalate_after_s seconds after it
 *     was notified, one EV_ESCALATE is emitted (at most once per occurrence).
 *     Acknowledging at exactly the deadline prevents it.
 *   - [D] Returning to NORMAL cancels a pending escalation; a HIGH<->LOW or
 *     alarm->FAULT transition starts a new occurrence with a fresh clock.
 *   - [D] escalate_after_s == 0 disables escalation.
 *
 * Maintenance mode
 *   - While on, alarm evaluation continues (alarm_state() stays truthful) but no
 *     notifications are emitted and escalation is suspended.
 *   - [D] On leaving maintenance the front-end is resynchronised so no alarm is
 *     lost: if the state changed while in maintenance and now differs from what
 *     was last reported (or is an alarm occurrence the front-end has not seen),
 *     one TO_<state> notification is emitted. An alarm first reported this way
 *     starts its escalation clock at that moment.
 *   - [D] For an alarm that was already reported before maintenance, time spent
 *     in maintenance does not count toward escalation (the clock pauses).
 *   - Acknowledgement is accepted in maintenance mode.
 *
 * General
 *   - Note.time is the 'now' of the call that produced it; note.value is the
 *     value that caused the transition (for escalation: the last sampled value).
 *   - Every call first runs timers that expired strictly before 'now', then
 *     applies its own input, then runs timers expiring at 'now'. Timers due in
 *     the same call are processed in chronological order.
 *   - Time differences use unsigned arithmetic, so uint32 wrap-around is safe.
 *     [D] A 'now' earlier than a stored timestamp counts as zero elapsed time.
 *   - [D] A negative or NaN deadband is treated as 0.
 */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define ALARM_MAGIC 0x414C524Du /* "ALRM" */
#define HALF_RANGE 0x7FFFFFFFu

typedef struct {
    uint32_t magic;
    alarm_cfg_t cfg;
    float value;         /* last sampled value */
    uint32_t pend_since; /* when the pending condition was first observed */
    uint32_t esc_start;  /* start of the unacknowledged clock */
    uint32_t esc_saved;  /* unacked time accumulated before entering maintenance */
    uint8_t state;       /* alarm_state_t */
    uint8_t pend_target; /* alarm_state_t the pending timer leads to */
    uint8_t reported;    /* state last reported to the front-end */
    bool fault;          /* last sample was faulty */
    bool pend;           /* a time-delayed transition is pending */
    bool acked;
    bool escalated;
    bool maint;
    bool occ_reported; /* current occurrence has been notified */
} impl_t;

_Static_assert(sizeof(impl_t) <= sizeof(alarm_t), "alarm_t storage too small");
_Static_assert(_Alignof(impl_t) <= _Alignof(alarm_t), "alarm_t alignment too small");

typedef struct {
    alarm_note_t *out;
    int n;
} sink_t;

/* Access the opaque storage via memcpy (no aliasing issues). */
static bool load(const alarm_t *a, impl_t *p) {
    if (!a) return false;
    memcpy(p, a, sizeof *p);
    return p->magic == ALARM_MAGIC;
}

static void store(alarm_t *a, const impl_t *p) { memcpy(a, p, sizeof *p); }

static uint32_t elapsed(uint32_t now, uint32_t since) {
    uint32_t d = now - since;
    return d > HALF_RANGE ? 0u : d;
}

/* strict: expired strictly before 'now'; otherwise at or before 'now'. */
static bool expired(uint32_t now, uint32_t since, uint32_t dur, bool strict, uint32_t *overdue) {
    uint32_t e = elapsed(now, since);
    bool r = strict ? e > dur : e >= dur;
    if (r && overdue) *overdue = e - dur;
    return r;
}

static void emit(const impl_t *p, sink_t *s, uint32_t now, alarm_event_t ev) {
    if (p->maint) return;
    if (!s->out || s->n >= ALARM_MAX_NOTES) return;
    s->out[s->n].time = now;
    s->out[s->n].event = ev;
    s->out[s->n].value = p->value;
    s->n++;
}

static alarm_event_t event_for(alarm_state_t st) {
    switch (st) {
    case ALARM_HIGH: return EV_TO_HIGH;
    case ALARM_LOW: return EV_TO_LOW;
    case ALARM_FAULT: return EV_TO_FAULT;
    default: return EV_TO_NORMAL;
    }
}

static bool esc_armed(const impl_t *p) {
    return p->state != ALARM_NORMAL && !p->acked && !p->escalated && !p->maint && p->cfg.escalate_after_s > 0;
}

static void enter(impl_t *p, sink_t *s, uint32_t now, alarm_state_t st) {
    p->state = (uint8_t)st;
    p->pend = false;
    if (st != ALARM_NORMAL) {
        p->acked = false;
        p->escalated = false;
        p->esc_start = now;
        p->esc_saved = 0;
    }
    emit(p, s, now, event_for(st));
    if (p->maint) {
        p->occ_reported = false;
    } else {
        p->reported = (uint8_t)st;
        p->occ_reported = true;
    }
}

/* The state the current input calls for, before applying the time delay. */
static alarm_state_t target(const impl_t *p) {
    const float v = p->value;
    const alarm_state_t st = (alarm_state_t)p->state;
    if (p->fault) return ALARM_FAULT;
    if (st == ALARM_FAULT) return ALARM_NORMAL; /* fault cleared: back via NORMAL */
    if (v > p->cfg.high_limit) return ALARM_HIGH;
    if (v < p->cfg.low_limit) return ALARM_LOW;
    if (st == ALARM_HIGH && !(v < p->cfg.high_limit - p->cfg.deadband)) return ALARM_HIGH;
    if (st == ALARM_LOW && !(v > p->cfg.low_limit + p->cfg.deadband)) return ALARM_LOW;
    return ALARM_NORMAL;
}

/* Re-evaluate after an input change: fault transitions are immediate, others
 * (re)arm the time-delay timer. */
static void evaluate(impl_t *p, sink_t *s, uint32_t now) {
    for (int i = 0; i < 2; i++) {
        alarm_state_t t = target(p);
        if (t == (alarm_state_t)p->state) {
            p->pend = false;
            return;
        }
        if (t == ALARM_FAULT || p->state == ALARM_FAULT) {
            enter(p, s, now, t);
            continue;
        }
        if (!p->pend || p->pend_target != (uint8_t)t) {
            p->pend = true;
            p->pend_target = (uint8_t)t;
            p->pend_since = now;
        }
        return;
    }
}

/* Fire expired timers (time-delay transition, escalation) in chronological order. */
static void run_timers(impl_t *p, sink_t *s, uint32_t now, bool strict) {
    for (int i = 0; i < 4; i++) {
        uint32_t od_t = 0, od_e = 0;
        bool td = p->pend && expired(now, p->pend_since, p->cfg.time_delay_s, strict, &od_t);
        bool ed = esc_armed(p) && expired(now, p->esc_start, p->cfg.escalate_after_s, strict, &od_e);
        if (!td && !ed) return;
        if (ed && (!td || od_e >= od_t)) {
            emit(p, s, now, EV_ESCALATE);
            p->escalated = true;
        } else {
            enter(p, s, now, (alarm_state_t)p->pend_target);
            evaluate(p, s, now);
        }
    }
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now) {
    (void)now;
    if (!a) return;
    impl_t p;
    memset(&p, 0, sizeof p);
    p.magic = ALARM_MAGIC;
    if (cfg) p.cfg = *cfg;
    if (!(p.cfg.deadband >= 0.0f)) p.cfg.deadband = 0.0f; /* negative or NaN */
    p.state = ALARM_NORMAL;
    p.reported = ALARM_NORMAL;
    p.occ_reported = true;
    memset(a, 0, sizeof *a);
    store(a, &p);
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s = {out, 0};
    if (!load(a, &p)) return 0;
    run_timers(&p, &s, now, true);
    p.value = value;
    p.fault = sensor_fault || !isfinite(value);
    evaluate(&p, &s, now);
    run_timers(&p, &s, now, false);
    store(a, &p);
    return s.n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s = {out, 0};
    if (!load(a, &p)) return 0;
    run_timers(&p, &s, now, false);
    store(a, &p);
    return s.n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s = {out, 0};
    if (!load(a, &p)) return 0;
    run_timers(&p, &s, now, true);
    if (p.state != ALARM_NORMAL) p.acked = true;
    run_timers(&p, &s, now, false);
    store(a, &p);
    return s.n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]) {
    impl_t p;
    sink_t s = {out, 0};
    if (!load(a, &p)) return 0;
    run_timers(&p, &s, now, true);
    if (on && !p.maint) {
        p.esc_saved = esc_armed(&p) ? elapsed(now, p.esc_start) : 0u;
        p.maint = true;
    } else if (!on && p.maint) {
        p.maint = false;
        if (!p.occ_reported) {
            /* Resynchronise the front-end with what happened during maintenance. */
            if (p.state != p.reported || p.state != ALARM_NORMAL) emit(&p, &s, now, event_for((alarm_state_t)p.state));
            p.reported = p.state;
            p.occ_reported = true;
            p.esc_start = now;
        } else {
            p.esc_start = now - p.esc_saved; /* clock paused during maintenance */
        }
    }
    run_timers(&p, &s, now, false);
    store(a, &p);
    return s.n;
}

alarm_state_t alarm_state(const alarm_t *a) {
    impl_t p;
    if (!load(a, &p)) return ALARM_FAULT; /* uninitialised: report as fault */
    return (alarm_state_t)p.state;
}
