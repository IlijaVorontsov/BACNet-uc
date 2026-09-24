/* alarm.h - out-of-range alarming for analog input points */
#ifndef ALARM_H
#define ALARM_H

#include <stdbool.h>
#include <stdint.h>

typedef enum { ALARM_NORMAL = 0, ALARM_HIGH = 1, ALARM_LOW = 2, ALARM_FAULT = 3 } alarm_state_t;

typedef enum {
    EV_TO_NORMAL = 0,
    EV_TO_HIGH = 1,
    EV_TO_LOW = 2,
    EV_TO_FAULT = 3,
    EV_ESCALATE = 4
} alarm_event_t;

typedef struct {
    float high_limit;
    float low_limit;
    float deadband;            /* >= 0 */
    uint32_t time_delay_s;
    uint32_t escalate_after_s;
} alarm_cfg_t;

/* A notification for the BMS front-end. */
typedef struct {
    uint32_t time;       /* 'now' of the call that produced it */
    alarm_event_t event;
    float value;
} alarm_note_t;

/* Per-point state. Opaque: the implementation may use up to sizeof(alarm_t) bytes.
 * No heap is available. alarm_init copies the configuration. */
typedef struct {
    uint64_t storage[16];
} alarm_t;

#define ALARM_MAX_NOTES 4

/* 'now' is a monotonic time in seconds. Every function except alarm_init and
 * alarm_state writes up to ALARM_MAX_NOTES notifications to out[] and returns
 * how many it wrote. */
void alarm_init(alarm_t *a, const alarm_cfg_t *cfg, uint32_t now);
int alarm_sample(alarm_t *a, uint32_t now, float value, bool sensor_fault, alarm_note_t out[ALARM_MAX_NOTES]);
int alarm_tick(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]);
int alarm_ack(alarm_t *a, uint32_t now, alarm_note_t out[ALARM_MAX_NOTES]);
int alarm_set_maintenance(alarm_t *a, uint32_t now, bool on, alarm_note_t out[ALARM_MAX_NOTES]);
alarm_state_t alarm_state(const alarm_t *a);

#endif
