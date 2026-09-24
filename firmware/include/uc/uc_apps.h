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
 *   the "failed" state.
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
int uc_apps_stop(const char *name);    /* -ENOENT, stopping a stopped app is 0 */
/** Stop, remove from apps.json and optionally delete the module file and
 *  the app's /lfs/data directory. */
int uc_apps_remove(const char *name, bool delete_file);

/** Re-read apps.json: stop apps that were removed or whose entry changed,
 *  start autostart apps that are not running. */
int uc_apps_reload(void);

size_t uc_apps_installed(void);
size_t uc_apps_running(void);

/** Status by index (0..uc_apps_installed()-1) or by name. */
int uc_apps_status(size_t index, struct uc_app_status *out);
int uc_apps_status_by_name(const char *name, struct uc_app_status *out);

struct uc_wasm_info {
	bool interp;
	bool aot;
	const char *aot_target; /* e.g. "thumbv7em", "" without AOT */
	uint32_t pool_total;
	uint32_t pool_free;
};

void uc_apps_wasm_info(struct uc_wasm_info *out);

#ifdef __cplusplus
}
#endif

#endif /* UC_APPS_H_ */
