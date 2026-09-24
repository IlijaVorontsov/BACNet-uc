/* driver.c - scenario CLI for the alarm module (test infrastructure; do not modify).
 *
 * Reads commands from stdin, prints exactly one line per command:
 *   cfg high=H low=L deadband=D delay=T escalate=E   -> "cfg"
 *       optional: delay_normal=N (default 4294967295)
 *   init T          alarm_init at time T
 *   s T V           alarm_sample(T, V, sensor_fault=false)    V may be nan/inf/-inf
 *   sf T V          alarm_sample(T, V, sensor_fault=true)
 *   tick T          alarm_tick(T)
 *   ack T           alarm_ack(T)
 *   maint T on|off  alarm_set_maintenance(T, on/off)
 * Output for init and API calls:  <STATE> [<EVENT>@<time>:<value> ...]
 * e.g. "HIGH TO_HIGH@40:31.000". Values are printed with 3 decimals.
 */
#include "alarm.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static alarm_cfg_t cfg;
static alarm_t point;

static const char *state_name(alarm_state_t s) {
    switch (s) {
    case ALARM_NORMAL: return "NORMAL";
    case ALARM_HIGH: return "HIGH";
    case ALARM_LOW: return "LOW";
    case ALARM_FAULT: return "FAULT";
    default: return "STATE?";
    }
}

static void print_event(int e) {
    static const char *names[] = {"TO_NORMAL", "TO_HIGH", "TO_LOW", "TO_FAULT", "ESCALATE"
        , "ACKED"
    };
    if (e >= 0 && e < (int)(sizeof names / sizeof names[0])) printf("%s", names[e]);
    else printf("EV%d", e);
}

static void print_value(float v) {
    if (isnan(v)) printf("nan");
    else if (isinf(v)) printf(v > 0 ? "inf" : "-inf");
    else {
        char b[64];
        snprintf(b, sizeof b, "%.3f", (double)v);
        if (!strcmp(b, "-0.000")) strcpy(b, "0.000");
        printf("%s", b);
    }
}

static int parse_u32(const char *s, uint32_t *out) {
    char *end;
    if (!s || *s == '-') return 0;
    unsigned long long v = strtoull(s, &end, 10);
    if (*end || v > 0xFFFFFFFFull) return 0;
    *out = (uint32_t)v;
    return 1;
}

static int parse_f(const char *s, float *out) {
    char *end;
    if (!s) return 0;
    if (!strcmp(s, "nan")) { *out = NAN; return 1; }
    *out = strtof(s, &end);
    return *end == 0;
}

#define GUARDN 4
static void report(int n, alarm_note_t *notes) {
    printf("%s", state_name(alarm_state(&point)));
    if (n < 0 || n > ALARM_MAX_NOTES) { printf(" BADCOUNT %d", n); return; }
    for (int i = 0; i < n; i++) {
        printf(" ");
        print_event((int)notes[i].event);
        printf("@%u:", (unsigned)notes[i].time);
        print_value(notes[i].value);
    }
    for (int i = ALARM_MAX_NOTES; i < ALARM_MAX_NOTES + GUARDN; i++)
        if (notes[i].time != 0xDEADBEEFu) { printf(" OVERRUN"); break; }
}

static void command(char *line) {
    char *argv[12];
    int argc = 0;
    for (char *t = strtok(line, " \t"); t && argc < 12; t = strtok(NULL, " \t")) argv[argc++] = t;
    if (argc == 0) { printf("BADCMD"); return; }
    alarm_note_t notes[ALARM_MAX_NOTES + GUARDN];
    for (int i = 0; i < ALARM_MAX_NOTES + GUARDN; i++) { notes[i].time = 0xDEADBEEFu; notes[i].event = (alarm_event_t)99; notes[i].value = 0; }
    uint32_t t;
    float v;
    if (!strcmp(argv[0], "cfg")) {
        memset(&cfg, 0, sizeof cfg);
        cfg.time_delay_normal_s = 0xFFFFFFFFu;
        for (int i = 1; i < argc; i++) {
            char *eq = strchr(argv[i], '=');
            if (!eq) { printf("BADCMD"); return; }
            *eq = 0;
            const char *k = argv[i], *val = eq + 1;
            int ok = 1;
            if (!strcmp(k, "high")) ok = parse_f(val, &cfg.high_limit);
            else if (!strcmp(k, "low")) ok = parse_f(val, &cfg.low_limit);
            else if (!strcmp(k, "deadband")) ok = parse_f(val, &cfg.deadband);
            else if (!strcmp(k, "delay")) ok = parse_u32(val, &cfg.time_delay_s);
            else if (!strcmp(k, "escalate")) ok = parse_u32(val, &cfg.escalate_after_s);
            else if (!strcmp(k, "delay_normal")) ok = parse_u32(val, &cfg.time_delay_normal_s);
            else ok = 0;
            if (!ok) { printf("BADCMD"); return; }
        }
        printf("cfg");
        return;
    }
    if (!strcmp(argv[0], "init") && argc == 2 && parse_u32(argv[1], &t)) {
        memset(&point, 0xCC, sizeof point);
        alarm_init(&point, &cfg, t);
        printf("%s", state_name(alarm_state(&point)));
        return;
    }
    if ((!strcmp(argv[0], "s") || !strcmp(argv[0], "sf")) && argc == 3 && parse_u32(argv[1], &t) && parse_f(argv[2], &v)) {
        report(alarm_sample(&point, t, v, argv[0][1] == 'f', notes), notes);
        return;
    }
    if (!strcmp(argv[0], "tick") && argc == 2 && parse_u32(argv[1], &t)) { report(alarm_tick(&point, t, notes), notes); return; }
    if (!strcmp(argv[0], "ack") && argc == 2 && parse_u32(argv[1], &t)) { report(alarm_ack(&point, t, notes), notes); return; }
    if (!strcmp(argv[0], "maint") && argc == 3 && parse_u32(argv[1], &t) && (!strcmp(argv[2], "on") || !strcmp(argv[2], "off"))) {
        report(alarm_set_maintenance(&point, t, !strcmp(argv[2], "on"), notes), notes);
        return;
    }
    printf("BADCMD");
}

int main(void) {
    static char line[4096];
    while (fgets(line, sizeof line, stdin)) {
        size_t l = strlen(line);
        while (l && (line[l - 1] == '\n' || line[l - 1] == '\r')) line[--l] = 0;
        if (l == 0) continue;
        if (!strncmp(line, "@block", 6)) { printf("%s\n", line); fflush(stdout); continue; }
        command(line);
        printf("\n");
        fflush(stdout);
    }
    return 0;
}
