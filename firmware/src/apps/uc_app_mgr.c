/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * WebAssembly application manager (uc_apps.h).
 *
 * Runtime
 *   One WAMR runtime (wasm_runtime_full_init) that allocates everything
 *   from a static pool of CONFIG_UC_APP_POOL_SIZE bytes: module images,
 *   loaded modules, instances (linear memory + app heap) and exec envs
 *   (operand stack). The host API is registered as import module
 *   "bacnet_uc" (uc_app_host_api.c).
 *
 * Slots
 *   CONFIG_UC_APPS_MAX slots. Every installed app (apps.json entry) is
 *   bound to one slot while it is installed; objects it creates are owned
 *   by UC_OWNER_APP_BASE + slot index. Each slot has one thread (created at
 *   init), an event queue and a watchdog. Manager calls are serialised by
 *   mgr_lock; a slot's cfg only changes while no instance runs, so the app
 *   thread reads it without a lock.
 *
 * App thread, one run per start
 *   STARTING: wait for uc_bn_ready() -> optional sha256 check -> module
 *   image into a pool buffer (kept until unload, WAMR references it) ->
 *   wasm_runtime_load -> every function import resolved? -> instantiate
 *   (stack_kb, heap_kb) -> exec env -> export signatures ->
 *   uc_app_api_version() major == UC_API_VERSION_MAJOR -> uc_app_init().
 *   RUNNING: uc_app_tick(now_ms) when due, otherwise wait for an event
 *   (uc_app_on_cov / uc_app_on_write) until the next tick.
 *   Stop request: uc_app_deinit() -> cleanup -> STOPPED. Trap, watchdog or
 *   failed start: cleanup -> FAILED with last_error.
 *   Cleanup: COV subscriptions, owned objects, queued events, exec env,
 *   instance, module, module image.
 *
 * Watchdog
 *   Every callback arms a k_timer of CONFIG_UC_APP_WATCHDOG_MS. Its expiry
 *   submits a work item that calls wasm_runtime_terminate() when the same
 *   callback (generation counter) still runs. With CONFIG_WAMR_THREAD_MGR
 *   the interpreter checks the terminate flag on every branch and call, so
 *   endless loops end there. Host calls that block on the network or the
 *   file system pause the timer (uc_app_wd_pause/resume): only execution
 *   time counts.
 *
 * Events
 *   COV notifications (uc_app_host_api.c) and writes to objects owned by an
 *   app (write hook, BACnet thread) are queued without blocking; a full
 *   queue drops the event and counts an error.
 */

#include <ctype.h>
#include <errno.h>
#include <stdarg.h>
#include <string.h>

#include <zephyr/devicetree.h>
#include <zephyr/fs/fs.h>
#include <zephyr/kernel.h>
#include <zephyr/linker/devicetree_regions.h>
#include <zephyr/linker/section_tags.h>
#include <zephyr/logging/log.h>
#include <zephyr/spinlock.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>

#include <wasm_export.h>

#include "uc/uc_apps.h"
#include "uc/uc_bacnet.h"
#include "uc/uc_common.h"
#include "uc/uc_config.h"
#include "uc/uc_storage.h"
#include "uc_app_internal.h"

/* Guest ABI (UC_API_VERSION_*). Its guest-side io prototypes collide with
 * uc_io.h, so they are renamed while it is included. */
#define uc_io_find  uc_guest_io_find
#define uc_io_read  uc_guest_io_read
#define uc_io_write uc_guest_io_write
#include "../../../wasm/sdk/include/bacnet_uc.h"
#undef uc_io_find
#undef uc_io_read
#undef uc_io_write

LOG_MODULE_REGISTER(uc_app_mgr, CONFIG_UC_LOG_LEVEL);

/* WAMR error message buffer (load / instantiate). */
#define APP_ERR_BUF_SIZE 128
/* Poll period while an app waits for the BACnet node (ms). */
#define APP_BN_WAIT_MS 250
/* uc_apps_start() waits this long for the outcome when BACnet is up (ms). */
#define APP_START_WAIT_MS (CONFIG_UC_APP_WATCHDOG_MS + 3000)
/* uc_apps_stop() waits this long for the app thread (ms): a running
 * callback, uc_app_deinit() and a blocking host call. */
#define APP_STOP_WAIT_MS (2 * CONFIG_UC_APP_WATCHDOG_MS + 8000)
/* Retries of uc_bn_obj_delete_owned() while the executor queue is full. */
#define APP_EXEC_RETRIES 20
#define APP_EXEC_RETRY_MS 50

BUILD_ASSERT(UC_APP_NAME_MAX == sizeof(((struct uc_app_cfg *)0)->name),
	     "app name size mismatch");
BUILD_ASSERT(sizeof(((struct uc_app_status *)0)->last_error) ==
		     sizeof(((struct uc_app_slot *)0)->last_error),
	     "last_error size mismatch");
BUILD_ASSERT((sizeof(struct uc_app_event) % 8) == 0, "k_msgq element alignment");
BUILD_ASSERT(UC_OWNER_APP_BASE + CONFIG_UC_APPS_MAX <= UINT8_MAX, "owner ids are uint8_t");

/* ---------------------------------------------------------------------- */
/* State                                                                   */
/* ---------------------------------------------------------------------- */

/*
 * WAMR pool placement (WAMR initialises the pool itself, no zeroing needed):
 * the zephyr,memory-region selected by the chosen node "uc,app-pool"
 * (e.g. SRAMX of the MCXN947, or DTCM of the STM32F7 when the pool leaves
 * room for the Ethernet DMA buffers there), otherwise .noinit of the main
 * RAM.
 */
#if DT_HAS_CHOSEN(uc_app_pool)
#define APP_POOL_SECTION Z_GENERIC_SECTION(LINKER_DT_NODE_REGION_NAME(DT_CHOSEN(uc_app_pool)))
#define APP_POOL_REGION  LINKER_DT_NODE_REGION_NAME(DT_CHOSEN(uc_app_pool))
#else
#define APP_POOL_SECTION __noinit
#define APP_POOL_REGION  "RAM"
#endif

static uint8_t __aligned(8) wamr_pool[CONFIG_UC_APP_POOL_SIZE] APP_POOL_SECTION;

K_THREAD_STACK_ARRAY_DEFINE(app_stacks, CONFIG_UC_APPS_MAX, CONFIG_UC_APP_THREAD_STACK_SIZE);

static struct uc_app_slot slots[CONFIG_UC_APPS_MAX];

/* mgr_lock: installed apps in apps.json order (slot indices). */
static uint8_t order[CONFIG_UC_APPS_MAX];
static size_t n_installed;
static bool mgr_ready;

static K_MUTEX_DEFINE(mgr_lock);
/* Serialises WAMR load / instantiate / teardown of the app threads. */
static K_MUTEX_DEFINE(wamr_lock);
/* state, last_error, fail_rc, start_ms of every slot */
static struct k_spinlock state_lock;

static bool wamr_ready;
static char aot_target[24];

/* Exports looked up in every instance. */
enum app_export {
	EXP_VERSION,
	EXP_INIT,
	EXP_TICK,
	EXP_COV,
	EXP_WRITE,
	EXP_DEINIT,
	EXP_COUNT,
};

struct export_sig {
	const char *name;
	uint8_t n_params;
	uint8_t n_results;
	wasm_valkind_t params[6];
	wasm_valkind_t result;
};

static const struct export_sig export_sigs[EXP_COUNT] = {
	[EXP_VERSION] = {"uc_app_api_version", 0, 1, {0}, WASM_I32},
	[EXP_INIT] = {"uc_app_init", 0, 1, {0}, WASM_I32},
	[EXP_TICK] = {"uc_app_tick", 1, 0, {WASM_I64}, 0},
	[EXP_COV] = {"uc_app_on_cov",
		     6,
		     0,
		     {WASM_I32, WASM_I32, WASM_I32, WASM_I32, WASM_I32, WASM_F64},
		     0},
	[EXP_WRITE] = {"uc_app_on_write", 5, 0, {WASM_I32, WASM_I32, WASM_I32, WASM_I32, WASM_F64},
		       0},
	[EXP_DEINIT] = {"uc_app_deinit", 0, 0, {0}, 0},
};

/* Resources of one run (app thread stack). */
struct app_rt {
	uint8_t *image;
	uint32_t image_len;
	wasm_module_t module;
	wasm_module_inst_t inst;
	wasm_exec_env_t env;
	wasm_function_inst_t fn[EXP_COUNT];
};

/* ---------------------------------------------------------------------- */
/* Slot state helpers                                                      */
/* ---------------------------------------------------------------------- */

const char *uc_app_state_str(enum uc_app_state state)
{
	switch (state) {
	case UC_APP_STOPPED:
		return "stopped";
	case UC_APP_STARTING:
		return "starting";
	case UC_APP_RUNNING:
		return "running";
	case UC_APP_FAILED:
		return "failed";
	default:
		return "unknown";
	}
}

static enum uc_app_state slot_state(struct uc_app_slot *s)
{
	k_spinlock_key_t key = k_spin_lock(&state_lock);
	enum uc_app_state state = s->state;

	k_spin_unlock(&state_lock, key);
	return state;
}

static void slot_set_state(struct uc_app_slot *s, enum uc_app_state state)
{
	k_spinlock_key_t key = k_spin_lock(&state_lock);

	s->state = state;
	if (state == UC_APP_RUNNING) {
		s->start_ms = k_uptime_get();
	}
	k_spin_unlock(&state_lock, key);
}

/* Record an error of the running instance: last_error, error counter, log. */
static void slot_error(struct uc_app_slot *s, const char *fmt, ...)
{
	char buf[sizeof(s->last_error)];
	k_spinlock_key_t key;
	va_list ap;

	va_start(ap, fmt);
	(void)vsnprintk(buf, sizeof(buf), fmt, ap);
	va_end(ap);

	key = k_spin_lock(&state_lock);
	memcpy(s->last_error, buf, sizeof(buf));
	k_spin_unlock(&state_lock, key);

	atomic_inc(&s->errors);
	LOG_ERR("%s: %s", s->cfg.name, buf);
}

/* Status of a slot that is not running (bind, replace, remove). */
static void slot_reset_status(struct uc_app_slot *s)
{
	k_spinlock_key_t key = k_spin_lock(&state_lock);

	s->state = UC_APP_STOPPED;
	s->last_error[0] = '\0';
	s->fail_rc = 0;
	s->start_ms = 0;
	k_spin_unlock(&state_lock, key);

	atomic_clear(&s->ticks);
	atomic_clear(&s->events);
	atomic_clear(&s->errors);
	atomic_clear(&s->dropped);
}

/* ---------------------------------------------------------------------- */
/* Watchdog                                                                */
/* ---------------------------------------------------------------------- */

static void wd_expiry(struct k_timer *timer)
{
	struct uc_app_slot *s = CONTAINER_OF(timer, struct uc_app_slot, wd_timer);

	atomic_set(&s->wd_expired_gen, atomic_get(&s->wd_gen));
	(void)k_work_submit(&s->wd_work);
}

static void wd_work_fn(struct k_work *work)
{
	struct uc_app_slot *s = CONTAINER_OF(work, struct uc_app_slot, wd_work);
	bool fired = false;

	(void)k_mutex_lock(&s->wd_lock, K_FOREVER);
	if (s->wd_in_cb && !s->wd_fired && (s->inst != NULL) &&
	    (atomic_get(&s->wd_expired_gen) == atomic_get(&s->wd_gen))) {
		s->wd_fired = true;
		wasm_runtime_terminate(s->inst);
		fired = true;
	}
	(void)k_mutex_unlock(&s->wd_lock);

	if (fired) {
		LOG_WRN("%s: callback exceeded %d ms, terminating", s->cfg.name,
			CONFIG_UC_APP_WATCHDOG_MS);
	}
}

static void wd_arm(struct uc_app_slot *s)
{
	(void)k_mutex_lock(&s->wd_lock, K_FOREVER);
	atomic_inc(&s->wd_gen);
	s->wd_in_cb = true;
	s->wd_fired = false;
	(void)k_mutex_unlock(&s->wd_lock);

	k_timer_start(&s->wd_timer, K_MSEC(CONFIG_UC_APP_WATCHDOG_MS), K_NO_WAIT);
}

/* Returns true when the watchdog terminated the callback. */
static bool wd_disarm(struct uc_app_slot *s)
{
	bool fired;

	k_timer_stop(&s->wd_timer);

	(void)k_mutex_lock(&s->wd_lock, K_FOREVER);
	s->wd_in_cb = false;
	fired = s->wd_fired;
	(void)k_mutex_unlock(&s->wd_lock);

	return fired;
}

void uc_app_wd_pause(struct uc_app_slot *s)
{
	s->wd_remaining_ms = k_timer_remaining_get(&s->wd_timer);
	k_timer_stop(&s->wd_timer);
}

void uc_app_wd_resume(struct uc_app_slot *s)
{
	/* an expiry before the pause already terminated the callback */
	k_timer_start(&s->wd_timer, K_MSEC(MAX(s->wd_remaining_ms, 1U)), K_NO_WAIT);
}

/* ---------------------------------------------------------------------- */
/* Events                                                                  */
/* ---------------------------------------------------------------------- */

bool uc_app_post_event(struct uc_app_slot *s, const struct uc_app_event *ev)
{
	atomic_val_t dropped;

	if ((s == NULL) || (ev == NULL) || !atomic_get(&s->accept)) {
		return false;
	}

	if (k_msgq_put(&s->msgq, ev, K_NO_WAIT) != 0) {
		dropped = atomic_inc(&s->dropped) + 1;
		atomic_inc(&s->errors);
		if (IS_POWER_OF_TWO(dropped)) {
			LOG_WRN("%s: event queue full, %ld events dropped", s->cfg.name,
				(long)dropped);
		}
		return false;
	}

	return true;
}

/* Write hook (BACnet thread, must not block): writes by others to objects
 * an app owns become uc_app_on_write events. NULL (relinquish) and
 * non-numeric values are not forwarded. */
static void app_write_hook(uint8_t owner, uint8_t writer, uint16_t type, uint32_t instance,
			   uint32_t prop, uint8_t priority,
			   const BACNET_APPLICATION_DATA_VALUE *value)
{
	struct uc_app_event ev = {0};
	double v;

	if (!UC_OWNER_IS_APP(owner) || (UC_OWNER_APP_SLOT(owner) >= CONFIG_UC_APPS_MAX) ||
	    (writer == owner)) {
		return;
	}
	if ((value == NULL) || (uc_value_to_double(value, &v) < 0)) {
		return;
	}

	ev.kind = UC_APP_EV_WRITE;
	ev.type = type;
	ev.instance = instance;
	ev.prop = prop;
	ev.priority = priority;
	ev.value = v;
	(void)uc_app_post_event(&slots[UC_OWNER_APP_SLOT(owner)], &ev);
}

/* ---------------------------------------------------------------------- */
/* Module files                                                            */
/* ---------------------------------------------------------------------- */

static int module_check_header(const uint8_t hdr[8])
{
	static const uint8_t wasm_hdr[8] = {0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00};
	static const uint8_t aot_magic[4] = {0x00, 0x61, 0x6f, 0x74};

	if (memcmp(hdr, wasm_hdr, 4) == 0) {
		/* binary format version 1 */
		return (memcmp(hdr, wasm_hdr, sizeof(wasm_hdr)) == 0) ? 0 : -EILSEQ;
	}
	if (memcmp(hdr, aot_magic, sizeof(aot_magic)) == 0) {
		return IS_ENABLED(CONFIG_WAMR_AOT) ? 0 : -ENOTSUP;
	}

	return -EILSEQ;
}

static int module_sha256_check(const struct uc_app_cfg *cfg)
{
	uint8_t digest[32];
	int rc;

	rc = uc_storage_sha256(cfg->file, digest);
	if (rc < 0) {
		return rc;
	}

	return (memcmp(digest, cfg->sha256, sizeof(digest)) == 0) ? 0 : -EILSEQ;
}

/* Install-time checks: size, magic, optional sha256. */
static int module_verify(const struct uc_app_cfg *cfg)
{
	struct fs_file_t file;
	uint8_t hdr[8];
	size_t size;
	ssize_t n;
	int rc;

	rc = uc_storage_file_size(cfg->file, &size);
	if (rc < 0) {
		return rc;
	}
	if (size > CONFIG_UC_APP_MAX_FILE_SIZE) {
		return -EFBIG;
	}
	if (size < sizeof(hdr)) {
		return -EILSEQ;
	}

	fs_file_t_init(&file);
	rc = fs_open(&file, cfg->file, FS_O_READ);
	if (rc < 0) {
		return rc;
	}
	n = fs_read(&file, hdr, sizeof(hdr));
	(void)fs_close(&file);
	if (n < 0) {
		return (int)n;
	}
	if (n != (ssize_t)sizeof(hdr)) {
		return -EIO;
	}

	rc = module_check_header(hdr);
	if (rc < 0) {
		return rc;
	}

	return cfg->has_sha256 ? module_sha256_check(cfg) : 0;
}

/* Read a module image into a buffer from the WAMR pool. */
static int module_read(const char *path, uint8_t **image, uint32_t *image_len)
{
	struct fs_file_t file;
	size_t done = 0;
	size_t size;
	uint8_t *buf;
	int rc;

	rc = uc_storage_file_size(path, &size);
	if (rc < 0) {
		return rc;
	}
	if (size > CONFIG_UC_APP_MAX_FILE_SIZE) {
		return -EFBIG;
	}
	if (size < 8) {
		return -EILSEQ;
	}

	buf = wasm_runtime_malloc((uint32_t)size);
	if (buf == NULL) {
		return -ENOMEM;
	}

	fs_file_t_init(&file);
	rc = fs_open(&file, path, FS_O_READ);
	if (rc < 0) {
		wasm_runtime_free(buf);
		return rc;
	}
	while (done < size) {
		ssize_t n = fs_read(&file, buf + done, size - done);

		if (n < 0) {
			rc = (int)n;
			break;
		}
		if (n == 0) {
			rc = -EIO; /* file shrank */
			break;
		}
		done += (size_t)n;
	}
	(void)fs_close(&file);

	if (rc < 0) {
		wasm_runtime_free(buf);
		return rc;
	}

	*image = buf;
	*image_len = (uint32_t)size;
	return 0;
}

/* ---------------------------------------------------------------------- */
/* Instance lifecycle (app thread)                                         */
/* ---------------------------------------------------------------------- */

/* WAMR error text without its "WASM module ... failed: " prefix. */
static const char *wamr_msg(const char *msg)
{
	const char *p = strstr(msg, "failed: ");

	return (p != NULL) ? p + strlen("failed: ") : msg;
}

static int wamr_errno(const char *msg)
{
	return (strstr(msg, "allocate memory") != NULL) ? -ENOMEM : -EILSEQ;
}

static int check_imports(struct uc_app_slot *s, wasm_module_t module)
{
	int32_t count = wasm_runtime_get_import_count(module);

	for (int32_t i = 0; i < count; i++) {
		wasm_import_t imp;

		memset(&imp, 0, sizeof(imp));
		wasm_runtime_get_import_type(module, i, &imp);
		if ((imp.kind == WASM_IMPORT_EXPORT_KIND_FUNC) && !imp.linked) {
			slot_error(s, "unresolved import %s.%s",
				   (imp.module_name != NULL) ? imp.module_name : "?",
				   (imp.name != NULL) ? imp.name : "?");
			return -EILSEQ;
		}
	}

	return 0;
}

static bool export_sig_ok(wasm_module_inst_t inst, wasm_function_inst_t f,
			  const struct export_sig *sig)
{
	wasm_valkind_t types[ARRAY_SIZE(sig->params)];
	uint32_t np = wasm_func_get_param_count(f, inst);
	uint32_t nr = wasm_func_get_result_count(f, inst);

	if ((np != sig->n_params) || (nr != sig->n_results)) {
		return false;
	}
	if (np > 0U) {
		wasm_func_get_param_types(f, inst, types);
		if (memcmp(types, sig->params, np * sizeof(types[0])) != 0) {
			return false;
		}
	}
	if (nr > 0U) {
		wasm_func_get_result_types(f, inst, types);
		if (types[0] != sig->result) {
			return false;
		}
	}

	return true;
}

static int check_exports(struct uc_app_slot *s, struct app_rt *rt)
{
	for (int i = 0; i < EXP_COUNT; i++) {
		const struct export_sig *sig = &export_sigs[i];
		wasm_function_inst_t f = wasm_runtime_lookup_function(rt->inst, sig->name);

		rt->fn[i] = f;
		if ((f != NULL) && !export_sig_ok(rt->inst, f, sig)) {
			slot_error(s, "export %s: wrong signature", sig->name);
			return -EILSEQ;
		}
	}

	if (rt->fn[EXP_VERSION] == NULL) {
		slot_error(s, "export uc_app_api_version missing (UC_APP_DECLARE)");
		return -EILSEQ;
	}

	return 0;
}

static int app_load(struct uc_app_slot *s, struct app_rt *rt)
{
	char err[APP_ERR_BUF_SIZE];
	uint32_t stack_size = (uint32_t)s->cfg.stack_kb * 1024U;
	uint32_t heap_size = (uint32_t)s->cfg.heap_kb * 1024U;
	int rc;

	if (s->cfg.has_sha256) {
		rc = module_sha256_check(&s->cfg);
		if (rc < 0) {
			slot_error(s, (rc == -EILSEQ) ? "%s: sha256 mismatch" : "%s: sha256 failed",
				   s->cfg.file);
			return rc;
		}
	}

	rc = module_read(s->cfg.file, &rt->image, &rt->image_len);
	if (rc < 0) {
		slot_error(s, "read %s: %s (%d)", s->cfg.file, uc_err_str(rc), rc);
		return rc;
	}
	rc = module_check_header(rt->image);
	if (rc < 0) {
		slot_error(s, "%s: %s", s->cfg.file,
			   (rc == -ENOTSUP) ? "AOT modules not supported" : "not a WASM module");
		return rc;
	}

	err[0] = '\0';
	(void)k_mutex_lock(&wamr_lock, K_FOREVER);
	rt->module = wasm_runtime_load(rt->image, rt->image_len, err, sizeof(err));
	(void)k_mutex_unlock(&wamr_lock);
	if (rt->module == NULL) {
		slot_error(s, "load: %s", wamr_msg(err));
		return wamr_errno(err);
	}

	rc = check_imports(s, rt->module);
	if (rc < 0) {
		return rc;
	}

	err[0] = '\0';
	(void)k_mutex_lock(&wamr_lock, K_FOREVER);
	rt->inst = wasm_runtime_instantiate(rt->module, stack_size, heap_size, err, sizeof(err));
	if (rt->inst != NULL) {
		rt->env = wasm_runtime_create_exec_env(rt->inst, stack_size);
	}
	(void)k_mutex_unlock(&wamr_lock);
	if (rt->inst == NULL) {
		slot_error(s, "instantiate: %s", wamr_msg(err));
		return wamr_errno(err);
	}
	if (rt->env == NULL) {
		slot_error(s, "exec env: out of memory");
		return -ENOMEM;
	}
	wasm_runtime_set_user_data(rt->env, s);

	rc = check_exports(s, rt);
	if (rc < 0) {
		return rc;
	}

	(void)k_mutex_lock(&s->wd_lock, K_FOREVER);
	s->inst = rt->inst;
	(void)k_mutex_unlock(&s->wd_lock);

	return 0;
}

/* Call an export under the watchdog. On failure (trap, watchdog) the
 * instance is unusable and last_error is set. */
static bool app_call(struct uc_app_slot *s, struct app_rt *rt, enum app_export fn,
		     uint32_t n_results, wasm_val_t *results, uint32_t n_args, wasm_val_t *args)
{
	const char *exc;
	bool fired;
	bool ok;

	wd_arm(s);
	ok = wasm_runtime_call_wasm_a(rt->env, rt->fn[fn], n_results, results, n_args, args);
	fired = wd_disarm(s);

	if (ok && !fired) {
		return true;
	}

	if (fired) {
		slot_error(s, "%s: watchdog, callback exceeded %d ms", export_sigs[fn].name,
			   CONFIG_UC_APP_WATCHDOG_MS);
	} else {
		exc = wasm_runtime_get_exception(rt->inst);
		if ((exc != NULL) && (strncmp(exc, "Exception: ", 11) == 0)) {
			exc += 11;
		}
		slot_error(s, "%s: %s", export_sigs[fn].name, (exc != NULL) ? exc : "trap");
	}

	return false;
}

static wasm_val_t val_i32(uint32_t v)
{
	wasm_val_t val = {.kind = WASM_I32, .of.i32 = (int32_t)v};

	return val;
}

static wasm_val_t val_f64(double v)
{
	wasm_val_t val = {.kind = WASM_F64, .of.f64 = v};

	return val;
}

static int app_start_instance(struct uc_app_slot *s, struct app_rt *rt)
{
	wasm_val_t res = {0};
	uint32_t version;

	if (!app_call(s, rt, EXP_VERSION, 1, &res, 0, NULL)) {
		return -EFAULT;
	}
	version = (uint32_t)res.of.i32;
	if ((version >> 16) != UC_API_VERSION_MAJOR) {
		slot_error(s, "API %u.%u not supported (host %u.%u)", version >> 16,
			   version & 0xFFFFU, UC_API_VERSION_MAJOR, UC_API_VERSION_MINOR);
		return -EILSEQ;
	}

	if (rt->fn[EXP_INIT] == NULL) {
		return 0;
	}

	memset(&res, 0, sizeof(res));
	if (!app_call(s, rt, EXP_INIT, 1, &res, 0, NULL)) {
		return -EFAULT;
	}
	if (res.of.i32 != 0) {
		slot_error(s, "uc_app_init returned %d", res.of.i32);
		return -EINVAL;
	}

	return 0;
}

/* Deliver one queued event. Returns false when the instance trapped. */
static bool app_deliver(struct uc_app_slot *s, struct app_rt *rt, const struct uc_app_event *ev)
{
	wasm_val_t args[6];

	switch (ev->kind) {
	case UC_APP_EV_COV:
		if ((rt->fn[EXP_COV] == NULL) || !uc_app_host_sub_live(s, ev)) {
			return true;
		}
		args[0] = val_i32((uint32_t)ev->sub_id);
		args[1] = val_i32(ev->device);
		args[2] = val_i32(ev->type);
		args[3] = val_i32(ev->instance);
		args[4] = val_i32(ev->prop);
		args[5] = val_f64(ev->value);
		if (!app_call(s, rt, EXP_COV, 0, NULL, 6, args)) {
			return false;
		}
		break;
	case UC_APP_EV_WRITE:
		if (rt->fn[EXP_WRITE] == NULL) {
			return true;
		}
		args[0] = val_i32(ev->type);
		args[1] = val_i32(ev->instance);
		args[2] = val_i32(ev->prop);
		args[3] = val_i32(ev->priority);
		args[4] = val_f64(ev->value);
		if (!app_call(s, rt, EXP_WRITE, 0, NULL, 5, args)) {
			return false;
		}
		break;
	default:
		/* UC_APP_EV_STOP only wakes the loop */
		return true;
	}

	atomic_inc(&s->events);
	return true;
}

/* Tick / event loop. Returns true on a stop request, false on a trap. */
static bool app_loop(struct uc_app_slot *s, struct app_rt *rt)
{
	struct uc_app_event ev;
	int64_t next_tick = 0;

	s->period_changed = true;

	for (;;) {
		k_timeout_t timeout = K_FOREVER;
		int64_t now;

		if (atomic_get(&s->stop_req)) {
			return true;
		}

		now = k_uptime_get();
		if (s->period_changed) {
			s->period_changed = false;
			next_tick = now + s->period_ms;
		}

		if ((rt->fn[EXP_TICK] != NULL) && (s->period_ms != 0U)) {
			if (now >= next_tick) {
				wasm_val_t arg = {.kind = WASM_I64, .of.i64 = now};

				if (!app_call(s, rt, EXP_TICK, 0, NULL, 1, &arg)) {
					return false;
				}
				atomic_inc(&s->ticks);
				if (!s->period_changed) {
					next_tick += s->period_ms;
					now = k_uptime_get();
					if (next_tick <= now) {
						/* overrun: skip the missed ticks */
						next_tick = now + s->period_ms;
					}
				}
				continue;
			}
			timeout = K_MSEC(next_tick - now);
		}

		if (k_msgq_get(&s->msgq, &ev, timeout) == 0) {
			if (!app_deliver(s, rt, &ev)) {
				return false;
			}
		}
	}
}

static void app_teardown(struct uc_app_slot *s, struct app_rt *rt)
{
	int rc = 0;

	atomic_clear(&s->accept);

	/* no COV callback runs for this slot once this returns */
	uc_app_host_cleanup(s);

	/* synchronous through the BACnet thread: no write hook in flight
	 * afterwards either */
	for (int i = 0; i < APP_EXEC_RETRIES; i++) {
		rc = uc_bn_obj_delete_owned(s->owner);
		if (rc != -EAGAIN) {
			break;
		}
		k_msleep(APP_EXEC_RETRY_MS);
	}
	if (rc < 0) {
		LOG_WRN("%s: deleting owned objects failed (%d)", s->cfg.name, rc);
	} else if (rc > 0) {
		LOG_INF("%s: %d objects deleted", s->cfg.name, rc);
	}

	k_msgq_purge(&s->msgq);

	(void)k_mutex_lock(&s->wd_lock, K_FOREVER);
	s->inst = NULL;
	(void)k_mutex_unlock(&s->wd_lock);

	(void)k_mutex_lock(&wamr_lock, K_FOREVER);
	if (rt->env != NULL) {
		wasm_runtime_destroy_exec_env(rt->env);
	}
	if (rt->inst != NULL) {
		wasm_runtime_deinstantiate(rt->inst);
	}
	if (rt->module != NULL) {
		wasm_runtime_unload(rt->module);
	}
	if (rt->image != NULL) {
		wasm_runtime_free(rt->image);
	}
	(void)k_mutex_unlock(&wamr_lock);

	memset(rt, 0, sizeof(*rt));
}

/* End of a run: final state, wake a manager waiting for the start. */
static void app_finish(struct uc_app_slot *s, enum uc_app_state state, int rc)
{
	k_spinlock_key_t key = k_spin_lock(&state_lock);

	s->state = state;
	s->fail_rc = (state == UC_APP_FAILED) ? rc : 0;
	k_spin_unlock(&state_lock, key);

	k_sem_give(&s->started_sem);
	LOG_INF("%s: %s", s->cfg.name, uc_app_state_str(state));
}

static void app_run(struct uc_app_slot *s)
{
	struct app_rt rt;
	bool stopped;
	int rc;

	memset(&rt, 0, sizeof(rt));
	uc_app_host_reset(s);
	s->period_ms = (s->cfg.period_ms == 0U) ? 0U : MAX(s->cfg.period_ms, UC_APP_PERIOD_MIN_MS);
	s->period_changed = true;

	while (!uc_bn_ready()) {
		if (atomic_get(&s->stop_req)) {
			app_finish(s, UC_APP_STOPPED, 0);
			return;
		}
		(void)uc_bn_wait_ready(K_MSEC(APP_BN_WAIT_MS));
	}
	if (atomic_get(&s->stop_req)) {
		app_finish(s, UC_APP_STOPPED, 0);
		return;
	}

	/* events raised by uc_app_init() are delivered right after it */
	atomic_set(&s->accept, 1);

	rc = app_load(s, &rt);
	if (rc == 0) {
		rc = app_start_instance(s, &rt);
	}
	if (rc < 0) {
		app_teardown(s, &rt);
		app_finish(s, UC_APP_FAILED, rc);
		return;
	}

	slot_set_state(s, UC_APP_RUNNING);
	k_sem_give(&s->started_sem);
	LOG_INF("%s: running %s (period %u ms)", s->cfg.name, s->cfg.file, s->period_ms);

	stopped = app_loop(s, &rt);
	if (stopped && (rt.fn[EXP_DEINIT] != NULL)) {
		/* a trap here is recorded in last_error; the app stops anyway */
		(void)app_call(s, &rt, EXP_DEINIT, 0, NULL, 0, NULL);
	}

	app_teardown(s, &rt);
	app_finish(s, stopped ? UC_APP_STOPPED : UC_APP_FAILED, stopped ? 0 : -EFAULT);
}

static void app_thread(void *p1, void *p2, void *p3)
{
	struct uc_app_slot *s = p1;

	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	if (!wasm_runtime_init_thread_env()) {
		LOG_ERR("slot %u: WAMR thread environment failed", s->index);
	}

	for (;;) {
		(void)k_sem_take(&s->run_sem, K_FOREVER);
		app_run(s);
		atomic_clear(&s->busy);
		k_sem_give(&s->done_sem);
	}
}

/* ---------------------------------------------------------------------- */
/* Manager internals (mgr_lock held)                                       */
/* ---------------------------------------------------------------------- */

static struct uc_app_slot *slot_find(const char *name)
{
	for (size_t i = 0; i < ARRAY_SIZE(slots); i++) {
		if (slots[i].used && (strcmp(slots[i].cfg.name, name) == 0)) {
			return &slots[i];
		}
	}

	return NULL;
}

static struct uc_app_slot *slot_free_get(void)
{
	for (size_t i = 0; i < ARRAY_SIZE(slots); i++) {
		if (!slots[i].used && !atomic_get(&slots[i].busy)) {
			return &slots[i];
		}
	}

	return NULL;
}

static struct uc_app_slot *slot_bind(const struct uc_app_cfg *cfg)
{
	struct uc_app_slot *s = slot_free_get();

	if (s != NULL) {
		s->used = true;
		memcpy(&s->cfg, cfg, sizeof(s->cfg));
		slot_reset_status(s);
	}

	return s;
}

static bool slot_active(struct uc_app_slot *s)
{
	return atomic_get(&s->busy) != 0;
}

/* Start a run. Waits up to APP_START_WAIT_MS for its outcome when the
 * BACnet node is up; returns the errno of a failed start, 0 when running
 * or still starting. */
static int slot_start(struct uc_app_slot *s)
{
	enum uc_app_state state;
	k_spinlock_key_t key;
	int rc;

	if (!wamr_ready) {
		return -ENOMEM;
	}
	if (slot_active(s)) {
		state = slot_state(s);
		return ((state == UC_APP_STARTING) || (state == UC_APP_RUNNING)) ? -EALREADY
										  : -EBUSY;
	}

	k_msgq_purge(&s->msgq);
	k_sem_reset(&s->started_sem);
	atomic_clear(&s->stop_req);
	slot_reset_status(s);
	slot_set_state(s, UC_APP_STARTING);
	atomic_set(&s->busy, 1);
	k_sem_give(&s->run_sem);

	if (!uc_bn_ready()) {
		/* the app thread waits for the BACnet node */
		return 0;
	}
	if (k_sem_take(&s->started_sem, K_MSEC(APP_START_WAIT_MS)) != 0) {
		return 0;
	}

	key = k_spin_lock(&state_lock);
	rc = (s->state == UC_APP_FAILED) ? s->fail_rc : 0;
	k_spin_unlock(&state_lock, key);

	return (rc < 0) ? rc : 0;
}

/* Request a stop and wait for the app thread to clean up. */
static int slot_stop(struct uc_app_slot *s)
{
	struct uc_app_event ev = {.kind = UC_APP_EV_STOP};
	k_spinlock_key_t key;
	int64_t end;

	if (!slot_active(s)) {
		key = k_spin_lock(&state_lock);
		if (s->state == UC_APP_FAILED) {
			s->state = UC_APP_STOPPED;
		}
		k_spin_unlock(&state_lock, key);
		return 0;
	}

	atomic_set(&s->stop_req, 1);
	(void)k_msgq_put(&s->msgq, &ev, K_NO_WAIT);

	end = k_uptime_get() + APP_STOP_WAIT_MS;
	while (slot_active(s)) {
		int64_t left = end - k_uptime_get();

		if (left <= 0) {
			LOG_WRN("%s: not stopped after %d ms", s->cfg.name, APP_STOP_WAIT_MS);
			return -EBUSY;
		}
		(void)k_sem_take(&s->done_sem, K_MSEC(left));
	}

	return 0;
}

/* Snapshot of the installed list (k_malloc'ed) for uc_config_set_apps(). */
static struct uc_apps_cfg *list_build(const struct uc_app_slot *skip)
{
	struct uc_apps_cfg *list = k_malloc(sizeof(*list));

	if (list == NULL) {
		return NULL;
	}
	memset(list, 0, sizeof(*list));
	for (size_t i = 0; i < n_installed; i++) {
		const struct uc_app_slot *s = &slots[order[i]];

		if (s == skip) {
			continue;
		}
		memcpy(&list->apps[list->count++], &s->cfg, sizeof(s->cfg));
	}

	return list;
}

static const struct uc_app_cfg *list_find(const struct uc_apps_cfg *list, const char *name)
{
	for (size_t i = 0; i < list->count; i++) {
		if (strcmp(list->apps[i].name, name) == 0) {
			return &list->apps[i];
		}
	}

	return NULL;
}

static bool cfg_equal(const struct uc_app_cfg *a, const struct uc_app_cfg *b)
{
	if ((strcmp(a->name, b->name) != 0) || (strcmp(a->file, b->file) != 0) ||
	    (a->autostart != b->autostart) || (a->period_ms != b->period_ms) ||
	    (a->heap_kb != b->heap_kb) || (a->stack_kb != b->stack_kb) ||
	    (a->perms != b->perms) || (a->param_count != b->param_count) ||
	    (a->has_sha256 != b->has_sha256)) {
		return false;
	}
	if (a->has_sha256 && (memcmp(a->sha256, b->sha256, sizeof(a->sha256)) != 0)) {
		return false;
	}
	for (size_t i = 0; (i < a->param_count) && (i < CONFIG_UC_APP_PARAMS_MAX); i++) {
		if ((strcmp(a->params[i].key, b->params[i].key) != 0) ||
		    (strcmp(a->params[i].value, b->params[i].value) != 0)) {
			return false;
		}
	}

	return true;
}

/* strnlen() without relying on POSIX declarations */
static size_t str_len_max(const char *s, size_t max)
{
	const char *end = memchr(s, '\0', max);

	return (end != NULL) ? (size_t)(end - s) : max;
}

static bool file_char_ok(char c)
{
	return isalnum((unsigned char)c) || (c == '_') || (c == '.') || (c == '-');
}

/* "/lfs/apps/<stem>.wasm" or ".aot", stem 1..40 of [A-Za-z0-9_.-]. */
static bool app_file_ok(const char *file)
{
	static const char prefix[] = UC_DIR_APPS "/";
	static const char *const suffixes[] = {".wasm", ".aot"};
	const size_t plen = sizeof(prefix) - 1;
	size_t len = str_len_max(file, UC_PATH_MAX);

	if ((len >= UC_PATH_MAX) || (len <= plen) || (strncmp(file, prefix, plen) != 0)) {
		return false;
	}
	for (size_t i = plen; i < len; i++) {
		if (!file_char_ok(file[i])) {
			return false;
		}
	}
	for (size_t i = 0; i < ARRAY_SIZE(suffixes); i++) {
		size_t slen = strlen(suffixes[i]);

		if ((len > plen + slen) && (len - plen - slen <= 40U) &&
		    (strcmp(file + len - slen, suffixes[i]) == 0)) {
			return true;
		}
	}

	return false;
}

static bool str_terminated(const char *s, size_t size)
{
	return str_len_max(s, size) < size;
}

/* Manifest checks before any file access (uc_config_set_apps() repeats
 * the schema checks). */
static int app_cfg_check(const struct uc_app_cfg *cfg)
{
	const uint32_t all_perms =
		UC_PERM_BACNET_LOCAL | UC_PERM_BACNET_REMOTE | UC_PERM_IO | UC_PERM_KV;

	if (!str_terminated(cfg->name, sizeof(cfg->name)) || !uc_app_name_valid(cfg->name) ||
	    !str_terminated(cfg->file, sizeof(cfg->file)) || !app_file_ok(cfg->file)) {
		return -EINVAL;
	}
	if ((cfg->period_ms > UC_APP_PERIOD_MAX_MS) || (cfg->heap_kb > 256U) ||
	    (cfg->stack_kb < 1U) || (cfg->stack_kb > 64U) || ((cfg->perms & ~all_perms) != 0U) ||
	    (cfg->param_count > CONFIG_UC_APP_PARAMS_MAX)) {
		return -EINVAL;
	}
	for (size_t i = 0; i < cfg->param_count; i++) {
		const struct uc_app_param *p = &cfg->params[i];

		if (!str_terminated(p->key, sizeof(p->key)) ||
		    !uc_key_valid(p->key, sizeof(p->key) - 1) ||
		    !str_terminated(p->value, sizeof(p->value))) {
			return -EINVAL;
		}
	}

	return 0;
}

static void order_remove(uint8_t index)
{
	for (size_t i = 0; i < n_installed; i++) {
		if (order[i] != index) {
			continue;
		}
		memmove(&order[i], &order[i + 1], (n_installed - i - 1) * sizeof(order[0]));
		n_installed--;
		return;
	}
}

static void slot_fill_status(struct uc_app_slot *s, struct uc_app_status *out)
{
	k_spinlock_key_t key;

	memcpy(&out->cfg, &s->cfg, sizeof(out->cfg));

	key = k_spin_lock(&state_lock);
	out->state = s->state;
	memcpy(out->last_error, s->last_error, sizeof(out->last_error));
	out->uptime_ms =
		(s->state == UC_APP_RUNNING) ? (uint64_t)(k_uptime_get() - s->start_ms) : 0U;
	k_spin_unlock(&state_lock, key);

	out->ticks = (uint32_t)atomic_get(&s->ticks);
	out->events = (uint32_t)atomic_get(&s->events);
	out->errors = (uint32_t)atomic_get(&s->errors);
}

/* ---------------------------------------------------------------------- */
/* Public API                                                              */
/* ---------------------------------------------------------------------- */

static void aot_target_init(void)
{
#if defined(CONFIG_WAMR_AOT)
	const char *t = CONFIG_WAMR_BUILD_TARGET;
	size_t n = strlen(t);

	/* "_VFP" selects the calling convention, not the wamrc target */
	if ((n > 4) && (strcmp(t + n - 4, "_VFP") == 0)) {
		n -= 4;
	}
	n = MIN(n, sizeof(aot_target) - 1);
	for (size_t i = 0; i < n; i++) {
		aot_target[i] = (char)tolower((unsigned char)t[i]);
	}
	aot_target[n] = '\0';
#else
	aot_target[0] = '\0';
#endif
}

static void slot_init(struct uc_app_slot *s, uint8_t index)
{
	char name[16];

	memset(s, 0, sizeof(*s));
	s->index = index;
	s->owner = (uint8_t)(UC_OWNER_APP_BASE + index);
	s->state = UC_APP_STOPPED;

	k_sem_init(&s->run_sem, 0, 1);
	k_sem_init(&s->done_sem, 0, 1);
	k_sem_init(&s->started_sem, 0, 1);
	k_msgq_init(&s->msgq, (char *)s->msgq_buf, sizeof(struct uc_app_event),
		    ARRAY_SIZE(s->msgq_buf));
	k_timer_init(&s->wd_timer, wd_expiry, NULL);
	k_work_init(&s->wd_work, wd_work_fn);
	k_mutex_init(&s->wd_lock);

	(void)k_thread_create(&s->thread, app_stacks[index],
			      K_THREAD_STACK_SIZEOF(app_stacks[index]), app_thread, s, NULL, NULL,
			      CONFIG_UC_APP_THREAD_PRIORITY, K_FP_REGS, K_NO_WAIT);
	(void)snprintk(name, sizeof(name), "uc_app%u", index);
	(void)k_thread_name_set(&s->thread, name);
}

int uc_apps_init(void)
{
	struct uc_apps_cfg *installed;
	RuntimeInitArgs args;
	uint32_t n_natives = 0;
	int rc = 0;

	if (mgr_ready) {
		return -EALREADY;
	}

	memset(&args, 0, sizeof(args));
	args.mem_alloc_type = Alloc_With_Pool;
	args.mem_alloc_option.pool.heap_buf = wamr_pool;
	args.mem_alloc_option.pool.heap_size = sizeof(wamr_pool);
	args.native_module_name = "bacnet_uc";
	args.native_symbols = uc_app_host_natives(&n_natives);
	args.n_native_symbols = n_natives;
	args.max_thread_num = 1;
	wamr_ready = wasm_runtime_full_init(&args);
	if (!wamr_ready) {
		LOG_ERR("WAMR init failed (pool %u bytes)", (unsigned int)sizeof(wamr_pool));
		rc = -ENOMEM;
	}

	aot_target_init();
	for (size_t i = 0; i < ARRAY_SIZE(slots); i++) {
		slot_init(&slots[i], (uint8_t)i);
	}
	uc_bn_set_write_hook(app_write_hook);

	installed = k_malloc(sizeof(*installed));

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	mgr_ready = true;
	if (installed == NULL) {
		LOG_ERR("apps.json: out of memory");
		rc = -ENOMEM;
	} else {
		uc_config_get_apps(installed);
		for (size_t i = 0; (i < installed->count) && (i < ARRAY_SIZE(slots)); i++) {
			struct uc_app_slot *s = slot_bind(&installed->apps[i]);

			if (s != NULL) {
				order[n_installed++] = s->index;
			}
		}
		for (size_t i = 0; i < n_installed; i++) {
			struct uc_app_slot *s = &slots[order[i]];
			int err;

			if (!s->cfg.autostart) {
				continue;
			}
			err = slot_start(s);
			if (err < 0) {
				LOG_ERR("%s: start failed (%d)", s->cfg.name, err);
			}
		}
	}
	(void)k_mutex_unlock(&mgr_lock);
	k_free(installed);

	LOG_INF("WAMR %s interpreter%s, pool %u bytes (%s), %u apps installed",
		IS_ENABLED(CONFIG_WAMR_FAST_INTERP) ? "fast" : "classic",
		IS_ENABLED(CONFIG_WAMR_AOT) ? " + AOT" : "", (unsigned int)sizeof(wamr_pool),
		APP_POOL_REGION, (unsigned int)n_installed);

	return rc;
}

int uc_apps_install(const struct uc_app_cfg *cfg, bool restart)
{
	struct uc_apps_cfg *list;
	struct uc_app_slot *s;
	size_t pos;
	int rc;

	if (cfg == NULL) {
		return -EINVAL;
	}
	if (!mgr_ready) {
		return -EAGAIN;
	}

	rc = app_cfg_check(cfg);
	if (rc < 0) {
		return rc;
	}
	rc = module_verify(cfg);
	if (rc < 0) {
		LOG_WRN("install %s: %s: %s (%d)", cfg->name, cfg->file, uc_err_str(rc), rc);
		return rc;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);

	s = slot_find(cfg->name);
	if ((s != NULL) && slot_active(s)) {
		if (!restart) {
			rc = -EALREADY;
			goto out;
		}
		rc = slot_stop(s);
		if (rc < 0) {
			goto out;
		}
	}
	if ((s == NULL) && ((n_installed >= ARRAY_SIZE(slots)) || (slot_free_get() == NULL))) {
		rc = -ENOSPC;
		goto out;
	}

	list = list_build(NULL);
	if (list == NULL) {
		rc = -ENOMEM;
		goto out;
	}
	pos = list->count;
	for (size_t i = 0; i < list->count; i++) {
		if (strcmp(list->apps[i].name, cfg->name) == 0) {
			pos = i;
			break;
		}
	}
	memcpy(&list->apps[pos], cfg, sizeof(*cfg));
	if (pos == list->count) {
		list->count++;
	}
	rc = uc_config_set_apps(list);
	k_free(list);
	if (rc < 0) {
		LOG_ERR("install %s: apps.json not written (%d)", cfg->name, rc);
		goto out;
	}

	if (s == NULL) {
		/* a free slot was checked above under the same lock */
		s = slot_bind(cfg);
		if (s == NULL) {
			rc = -ENOSPC;
			goto out;
		}
		order[n_installed++] = s->index;
	} else {
		memcpy(&s->cfg, cfg, sizeof(s->cfg));
		slot_reset_status(s);
	}
	LOG_INF("installed %s (%s)", cfg->name, cfg->file);

	if (cfg->autostart) {
		rc = slot_start(s);
	}

out:
	(void)k_mutex_unlock(&mgr_lock);
	return rc;
}

int uc_apps_start(const char *name)
{
	struct uc_app_slot *s;
	int rc;

	if (name == NULL) {
		return -EINVAL;
	}
	if (!mgr_ready) {
		return -EAGAIN;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	s = slot_find(name);
	rc = (s != NULL) ? slot_start(s) : -ENOENT;
	(void)k_mutex_unlock(&mgr_lock);

	return rc;
}

int uc_apps_stop(const char *name)
{
	struct uc_app_slot *s;
	int rc;

	if (name == NULL) {
		return -EINVAL;
	}
	if (!mgr_ready) {
		return -EAGAIN;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	s = slot_find(name);
	rc = (s != NULL) ? slot_stop(s) : -ENOENT;
	(void)k_mutex_unlock(&mgr_lock);

	return rc;
}

int uc_apps_remove(const char *name, bool delete_file)
{
	char app_name[UC_APP_NAME_MAX];
	char file[UC_PATH_MAX];
	struct uc_apps_cfg *list;
	struct uc_app_slot *s;
	bool shared = false;
	int rc;

	if (name == NULL) {
		return -EINVAL;
	}
	if (!mgr_ready) {
		return -EAGAIN;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);

	s = slot_find(name);
	if (s == NULL) {
		rc = -ENOENT;
		goto out;
	}
	rc = slot_stop(s);
	if (rc < 0) {
		goto out;
	}

	list = list_build(s);
	if (list == NULL) {
		rc = -ENOMEM;
		goto out;
	}
	rc = uc_config_set_apps(list);
	k_free(list);
	if (rc < 0) {
		LOG_ERR("remove %s: apps.json not written (%d)", name, rc);
		goto out;
	}

	(void)uc_strlcpy(app_name, s->cfg.name, sizeof(app_name));
	(void)uc_strlcpy(file, s->cfg.file, sizeof(file));
	order_remove(s->index);
	s->used = false;
	slot_reset_status(s);
	LOG_INF("removed %s", app_name);

	if (delete_file) {
		int err;

		/* several instances may share one module file (uc-link) */
		for (size_t i = 0; i < n_installed; i++) {
			if (strcmp(slots[order[i]].cfg.file, file) == 0) {
				shared = true;
			}
		}
		if (shared) {
			LOG_INF("%s still used by another app, not deleted", file);
		} else {
			err = uc_storage_remove(file);
			if (err < 0) {
				LOG_WRN("delete %s failed (%d)", file, err);
				rc = err;
			}
		}
		err = uc_app_kv_remove_all(app_name);
		if (err < 0) {
			LOG_WRN("delete %s/%s failed (%d)", UC_DIR_DATA, app_name, err);
			if (rc == 0) {
				rc = err;
			}
		}
	}

out:
	(void)k_mutex_unlock(&mgr_lock);
	return rc;
}

int uc_apps_reload(void)
{
	struct uc_apps_cfg *next;
	int first_err = 0;
	int rc;

	if (!mgr_ready) {
		return -EAGAIN;
	}

	rc = uc_config_reload(UC_CFG_APPS);
	if (rc < 0) {
		return rc;
	}
	next = k_malloc(sizeof(*next));
	if (next == NULL) {
		return -ENOMEM;
	}
	uc_config_get_apps(next);

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);

	/* stop apps that were removed or whose entry changed */
	for (size_t i = 0; i < ARRAY_SIZE(slots); i++) {
		struct uc_app_slot *s = &slots[i];
		const struct uc_app_cfg *n;

		if (!s->used) {
			continue;
		}
		n = list_find(next, s->cfg.name);
		if ((n != NULL) && cfg_equal(&s->cfg, n)) {
			continue;
		}
		rc = slot_stop(s);
		if (rc < 0) {
			/* keeps its old entry until it stops */
			first_err = (first_err != 0) ? first_err : rc;
			continue;
		}
		if (n == NULL) {
			LOG_INF("%s removed from apps.json", s->cfg.name);
			s->used = false;
		} else {
			LOG_INF("%s changed in apps.json", s->cfg.name);
			memcpy(&s->cfg, n, sizeof(s->cfg));
		}
		slot_reset_status(s);
	}

	/* new order, bind new apps */
	n_installed = 0;
	for (size_t i = 0; i < next->count; i++) {
		struct uc_app_slot *s = slot_find(next->apps[i].name);

		if (s == NULL) {
			s = slot_bind(&next->apps[i]);
		}
		if (s == NULL) {
			first_err = (first_err != 0) ? first_err : -ENOSPC;
			continue;
		}
		order[n_installed++] = s->index;
	}

	/* start autostart apps that are not running */
	for (size_t i = 0; i < n_installed; i++) {
		struct uc_app_slot *s = &slots[order[i]];

		if (!s->cfg.autostart || slot_active(s)) {
			continue;
		}
		rc = slot_start(s);
		if (rc < 0) {
			LOG_ERR("%s: start failed (%d)", s->cfg.name, rc);
			first_err = (first_err != 0) ? first_err : rc;
		}
	}

	(void)k_mutex_unlock(&mgr_lock);
	k_free(next);

	return first_err;
}

size_t uc_apps_installed(void)
{
	size_t n;

	if (!mgr_ready) {
		return 0;
	}
	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	n = n_installed;
	(void)k_mutex_unlock(&mgr_lock);

	return n;
}

size_t uc_apps_running(void)
{
	size_t n = 0;

	for (size_t i = 0; i < ARRAY_SIZE(slots); i++) {
		if (slot_state(&slots[i]) == UC_APP_RUNNING) {
			n++;
		}
	}

	return n;
}

int uc_apps_status(size_t index, struct uc_app_status *out)
{
	int rc = 0;

	if (out == NULL) {
		return -EINVAL;
	}
	if (!mgr_ready) {
		return -ENOENT;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	if (index < n_installed) {
		slot_fill_status(&slots[order[index]], out);
	} else {
		rc = -ENOENT;
	}
	(void)k_mutex_unlock(&mgr_lock);

	return rc;
}

int uc_apps_status_by_name(const char *name, struct uc_app_status *out)
{
	struct uc_app_slot *s;
	int rc = 0;

	if ((name == NULL) || (out == NULL)) {
		return -EINVAL;
	}
	if (!mgr_ready) {
		return -ENOENT;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	s = slot_find(name);
	if (s != NULL) {
		slot_fill_status(s, out);
	} else {
		rc = -ENOENT;
	}
	(void)k_mutex_unlock(&mgr_lock);

	return rc;
}

int uc_apps_owner_name(uint8_t owner, char *buf, size_t size)
{
	int rc = -ENOENT;

	if ((buf == NULL) || (size == 0U)) {
		return -EINVAL;
	}
	buf[0] = '\0';
	if (!mgr_ready || !UC_OWNER_IS_APP(owner) ||
	    (UC_OWNER_APP_SLOT(owner) >= CONFIG_UC_APPS_MAX)) {
		return -ENOENT;
	}

	(void)k_mutex_lock(&mgr_lock, K_FOREVER);
	if (slots[UC_OWNER_APP_SLOT(owner)].used) {
		rc = uc_strlcpy(buf, slots[UC_OWNER_APP_SLOT(owner)].cfg.name, size);
	}
	(void)k_mutex_unlock(&mgr_lock);

	return rc;
}

void uc_apps_wasm_info(struct uc_wasm_info *out)
{
	mem_alloc_info_t mi;

	if (out == NULL) {
		return;
	}

	memset(out, 0, sizeof(*out));
	out->interp = true;
	out->aot = IS_ENABLED(CONFIG_WAMR_AOT);
	out->aot_target = aot_target;
	out->pool_total = sizeof(wamr_pool);

	if (wamr_ready && wasm_runtime_get_mem_alloc_info(&mi)) {
		out->pool_total = mi.total_size;
		/* WAMR 2.4.5 (ems_alloc.c, gc_realloc_vo) does not subtract a
		 * block grown in place from total_free_size, so the figure
		 * drifts upwards over module loads; never report more than the
		 * pool. */
		out->pool_free = MIN(mi.total_free_size, mi.total_size);
	}
}
