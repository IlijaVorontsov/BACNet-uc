/* alarm.c - out-of-range alarming for analog input points */
#include "alarm.h"

#include <math.h>
#include <string.h>

#define NONE 0xFF

typedef struct {
    const alarm_cfg_t *cfg;
    uint32_t pending_start;
    uint32_t alarm_time;
    uint32_t last_now;
    float last_value;
    uint8_t state;
    uint8_t last_notified;
    uint8_t pending;
    uint8_t flags;
} point_t;

#define F_UNACKED 0x01u
#define F_ESCALATED 0x02u
#define F_MAINT 0x04u

_Static_assert(sizeof(point_t) <= sizeof(alarm_t), "point_t too large");

static bool get(const point_t *p, uint8_t f) { return (p->flags & f) != 0; }
static void set(point_t *p, uint8_t f, bool on) { p->flags = on ? (uint8_t)(p->flags | f) : (uint8_t)(p->flags & ~f); }

static point_t *P(alarm_t *a) { return (point_t *)(void *)a; }

static uint8_t target(const point_t *p, float v)
{
    const alarm_cfg_t *c = p->cfg;
    switch (p->state) {
    case ALARM_NORMAL:
        if (v > c->high_limit)
            return ALARM_HIGH;
        if (v < c->low_limit)
            return ALARM_LOW;
        return NONE;
    case ALARM_HIGH:
        if (v < c->low_limit)
            return ALARM_LOW;
        if (v < c->high_limit - c->deadband)
            return ALARM_NORMAL;
        return NONE;
    case ALARM_LOW:
        if (v > c->high_limit)
            return ALARM_HIGH;
        if (v > c->low_limit + c->deadband)
            return ALARM_NORMAL;
        return NONE;
    default:
        return NONE;
    }
}

static void emit(alarm_note_t *out, int *n, uint32_t now, alarm_event_t ev, float v)
{
    if (*n < ALARM_MAX_NOTES) {
        out[*n].time = now;
        out[*n].event = ev;
        out[*n].value = v;
        (*n)++;
    }
}

static const alarm_event_t to_event[4] = {EV_TO_NORMAL, EV_TO_HIGH, EV_TO_LOW, EV_TO_FAULT};

static void notify_state(point_t *p, uint32_t now, alarm_note_t *out, int *n)
{
    emit(out, n, now, to_event[p->state], p->last_value);
    p->last_notified = p->state;
    if (p->state == ALARM_HIGH || p->state == ALARM_LOW) {
        set(p, F_UNACKED, true);
        p->alarm_time = now;
        set(p, F_ESCALATED, false);
    }
}

static void transition(point_t *p, uint8_t to, uint32_t now, alarm_note_t *out, int *n)
{
    p->state = to;
    p->pending = NONE;
    if (!get(p, F_MAINT))
        notify_state(p, now, out, n);
}

static uint32_t delay_for(const point_t *p)
{
    if (p->pending == ALARM_NORMAL && p->cfg->time_delay_normal_s != 0xFFFFFFFFu)
        return p->cfg->time_delay_normal_s;
    return p->cfg->time_delay_s;
}

static void eval_timer(point_t *p, uint32_t now, alarm_note_t *out, int *n)
{
    if (p->state == ALARM_FAULT || p->pending == NONE)
        return;
    if (now - p->pending_start >= delay_for(p))
        transition(p, p->pending, now, out, n);
}

static void check_escalate(point_t *p, uint32_t now, alarm_note_t *out, int *n)
{
    if (get(p, F_UNACKED) && !get(p, F_ESCALATED) && !get(p, F_MAINT) && p->cfg->escalate_after_s > 0 &&
        now - p->alarm_time >= p->cfg->escalate_after_s) {
        emit(out, n, now, EV_ESCALATE, p->last_value);
        set(p, F_ESCALATED, true);
    }
}

static bool accept_time(point_t *p, uint32_t now)
{
    if (now < p->last_now)
        return false;
    p->last_now = now;
    return true;
}

void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now)
{
    point_t *p = P(a);
    memset(a, 0, sizeof *a);
    p->cfg = cfg;
    p->state = ALARM_NORMAL;
    p->last_notified = ALARM_NORMAL;
    p->pending = NONE;
    p->last_now = now;
}

int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    if (!accept_time(p, now))
        return 0;
    p->last_value = value;
    if (sensor_fault || isnan(value)) {
        if (p->state != ALARM_FAULT)
            transition(p, ALARM_FAULT, now, out, &n);
        p->pending = NONE;
    } else {
        if (p->state == ALARM_FAULT)
            transition(p, ALARM_NORMAL, now, out, &n);
        uint8_t t = target(p, value);
        if (t == NONE) {
            p->pending = NONE;
        } else if (t != p->pending) {
            p->pending = t;
            p->pending_start = now;
        }
        eval_timer(p, now, out, &n);
    }
    check_escalate(p, now, out, &n);
    return n;
}

int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    if (!accept_time(p, now))
        return 0;
    eval_timer(p, now, out, &n);
    check_escalate(p, now, out, &n);
    return n;
}

int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    if (!accept_time(p, now))
        return 0;
    eval_timer(p, now, out, &n);
    if (get(p, F_UNACKED)) {
        set(p, F_UNACKED, false);
        if (!get(p, F_MAINT))
            emit(out, &n, now, EV_ACKED, p->last_value);
    }
    check_escalate(p, now, out, &n);
    return n;
}

int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES])
{
    point_t *p = P(a);
    int n = 0;
    if (!accept_time(p, now))
        return 0;
    eval_timer(p, now, out, &n);
    if (on) {
        set(p, F_MAINT, true);
    } else if (get(p, F_MAINT)) {
        set(p, F_MAINT, false);
        if (p->state != p->last_notified)
            notify_state(p, now, out, &n);
    }
    check_escalate(p, now, out, &n);
    return n;
}

alarm_state_t alarm_state(const alarm_t *a)
{
    return (alarm_state_t)((const point_t *)(const void *)a)->state;
}
