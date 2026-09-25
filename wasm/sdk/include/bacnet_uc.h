/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * bacnet_uc.h - guest-side API for BACnet-uc WebAssembly applications.
 *
 * This header is the ABI contract between a WebAssembly application and the
 * BACnet-uc firmware (firmware/src/apps/uc_app_host_api.c, host stub for
 * native tests: wasm/sdk/host-stub). Every function declared with
 * UC_IMPORT is resolved by the host from the import module "bacnet_uc".
 * Every function the application defines with UC_EXPORT is looked up by the
 * host by name; all of them are optional except uc_app_api_version, which
 * UC_APP_DECLARE() provides.
 *
 * Build with wasm/sdk/uc-cc (wasm/README.md "uc-cc"; "uc-cc --print-flags"
 * prints the flags for other build systems, see also wasm/sdk/Makefile.inc
 * and wasm/sdk/cmake):
 *   uc-cc [-O z|s|0..3] [-I dir] [-D name] -o app.wasm app.c [more.c ...]
 * compiles with
 *   clang --target=wasm32 -nostdlib -mcpu=mvp -msign-ext
 *         -mnontrapping-fptoint -mbulk-memory -Oz -Wall -Wextra -I<sdk>/include
 * links with
 *   -Wl,--no-entry -Wl,--stack-first -Wl,-z,stack-size=4096
 *   -Wl,--initial-memory=65536 -Wl,--max-memory=65536
 *   -Wl,--export=__heap_base -Wl,--export=__data_end
 *   -Wl,--allow-undefined-file=<libc-builtin symbols> -Wl,--strip-all
 * and checks the module against this ABI. There is no --allow-undefined:
 * the functions below are imports through their import_module attribute,
 * and only the C library subset of uc_libc.h (WAMR libc-builtin, module
 * "env") may stay undefined. The stack is enlarged so that __heap_base is a
 * multiple of 4096 (WAMR shrinks the linear memory to __heap_base plus the
 * app heap, apps.json "heap_kb").
 *
 * Execution model
 *   - Each application instance runs in its own firmware thread. Host calls
 *     into the application are never concurrent for one instance.
 *   - uc_app_init() runs once after instantiation, then uc_app_tick() runs
 *     every period_ms (from the app manifest, changeable at run time with
 *     uc_set_tick_period()). Events (uc_app_on_cov, uc_app_on_write) are
 *     delivered between ticks, in arrival order.
 *   - Host functions may block the calling application thread: remote
 *     BACnet requests wait for the confirmation up to timeout_ms, key/value
 *     calls wait for the file system. They never block other applications
 *     or the BACnet stack.
 *   - A callback that runs longer than the watchdog
 *     (CONFIG_UC_APP_WATCHDOG_MS, default 2000 ms) is terminated and the
 *     application enters the failed state. The watchdog pauses while a
 *     host call blocks (uc_remote_* to another device, uc_kv_*): only
 *     execution time counts. On native_sim, simulated time stands still
 *     while a callback computes, so there a budget of interpreted
 *     instructions per callback (CONFIG_WAMR_INSTRUCTION_LIMIT) takes the
 *     watchdog's place.
 *   - Stopping an application cancels its blocking host calls: a remote
 *     request in flight is abandoned within about 50 ms and returns
 *     UC_ERR_TIMEOUT; once a stop is pending, further blocking calls fail
 *     at once (uc_remote_*: UC_ERR_TIMEOUT, uc_kv_*: UC_ERR_IO) and no
 *     longer pause the watchdog, so the callback must return.
 *   - No module code may run during instantiation: the host refuses (SMP rc
 *     VERIFY) a module with a start function or exporting
 *     __wasm_call_ctors, __post_instantiate or _initialize. uc-cc builds
 *     conforming modules; do global setup in uc_app_init().
 *   - printf/puts output of an application is logged like uc_log() at
 *     info level (see uc_libc.h).
 *
 * Pointer arguments
 *   A pointer (with its length) passed to a bacnet_uc function must lie in
 *   the application's linear memory. The host checks the whole range; a
 *   NULL pointer, a range outside the memory or a zero length where data is
 *   needed returns UC_ERR_INVALID (no trap). The C library functions of
 *   uc_libc.h trap on invalid pointers instead.
 *
 * Values
 *   Numeric property values cross the ABI as double. The host converts to
 *   and from the property's BACnet datatype:
 *     REAL, DOUBLE                  <-> value (REAL: finite and within the
 *                                       float range on write)
 *     UNSIGNED, SIGNED, ENUMERATED  <-> value (integral and within the
 *                                       datatype's range on write)
 *     BOOLEAN                       <-> 0.0 / 1.0
 *     binary PV (BI/BO/BV)          <-> 0.0 inactive / 1.0 active
 *   A write is never truncated or rounded: NaN, an infinity, a REAL beyond
 *   the float range, a fraction for an integer datatype (e.g. 2.7 for a
 *   multi-state value), a value out of range, and anything but exactly 0.0
 *   or 1.0 for BOOLEAN and a binary present value (Present_Value,
 *   Relinquish_Default) return UC_ERR_INVALID; the property keeps its value.
 *   Other datatypes return UC_ERR_TYPE.
 *
 * Priorities (uc_prop_write*, uc_remote_write*, uc_app_on_write)
 *   The Present_Value of the commandable objects AO, BO and MSO has a
 *   priority array: a write goes into slot priority 1..16 (UC_PRIORITY_NONE
 *   means no priority, which BACnet treats as 16), a relinquish (NULL
 *   write) clears the slot, and the highest set slot (or
 *   Relinquish_Default) is the Present_Value. The value objects AV, BV and
 *   MSV of BACnet-uc nodes have no priority array: a write sets the
 *   Present_Value whatever the priority (except priority 6 on AV, below),
 *   and a relinquish succeeds and changes nothing (ASHRAE 135 clause
 *   15.9.2). A relinquish of any other property or of an input's
 *   Present_Value returns UC_ERR_TYPE. Other properties ignore the
 *   priority. Devices of other vendors may make value objects commandable.
 *   Priority 6 is reserved for Minimum_On/Off_Time (135 clause 19.2.3): a
 *   write or relinquish at priority 6 of the Present_Value of AO, BO and
 *   MSO, and a write at priority 6 to an AV, return UC_ERR_PERM
 *   (write-access-denied); BV and MSV ignore it like any priority. Do not
 *   use priority 6.
 *
 * Errors
 *   Functions returning int32_t return >= 0 on success and a negative
 *   UC_ERR_* code on failure. The host counts failures in the app status
 *   ("errors"), except UC_ERR_NOT_FOUND of uc_param_get,
 *   uc_param_get_number and uc_kv_get (a missing key selects a default).
 */
#ifndef BACNET_UC_H
#define BACNET_UC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** ABI version. Major in the upper 16 bits, minor in the lower 16 bits. The
 *  host refuses to start an application whose major differs from its own. */
#define UC_API_VERSION_MAJOR 1
#define UC_API_VERSION_MINOR 0
#define UC_API_VERSION ((UC_API_VERSION_MAJOR << 16) | UC_API_VERSION_MINOR)

#if defined(__wasm__)
#define UC_IMPORT(name) \
	__attribute__((import_module("bacnet_uc"), import_name(#name)))
#define UC_EXPORT(name) __attribute__((export_name(#name), used))
#else
/* Allows host-side unit tests of application logic with a native stub. */
#define UC_IMPORT(name)
#define UC_EXPORT(name)
#endif

/* ---------------------------------------------------------------------- */
/* Error codes                                                             */
/* ---------------------------------------------------------------------- */
#define UC_OK              0
#define UC_ERR_INVALID    -1 /**< bad argument, bad pointer, bad length */
#define UC_ERR_NOT_FOUND  -2 /**< object, property, channel, key missing */
#define UC_ERR_PERM       -3 /**< permission not granted in the manifest */
#define UC_ERR_TIMEOUT    -4 /**< no confirmation within timeout_ms */
#define UC_ERR_BUSY       -5 /**< out of transaction slots, retry later */
#define UC_ERR_NO_MEM     -6 /**< host could not allocate */
#define UC_ERR_BACNET     -7 /**< remote BACnet Error/Reject/Abort PDU */
#define UC_ERR_UNSUPPORTED -8 /**< feature not built into this firmware */
#define UC_ERR_IO         -9 /**< hardware or file system error */
#define UC_ERR_TYPE      -10 /**< property datatype not representable */
#define UC_ERR_EXISTS    -11 /**< object already exists */
#define UC_ERR_NO_ROUTE  -12 /**< remote device could not be bound */

/* ---------------------------------------------------------------------- */
/* Constants                                                               */
/* ---------------------------------------------------------------------- */
/** array_index value meaning "not an array access" (BACNET_ARRAY_ALL). */
#define UC_ARRAY_ALL (-1)
/** priority value "no priority": commandable objects (AO, BO, MSO) take
 *  such a write at priority 16 (see "Priorities" above). */
#define UC_PRIORITY_NONE 0
/** device argument of uc_remote_*() and uc_cov_subscribe() meaning "this
 *  device" (the node's own device instance works the same way). */
#define UC_DEVICE_LOCAL 0xFFFFFFFFu

/* Log levels (match Zephyr LOG_LEVEL_*). */
#define UC_LOG_ERR 1
#define UC_LOG_WRN 2
#define UC_LOG_INF 3
#define UC_LOG_DBG 4

/* BACnet object types (subset, ASHRAE 135 values). */
#define UC_OBJ_ANALOG_INPUT       0u
#define UC_OBJ_ANALOG_OUTPUT      1u
#define UC_OBJ_ANALOG_VALUE       2u
#define UC_OBJ_BINARY_INPUT       3u
#define UC_OBJ_BINARY_OUTPUT      4u
#define UC_OBJ_BINARY_VALUE       5u
#define UC_OBJ_DEVICE             8u
#define UC_OBJ_MULTI_STATE_INPUT  13u
#define UC_OBJ_MULTI_STATE_OUTPUT 14u
#define UC_OBJ_MULTI_STATE_VALUE  19u

/* BACnet property identifiers (subset). */
#define UC_PROP_DESCRIPTION     28u
#define UC_PROP_OBJECT_NAME     77u
#define UC_PROP_OUT_OF_SERVICE  81u
#define UC_PROP_PRESENT_VALUE   85u
#define UC_PROP_RELINQUISH_DEFAULT 104u
#define UC_PROP_STATUS_FLAGS    111u
#define UC_PROP_UNITS           117u
#define UC_PROP_COV_INCREMENT   22u
#define UC_PROP_NUMBER_OF_STATES 74u

/* ---------------------------------------------------------------------- */
/* Host imports: logging, time, scheduling                                 */
/* ---------------------------------------------------------------------- */
/** Emit a log line through the firmware logging subsystem (tagged with the
 *  application name; ends up on the console, in /lfs/log and in syslog).
 *  Lines are cut at 120 characters, trailing CR/LF removed, control
 *  characters replaced; more than 20 lines per second are dropped (and
 *  counted in a warning). */
UC_IMPORT(uc_log)
void uc_log(int32_t level, const char *msg, uint32_t len);

/** Milliseconds since boot. */
UC_IMPORT(uc_uptime_ms)
uint64_t uc_uptime_ms(void);

/** Change the tick period (10..3600000 ms, else UC_ERR_INVALID). 0 disables
 *  ticks (events only). The next tick is one period after the call. */
UC_IMPORT(uc_set_tick_period)
int32_t uc_set_tick_period(uint32_t period_ms);

/* ---------------------------------------------------------------------- */
/* Host imports: deployment parameters (apps.json "params")                */
/* ---------------------------------------------------------------------- */
/** Copy the parameter value for key into buf (NUL-terminated if it fits).
 *  Returns the value length without NUL, UC_ERR_NOT_FOUND, or
 *  UC_ERR_INVALID if buf is too small. */
UC_IMPORT(uc_param_get)
int32_t uc_param_get(const char *key, uint32_t key_len, char *buf,
		     uint32_t buf_len);

/** Parse the parameter value for key as a number (strtod semantics).
 *  UC_ERR_NOT_FOUND for a missing key, UC_ERR_TYPE if it is no number. */
UC_IMPORT(uc_param_get_number)
int32_t uc_param_get_number(const char *key, uint32_t key_len, double *out);

/* ---------------------------------------------------------------------- */
/* Host imports: local BACnet objects (permission "bacnet.local")          */
/* ---------------------------------------------------------------------- */
/** Create a local object owned by this application. Supported types: AI,
 *  AO, AV, BI, BO, BV, MSI, MSO, MSV. Objects created by an application
 *  are deleted when it stops. Returns UC_ERR_EXISTS if the object exists
 *  and is owned by someone else (IO configuration or another app); an
 *  object already owned by this app is left as is and UC_OK returned. */
UC_IMPORT(uc_obj_create)
int32_t uc_obj_create(uint32_t type, uint32_t instance, const char *name,
		      uint32_t name_len);

/** Delete a local object owned by this application. */
UC_IMPORT(uc_obj_delete)
int32_t uc_obj_delete(uint32_t type, uint32_t instance);

/** Read a numeric property of any local object. */
UC_IMPORT(uc_prop_read)
int32_t uc_prop_read(uint32_t type, uint32_t instance, uint32_t prop,
		     int32_t array_index, double *out);

/** Write a numeric property of a local object, with the rules of a BACnet
 *  WriteProperty request. priority 0..16 (UC_PRIORITY_NONE = no priority):
 *  AO, BO, MSO Present_Value go into the priority array, AV, BV, MSV and
 *  other properties ignore the priority, except that priority 6 is
 *  UC_ERR_PERM for AO, BO, MSO and AV (see "Priorities" above). The value
 *  must fit the property's datatype, else UC_ERR_INVALID (see "Values"). */
UC_IMPORT(uc_prop_write)
int32_t uc_prop_write(uint32_t type, uint32_t instance, uint32_t prop,
		      int32_t array_index, double value, uint32_t priority);

/** Relinquish (write NULL) a Present_Value at priority 0..16
 *  (UC_PRIORITY_NONE: 16). AO, BO, MSO: clears that slot of the priority
 *  array. AV, BV, MSV (no priority array): UC_OK, nothing changes. Inputs
 *  and other properties: UC_ERR_TYPE. */
UC_IMPORT(uc_prop_write_null)
int32_t uc_prop_write_null(uint32_t type, uint32_t instance, uint32_t prop,
			   uint32_t priority);

/** Write a CharacterString property (object-name, description), without
 *  priority. len 0 writes an empty string; embedded NULs are invalid. */
UC_IMPORT(uc_prop_write_string)
int32_t uc_prop_write_string(uint32_t type, uint32_t instance, uint32_t prop,
			     const char *str, uint32_t len);

/* ---------------------------------------------------------------------- */
/* Host imports: remote BACnet devices (permission "bacnet.remote")        */
/* ---------------------------------------------------------------------- */
/** ReadProperty on a remote device. The device is bound dynamically
 *  (Who-Is/I-Am) or through a static binding from device.json.
 *  uc_remote_read/uc_remote_write/uc_remote_write_null with device
 *  UC_DEVICE_LOCAL or the node's own device instance are served locally
 *  like uc_prop_read/uc_prop_write/uc_prop_write_null: no request,
 *  timeout_ms unused, permission "bacnet.local" instead of
 *  "bacnet.remote". The watchdog pauses while a remote request waits. */
UC_IMPORT(uc_remote_read)
int32_t uc_remote_read(uint32_t device, uint32_t type, uint32_t instance,
		       uint32_t prop, int32_t array_index, double *out,
		       uint32_t timeout_ms);

/** WriteProperty on a remote device (priority as for uc_prop_write; a
 *  BACnet-uc node's AV, BV, MSV ignore it, except priority 6 on AV). The
 *  value is converted as described in "Values" before the request is sent
 *  (UC_ERR_INVALID if it does not fit the datatype). The datatype is taken
 *  from the host's table of standard numeric properties, which covers AI,
 *  AO, AV, BI, BO, BV, MSI, MSO, MSV, Integer Value, Positive Integer
 *  Value, Large Analog Value, Accumulator, Loop, Pulse Converter, Lighting
 *  Output and Binary Lighting Output; other properties return
 *  UC_ERR_TYPE. */
UC_IMPORT(uc_remote_write)
int32_t uc_remote_write(uint32_t device, uint32_t type, uint32_t instance,
			uint32_t prop, int32_t array_index, double value,
			uint32_t priority, uint32_t timeout_ms);

/** WriteProperty NULL (relinquish) on a remote device (see
 *  uc_prop_write_null for the effect on a BACnet-uc node). */
UC_IMPORT(uc_remote_write_null)
int32_t uc_remote_write_null(uint32_t device, uint32_t type,
			     uint32_t instance, uint32_t prop,
			     uint32_t priority, uint32_t timeout_ms);

/** Subscribe to present-value changes of an object. device may be a remote
 *  device instance (SubscribeCOV, renewed by the host before lifetime_s
 *  expires; permission "bacnet.remote") or UC_DEVICE_LOCAL / the local
 *  instance (host-internal change detection, no network traffic;
 *  permission "bacnet.local"). Does not block. Returns a subscription id
 *  >= 0; notifications arrive through uc_app_on_cov(), the first one with
 *  the current value right after subscribing. */
UC_IMPORT(uc_cov_subscribe)
int32_t uc_cov_subscribe(uint32_t device, uint32_t type, uint32_t instance,
			 uint32_t lifetime_s);

/** Cancel a subscription. */
UC_IMPORT(uc_cov_unsubscribe)
int32_t uc_cov_unsubscribe(int32_t sub_id);

/* ---------------------------------------------------------------------- */
/* Host imports: raw IO channels (permission "io")                         */
/* ---------------------------------------------------------------------- */
/** Look up an IO channel by catalog name ("di0", "ai1", ...). Returns the
 *  channel id >= 0. Prefer BACnet objects bound in io.json; raw access is
 *  meant for channels not mapped to an object. */
UC_IMPORT(uc_io_find)
int32_t uc_io_find(const char *name, uint32_t len);

/** Read a channel in engineering units (di/do: 0/1, ai: mV, ao: %). */
UC_IMPORT(uc_io_read)
int32_t uc_io_read(int32_t channel, double *out);

/** Write an output channel: do non-zero = 1, ao clamped to 0..100 %.
 *  UC_ERR_PERM for an input channel, UC_ERR_INVALID for NaN or an
 *  infinity. */
UC_IMPORT(uc_io_write)
int32_t uc_io_write(int32_t channel, double value);

/* ---------------------------------------------------------------------- */
/* Host imports: persistent key/value store (permission "kv")              */
/* ---------------------------------------------------------------------- */
/** Read a value stored with uc_kv_set(). Returns the stored length (which
 *  may exceed buf_len; only buf_len bytes are copied), UC_ERR_NOT_FOUND
 *  for a key never stored, or an error. Keys are 1..31 characters from
 *  [A-Za-z0-9_.-]. */
UC_IMPORT(uc_kv_get)
int32_t uc_kv_get(const char *key, uint32_t key_len, void *buf,
		  uint32_t buf_len);

/** Store a value (max CONFIG_UC_APP_KV_VALUE_MAX bytes) persistently in
 *  /lfs/data/<app>/<key>. */
UC_IMPORT(uc_kv_set)
int32_t uc_kv_set(const char *key, uint32_t key_len, const void *val,
		  uint32_t val_len);

/* ---------------------------------------------------------------------- */
/* Application exports (define the ones you need)                          */
/* ---------------------------------------------------------------------- */
/*
 * int32_t uc_app_init(void);
 *     Called once. Return 0 to run, non-zero to abort the start.
 * void uc_app_tick(uint64_t now_ms);
 *     Called every tick period.
 * void uc_app_on_cov(int32_t sub_id, uint32_t device, uint32_t type,
 *                    uint32_t instance, uint32_t prop, double value);
 *     A subscribed value changed.
 * void uc_app_on_write(uint32_t type, uint32_t instance, uint32_t prop,
 *                      uint32_t priority, double value);
 *     A BACnet client, the management interface or another application
 *     wrote a numeric property of an object this application owns (not
 *     for the application's own writes, not for a relinquish). priority
 *     is the write's priority 1..16 (16 for a write without priority),
 *     also for objects that ignore it (AV, BV, MSV).
 * void uc_app_deinit(void);
 *     Called before the instance is destroyed on a regular stop.
 */

/** Declares the ABI version export the host checks before uc_app_init. */
#define UC_APP_DECLARE()                                                   \
	UC_EXPORT(uc_app_api_version) uint32_t uc_app_api_version(void)    \
	{                                                                  \
		return UC_API_VERSION;                                     \
	}

/* ---------------------------------------------------------------------- */
/* Convenience helpers (header-only, no libc needed)                       */
/* ---------------------------------------------------------------------- */
static inline uint32_t uc_strlen(const char *s)
{
	uint32_t n = 0;

	while (s[n] != '\0') {
		n++;
	}
	return n;
}

/** Log a NUL-terminated string. */
static inline void uc_log_str(int32_t level, const char *msg)
{
	uc_log(level, msg, uc_strlen(msg));
}

static inline int32_t uc_param_str(const char *key, char *buf, uint32_t len)
{
	return uc_param_get(key, uc_strlen(key), buf, len);
}

/** Numeric parameter with a default for a missing/invalid key. */
static inline double uc_param_num(const char *key, double fallback)
{
	double v;

	if (uc_param_get_number(key, uc_strlen(key), &v) < 0) {
		return fallback;
	}
	return v;
}

static inline int32_t uc_obj_create_str(uint32_t type, uint32_t instance,
					const char *name)
{
	return uc_obj_create(type, instance, name, uc_strlen(name));
}

static inline int32_t uc_pv_read(uint32_t type, uint32_t instance,
				 double *out)
{
	return uc_prop_read(type, instance, UC_PROP_PRESENT_VALUE,
			    UC_ARRAY_ALL, out);
}

static inline int32_t uc_pv_write(uint32_t type, uint32_t instance,
				  double value, uint32_t priority)
{
	return uc_prop_write(type, instance, UC_PROP_PRESENT_VALUE,
			     UC_ARRAY_ALL, value, priority);
}

static inline int32_t uc_io_find_str(const char *name)
{
	return uc_io_find(name, uc_strlen(name));
}

#ifdef __cplusplus
}
#endif

#endif /* BACNET_UC_H */
