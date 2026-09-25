/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * WebAssembly application manager (WAMR).
 *
 * - One WAMR runtime with a static memory pool (CONFIG_UC_APP_POOL_SIZE).
 * - Up to CONFIG_UC_APPS_MAX installed apps (apps.json), each running in its
 *   own thread (stack CONFIG_UC_APP_THREAD_STACK_SIZE) with its own module
 *   instance, event queue and tick timer.
 * - Host functions of import module "bacnet_uc" implement
 *   wasm/sdk/include/bacnet_uc.h, gated by the app's permissions.
 * - Objects an app creates are owned by UC_OWNER_APP_BASE + slot and are
 *   deleted when it stops; writes to them by others raise
 *   uc_app_on_write in the app.
 * - A watchdog terminates a callback that runs longer than
 *   CONFIG_UC_APP_WATCHDOG_MS (wasm_runtime_terminate); the app enters
 *   the "failed" state. Blocking host calls do not count, unless a stop is
 *   pending: a stop cancels them, so it never waits for an app blocked in
 *   (or looping on) remote requests. On native_sim the timer cannot
 *   interrupt a busy loop (simulated time stands still); an instruction
 *   budget per call (CONFIG_WAMR_INSTRUCTION_LIMIT) stops it there.
 * - Modules whose instantiation would run code (start function, exported
 *   __wasm_call_ctors / __post_instantiate / _initialize) are refused at start.
 * - The modules' libc-builtin printf/puts/putchar output becomes log lines
 *   of the app, like uc_log (level inf).
 */
#ifndef UC_APPS_H_
#define UC_APPS_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "uc_config.h"

#ifdef __cplusplus
extern "C" {
#endif

enum uc_app_state {
	UC_APP_STOPPED,
	UC_APP_STARTING,
	UC_APP_RUNNING,
	UC_APP_FAILED,
};

const char *uc_app_state_str(enum uc_app_state state);

struct uc_app_status {
	struct uc_app_cfg cfg;
	enum uc_app_state state;
	uint32_t ticks;
	uint32_t events;
	uint32_t errors;
	char last_error[80];
	uint64_t uptime_ms;
};

/** Initialise WAMR, register the host API, load the cached apps.json and
 *  start autostart apps (their threads wait for uc_bn_ready()). */
int uc_apps_init(void);

/** Install or replace an app (see management-protocol.md "install").
 *  Validates the file (size, optional sha256, WASM "\0asm" or AOT "\0aot"
 *  magic), persists apps.json. Starts it if cfg->autostart, restarting a
 *  running instance only when restart is true (-EALREADY otherwise). */
int uc_apps_install(const struct uc_app_cfg *cfg, bool restart);

int uc_apps_start(const char *name);   /* -ENOENT, -EALREADY if running */
/** -ENOENT; stopping a stopped app is 0. Cancels the app's blocking host
 *  calls and waits until the app thread has cleaned up (-EBUSY if it has
 *  not after 2 * CONFIG_UC_APP_WATCHDOG_MS + 8 s). */
int uc_apps_stop(const char *name);
/** Stop, remove from apps.json and optionally delete the module file and
 *  the app's /lfs/data directory. */
int uc_apps_remove(const char *name, bool delete_file);

/** Re-read apps.json (activating a staged apps.json.new) and apply it,
 *  under the same lock as install and remove: stop apps that were removed
 *  or whose entry changed, start autostart apps that are not running. An
 *  app that does not stop keeps its old entry (it stays installed and
 *  apps.json is rewritten to list it); the first error is returned. */
int uc_apps_reload(void);

size_t uc_apps_installed(void);
size_t uc_apps_running(void);

/** Status by index (0..uc_apps_installed()-1) or by name. */
int uc_apps_status(size_t index, struct uc_app_status *out);
int uc_apps_status_by_name(const char *name, struct uc_app_status *out);

/** Name of the installed app that owns objects of owner id owner
 *  (UC_OWNER_APP_BASE + slot, see uc_common.h) into buf (NUL-terminated).
 *  Returns 0, -EINVAL (buf NULL or size 0), -ENOENT (not an app owner id,
 *  or no app bound to that slot; buf is "") or -ENOSPC (truncated). The
 *  slot index is not the uc_apps_status() index. Takes the manager lock:
 *  not from the BACnet thread (a manager call may hold it while an app
 *  thread waits for the BACnet executor). */
int uc_apps_owner_name(uint8_t owner, char *buf, size_t size);

struct uc_wasm_info {
	bool interp;
	/* AOT files load: CONFIG_WAMR_AOT=y and the pool is executable
	 * (see CONFIG_WAMR_AOT, CONFIG_WAMR_AOT_MPU_EXEC) */
	bool aot;
	const char *aot_target; /* e.g. "thumbv7em", "" when aot is false */
	uint32_t pool_total;
	uint32_t pool_free;
};

void uc_apps_wasm_info(struct uc_wasm_info *out);

#ifdef __cplusplus
}
#endif

#endif /* UC_APPS_H_ */
