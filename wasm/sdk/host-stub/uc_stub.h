/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc_stub.h - native (host) implementation of the BACnet-uc guest ABI for
 * off-target tests of application logic.
 *
 * uc_stub.c implements every "bacnet_uc" import of bacnet_uc.h as a plain C
 * function (bacnet_uc.h maps UC_IMPORT/UC_EXPORT to nothing when __wasm__
 * is not defined), backed by:
 *   - an in-memory table of local objects (priority arrays for AO, BO,
 *     MSO; the value objects AV, BV, MSV have none, as in the firmware),
 *   - scripted remote points (values, errors, COV support),
 *   - COV subscriptions (initial notification, change detection),
 *   - a controllable clock and the firmware's tick/event scheduling,
 *   - parameters, a key/value store, raw IO channels and a log capture.
 *
 * It mirrors the firmware (firmware/src/apps/uc_app_host_api.c,
 * uc_app_mgr.c) where application-visible behaviour is concerned:
 * permissions, argument checks, return codes, the first tick one period
 * after start, ticks before events, "writes by others" raising
 * uc_app_on_write (numeric, non-NULL only), subscription notifications
 * right after subscribing, app objects deleted on stop.
 *
 * Simplifications: analog values are stored with REAL (float) precision
 * like the firmware; every change of a local Present_Value is notified
 * (no COV increment); non-PV properties are a plain per-object store; a
 * remote read that fails with UC_ERR_TIMEOUT advances the clock by its
 * timeout (the call blocks on the target).
 *
 * The stub calls the application through struct uc_stub_app: native tests
 * use uc_stub_native_app() (uc_stub_native.c), the WAMR runner installs
 * functions that call into a WebAssembly instance.
 */
#ifndef UC_STUB_H
#define UC_STUB_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include "bacnet_uc.h"

#ifdef __cplusplus
extern "C" {
#endif

#define UC_STUB_MAX_OBJECTS  64
#define UC_STUB_MAX_PROPS    8 /* non-PV numeric properties per object */
#define UC_STUB_MAX_REMOTE   32
#define UC_STUB_MAX_SUBS     16 /* CONFIG_UC_BACNET_COV_SUBS_MAX */
#define UC_STUB_MAX_PARAMS   16 /* CONFIG_UC_APP_PARAMS_MAX */
#define UC_STUB_MAX_KV       16
#define UC_STUB_KV_VALUE_MAX 256 /* CONFIG_UC_APP_KV_VALUE_MAX */
#define UC_STUB_MAX_IO       16
#define UC_STUB_MAX_LOG      1024
#define UC_STUB_LOG_LINE_MAX 120 /* host truncates longer app log lines */
#define UC_STUB_QUEUE_LEN    16  /* CONFIG_UC_APP_EVENT_QUEUE_LEN */
#define UC_STUB_NAME_MAX     64

/* Permission bits (apps.json "perms"). */
#define UC_STUB_PERM_LOCAL  0x1u /* bacnet.local */
#define UC_STUB_PERM_REMOTE 0x2u /* bacnet.remote */
#define UC_STUB_PERM_IO     0x4u /* io */
#define UC_STUB_PERM_KV     0x8u /* kv */
#define UC_STUB_PERM_ALL    0xFu

/** Owner of objects added with uc_stub_obj_add() (IO configuration). */
#define UC_STUB_OWNER_IO  2
#define UC_STUB_OWNER_APP 16

/** How the stub calls the application. NULL members are "not exported". */
struct uc_stub_app {
	uint32_t (*api_version)(void);
	int32_t (*init)(void);
	void (*tick)(uint64_t now_ms);
	void (*on_cov)(int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance,
		       uint32_t prop, double value);
	void (*on_write)(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority,
			 double value);
	void (*deinit)(void);
	/** Optional: true when the application trapped (WAMR runner). */
	bool (*failed)(void);
};

/** Functions of the application linked into the test executable. */
const struct uc_stub_app *uc_stub_native_app(void);

/* ---------------------------------------------------------------------- */
/* Setup                                                                   */
/* ---------------------------------------------------------------------- */

/** Clear everything (objects, remote points, subscriptions, parameters,
 *  key/value store, IO, log, clock = 0) and restore the defaults: all
 *  permissions, local device 1000, period 1000 ms. */
void uc_stub_reset(void);

void uc_stub_set_app(const struct uc_stub_app *app);
void uc_stub_set_perms(uint32_t perms);
/** apps.json "period_ms" (0 = events only; values below 10 become 10). */
void uc_stub_set_period(uint32_t period_ms);
void uc_stub_set_local_device(uint32_t instance);
uint32_t uc_stub_local_device(void);
/** Log lines to stderr as they are emitted (also: env UC_STUB_VERBOSE=1). */
void uc_stub_set_verbose(bool verbose);

/** Set (or replace) a parameter. Returns 0 or -1 when the table is full. */
int uc_stub_param_set(const char *key, const char *value);
void uc_stub_param_clear(void);

/* ---------------------------------------------------------------------- */
/* Application lifecycle and time                                          */
/* ---------------------------------------------------------------------- */

/** Start like the firmware: check uc_app_api_version (major 1), call
 *  uc_app_init. Returns 0 (running), the non-zero init result, or -100
 *  for a missing/incompatible api version, -101 for a trap. A failed
 *  start cleans up like a stop (objects, subscriptions, events). */
int32_t uc_stub_start(void);

/** Regular stop: uc_app_deinit, then delete app objects, cancel
 *  subscriptions and drop queued events. The key/value store survives. */
void uc_stub_stop(void);

bool uc_stub_running(void);

/** Advance the clock by ms, running due ticks and delivering queued
 *  events on the way (firmware order: a due tick first, then events).
 *  Returns false when the application trapped (runner only). */
bool uc_stub_run(uint64_t ms);

uint64_t uc_stub_now(void);
/** Move the clock without running anything. */
void uc_stub_advance(uint64_t ms);

uint32_t uc_stub_ticks(void);
uint32_t uc_stub_events(void);
/** Negative results the application received (firmware status "errors"). */
uint32_t uc_stub_errors(void);
uint32_t uc_stub_tick_period(void);
/** Events dropped because the queue was full. */
uint32_t uc_stub_events_dropped(void);

/* ---------------------------------------------------------------------- */
/* Local objects                                                           */
/* ---------------------------------------------------------------------- */

/** Add an object owned by the IO configuration (not the app). For the
 *  commandable types (AO, BO, MSO) value becomes the relinquish default.
 *  As in the firmware, the value objects (AV, BV, MSV) have no priority
 *  array: writes ignore the priority, a relinquish succeeds and changes
 *  nothing. Returns 0, -1 (full) or -2 (exists). */
int uc_stub_obj_add(uint32_t type, uint32_t instance, const char *name, double value);
bool uc_stub_obj_exists(uint32_t type, uint32_t instance);
bool uc_stub_obj_app_owned(uint32_t type, uint32_t instance);
const char *uc_stub_obj_name(uint32_t type, uint32_t instance);
/** Effective Present_Value; NAN if the object does not exist. */
double uc_stub_obj_pv(uint32_t type, uint32_t instance);
/** Priority array slot 1..16 of a commandable object: true and *value if
 *  set. */
bool uc_stub_obj_prio(uint32_t type, uint32_t instance, uint32_t priority, double *value);
/** Numeric non-PV property stored by the app; NAN if never written. */
double uc_stub_obj_prop(uint32_t type, uint32_t instance, uint32_t prop);
/** Present_Value writes (with a value) since the object was created. */
uint32_t uc_stub_obj_writes(uint32_t type, uint32_t instance);
/** Set the Present_Value as the IO layer would (input sampled): no
 *  uc_app_on_write, local COV subscribers are notified on change. */
int uc_stub_obj_set_pv(uint32_t type, uint32_t instance, double value);

/** WriteProperty by a BACnet client (priority 0 = none -> 16). Raises
 *  uc_app_on_write when the app owns the object. Returns UC_* code. */
int32_t uc_stub_client_write(uint32_t type, uint32_t instance, uint32_t prop, double value,
			     uint32_t priority);
/** Relinquish by a BACnet client (no uc_app_on_write, like the firmware). */
int32_t uc_stub_client_relinquish(uint32_t type, uint32_t instance, uint32_t priority);

/* ---------------------------------------------------------------------- */
/* Remote devices                                                          */
/* ---------------------------------------------------------------------- */

/** Add a remote point (Present_Value). Its device becomes reachable.
 *  Returns 0, -1 when the table is full. */
int uc_stub_remote_add(uint32_t device, uint32_t type, uint32_t instance, double value);
/** Change a remote value. notify: send a COV notification to active
 *  subscriptions of this point (false simulates a lost notification). */
int uc_stub_remote_set(uint32_t device, uint32_t type, uint32_t instance, double value,
		       bool notify);
/** Make reads/writes of the point fail with err (UC_ERR_*; 0 clears). */
int uc_stub_remote_fail(uint32_t device, uint32_t type, uint32_t instance, int32_t err);
/** Current value (NAN if unknown) and the last write priority. */
double uc_stub_remote_value(uint32_t device, uint32_t type, uint32_t instance);
uint32_t uc_stub_remote_writes(uint32_t device, uint32_t type, uint32_t instance);
uint32_t uc_stub_remote_last_priority(uint32_t device, uint32_t type, uint32_t instance);
bool uc_stub_remote_relinquished(uint32_t device, uint32_t type, uint32_t instance);
uint32_t uc_stub_remote_reads(uint32_t device, uint32_t type, uint32_t instance);

/* ---------------------------------------------------------------------- */
/* COV subscriptions                                                       */
/* ---------------------------------------------------------------------- */

/** Active subscriptions of the application. */
uint32_t uc_stub_subs_active(void);
/** Subscription id for a point or -1. */
int32_t uc_stub_sub_find(uint32_t device, uint32_t type, uint32_t instance);
/** Queue a notification for subscription sub_id with value (as a remote
 *  device would send it). Returns 0 or -1 (unknown id). */
int uc_stub_cov_notify(int32_t sub_id, double value);

/* ---------------------------------------------------------------------- */
/* Fault injection                                                         */
/* ---------------------------------------------------------------------- */

enum uc_stub_fn {
	UC_STUB_FN_OBJ_CREATE,
	UC_STUB_FN_PROP_READ,
	UC_STUB_FN_PROP_WRITE,
	UC_STUB_FN_REMOTE_READ,
	UC_STUB_FN_REMOTE_WRITE,
	UC_STUB_FN_COV_SUBSCRIBE,
	UC_STUB_FN_KV_GET,
	UC_STUB_FN_KV_SET,
	UC_STUB_FN_COUNT,
};

/** The next count calls of fn return err (count < 0: until cleared with
 *  count 0). */
void uc_stub_fail(enum uc_stub_fn fn, int32_t err, int32_t count);

/* ---------------------------------------------------------------------- */
/* Key/value store, IO channels                                            */
/* ---------------------------------------------------------------------- */

/** Stored length (value copied up to size) or -1 when missing. */
int uc_stub_kv_get(const char *key, void *buf, size_t size);
int uc_stub_kv_set(const char *key, const void *val, size_t len);

/** Add a raw IO channel. output: writable by uc_io_write. */
int uc_stub_io_add(const char *name, double value, bool output);
double uc_stub_io_get(const char *name);
int uc_stub_io_set(const char *name, double value);

/* ---------------------------------------------------------------------- */
/* Log capture                                                             */
/* ---------------------------------------------------------------------- */

size_t uc_stub_log_count(void);
/** Line i (NUL-terminated, truncated like the firmware) and its level. */
const char *uc_stub_log_line(size_t i, int32_t *level);
/** Lines containing substr at the given level (0: any level). */
size_t uc_stub_log_matches(int32_t level, const char *substr);
/** Lines above the firmware limit of 20 per second (would be dropped). */
size_t uc_stub_log_excess(void);
void uc_stub_log_clear(void);
void uc_stub_log_dump(void);

/** Canonical dump of the whole state (objects with priority arrays,
 *  remote points, subscriptions, key/value store, IO, counters, log) for
 *  comparing a native and a WebAssembly run of the same scenario. */
void uc_stub_dump(FILE *f);

#ifdef __cplusplus
}
#endif

#endif /* UC_STUB_H */
