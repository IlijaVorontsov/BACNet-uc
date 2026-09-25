/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Internal interfaces of the WebAssembly application manager:
 *
 *   uc_app_mgr.c       WAMR runtime (static pool), application slots and
 *                      threads, event loop, watchdog, write hook, manager
 *                      API of uc_apps.h
 *   uc_app_host_api.c  host functions of import module "bacnet_uc"
 *                      (wasm/sdk/include/bacnet_uc.h), COV callback, kv
 *                      store
 *
 * Threads touching a slot:
 *   manager callers    uc_apps_*() under mgr_lock (shell, SMP, main); a
 *                      stop sets cancel and stop_req
 *   app thread         owns the WAMR instance; the only thread that calls
 *                      into it (host functions and the libc-builtin output
 *                      of the module run in this thread)
 *   BACnet thread      write hook / COV callback: uc_app_post_event() only
 *   system workqueue   watchdog: wasm_runtime_terminate() under wd_lock,
 *                      sets cancel
 */
#ifndef UC_APP_INTERNAL_H_
#define UC_APP_INTERNAL_H_

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>

#include <wasm_export.h>

#include "uc/uc_apps.h"
#include "uc/uc_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Shortest non-zero tick period (ms), see uc_set_tick_period(). */
#define UC_APP_PERIOD_MIN_MS 10u
/* Longest tick period (ms), same limit as apps.json "period_ms". */
#define UC_APP_PERIOD_MAX_MS 3600000u
/* Longest log line taken from an app (uc_log, printf); longer lines are
 * truncated. */
#define UC_APP_LOG_LINE_MAX 120

enum uc_app_ev_kind {
	UC_APP_EV_STOP = 0, /* wake-up only; the stop flag carries the request */
	UC_APP_EV_COV,
	UC_APP_EV_WRITE,
};

/* Element of a slot's event queue (k_msgq, size is a multiple of 8). */
struct uc_app_event {
	uint8_t kind;
	uint8_t priority;
	uint16_t type;
	int32_t sub_id;
	uint32_t device;
	uint32_t instance;
	uint32_t prop;
	uint32_t reserved;
	double value;
};

/* A COV subscription made by the running instance (app thread only). */
struct uc_app_sub {
	int32_t id; /* -1: free */
	uint32_t device;
	uint32_t instance;
	uint16_t type;
};

struct uc_app_slot {
	/* constant after uc_apps_init() */
	uint8_t index;
	uint8_t owner; /* UC_OWNER_APP_BASE + index */

	/* mgr_lock: installed apps.json entry bound to this slot. cfg only
	 * changes while no instance runs (busy == 0), so the app thread and
	 * the host functions read it without a lock. */
	bool used;
	struct uc_app_cfg cfg;

	/* state_lock (spinlock in uc_app_mgr.c) */
	enum uc_app_state state;
	char last_error[80];
	int fail_rc;      /* errno of the last failed start, 0 otherwise */
	int64_t start_ms; /* uptime when the state became RUNNING */

	atomic_t ticks;
	atomic_t events;
	atomic_t errors;
	atomic_t dropped;  /* events lost because the queue was full */
	atomic_t busy;     /* 1 from start until the app thread has cleaned up */
	atomic_t stop_req; /* stop requested by the manager */
	atomic_t accept;   /* the write hook / COV callback may post events */
	/* blocking host calls return at once (stop request, watchdog expiry;
	 * cleared before uc_app_deinit()) */
	atomic_t cancel;

	struct k_thread thread;
	struct k_sem run_sem;     /* manager -> thread: run the configured app */
	struct k_sem done_sem;    /* thread -> manager: busy cleared */
	struct k_sem started_sem; /* thread -> manager: left STARTING */
	struct k_msgq msgq;
	struct uc_app_event msgq_buf[CONFIG_UC_APP_EVENT_QUEUE_LEN];

	/* watchdog */
	struct k_timer wd_timer;
	struct k_work wd_work;
	struct k_mutex wd_lock;     /* wd_in_cb, wd_fired, inst */
	bool wd_in_cb;              /* a callback into the instance runs */
	bool wd_fired;              /* the watchdog terminated that callback */
	atomic_t wd_gen;            /* incremented per callback */
	atomic_t wd_expired_gen;    /* wd_gen at the last timer expiry */
	uint32_t wd_remaining_ms;   /* budget left while paused */
	bool wd_paused;             /* app thread: paused by a blocking call */
	uint16_t cancel_refused;    /* app thread: cancelled calls this callback */
	wasm_module_inst_t inst;    /* running instance, NULL otherwise */

	/* app thread only */
	uint32_t period_ms;
	bool period_changed;
	struct uc_app_sub subs[CONFIG_UC_BACNET_COV_SUBS_MAX];
	int64_t log_window_ms;
	uint32_t log_count;
	uint32_t log_dropped;
	/* libc-builtin printf/puts/putchar output of the current line */
	char print_buf[UC_APP_LOG_LINE_MAX];
	uint16_t print_len; /* characters beyond print_buf are dropped */
};

/* ---------------------------------------------------------------------- */
/* uc_app_mgr.c                                                            */
/* ---------------------------------------------------------------------- */

/** Queue an event for the slot's instance without blocking (any thread,
 *  also the BACnet thread). Dropped (and counted as an error) when the
 *  instance does not accept events or the queue is full. */
bool uc_app_post_event(struct uc_app_slot *s, const struct uc_app_event *ev);

/** Bracket a host call that blocks on the network or the file system
 *  (app thread). uc_app_block_begin() returns false when blocking calls
 *  are cancelled (stop requested, watchdog expired): the host function
 *  must then fail at once without blocking. Otherwise it pauses the
 *  watchdog of the running callback (the budget left is kept, so only
 *  execution time counts), except while a stop is pending: then the whole
 *  time counts, so that a stopping callback and uc_app_deinit() end
 *  within the watchdog period. Remote requests also pass &s->cancel to
 *  the BACnet client, which gives up when it is set during the wait. A
 *  callback that keeps making blocking calls after they were cancelled
 *  is terminated. */
bool uc_app_block_begin(struct uc_app_slot *s);
void uc_app_block_end(struct uc_app_slot *s);

/** Slot of the calling application thread, NULL in any other thread. */
struct uc_app_slot *uc_app_current_slot(void);

/* uc_apps_owner_name() is public (uc_apps.h, included above). */

/* ---------------------------------------------------------------------- */
/* uc_app_host_api.c                                                       */
/* ---------------------------------------------------------------------- */

/** Native symbols of import module "bacnet_uc" (static table). */
NativeSymbol *uc_app_host_natives(uint32_t *count);

/** Reset the per-instance host state (subscriptions, log limiter) before
 *  an instance starts (app thread). */
void uc_app_host_reset(struct uc_app_slot *s);

/** Cancel every COV subscription of the slot (app thread, at cleanup). */
void uc_app_host_cleanup(struct uc_app_slot *s);

/** True when a COV event belongs to a subscription the running instance
 *  still holds (drops events of cancelled subscriptions). */
bool uc_app_host_sub_live(const struct uc_app_slot *s, const struct uc_app_event *ev);

/** Delete /lfs/data/<app> with all keys. Missing is not an error. */
int uc_app_kv_remove_all(const char *app_name);

/** One log line of the app (tag "<app>: ", control characters replaced,
 *  truncated to UC_APP_LOG_LINE_MAX, at most 20 lines per second and app;
 *  app thread). Used by uc_log and the libc-builtin output. */
void uc_app_log_line(struct uc_app_slot *s, int32_t level, const char *src, uint32_t len);

/** libc-builtin printf/puts/putchar output of the module: collected per
 *  line and logged with uc_app_log_line() at level inf (app thread).
 *  uc_app_print_flush() logs an unterminated line (end of a callback). */
void uc_app_print_char(struct uc_app_slot *s, char c);
void uc_app_print_flush(struct uc_app_slot *s);

#ifdef __cplusplus
}
#endif

#endif /* UC_APP_INTERNAL_H_ */
