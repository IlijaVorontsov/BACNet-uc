/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Makes the file system log backend commit every batch of log messages,
 * including a batch of one.
 *
 * Zephyr's FS backend (subsys/logging/backends/log_backend_fs.c) calls
 * fs_sync() only on LOG_BACKEND_EVT_PROCESS_THREAD_DONE. The log processing
 * thread (log_core.c, log_process_thread_func) raises that event only after
 * a log_process() call returned true, i.e. when more messages were pending
 * after the one just processed. A lone message therefore never triggers a
 * sync: on a quiet node the newest line stays uncommitted in LittleFS
 * (invisible to SMP downloads, lost on power loss) until a later burst.
 *
 * This backend does nothing but send that event to the FS backend when the
 * message queue has drained. Backends are dispatched in the linker's
 * SORT_BY_NAME order of their section entries, and "log_backend_fs_uc_sync"
 * sorts right after "log_backend_fs", so this runs in the logging thread
 * after the FS backend has written the message: no locking against the FS
 * backend is needed and a burst still costs a single fs_sync().
 */

#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_ctrl.h>

static const struct log_backend *fs_backend;

static void uc_log_sync_process(const struct log_backend *const backend,
				union log_msg_generic *msg)
{
	ARG_UNUSED(backend);
	ARG_UNUSED(msg);

	if (fs_backend == NULL) {
		fs_backend = log_backend_get_by_name("log_backend_fs");
		if (fs_backend == NULL) {
			return;
		}
	}

	if (!log_data_pending() && log_backend_is_active(fs_backend)) {
		log_backend_notify(fs_backend, LOG_BACKEND_EVT_PROCESS_THREAD_DONE, NULL);
	}
}

static const struct log_backend_api uc_log_sync_api = {
	.process = uc_log_sync_process,
};

LOG_BACKEND_DEFINE(log_backend_fs_uc_sync, uc_log_sync_api, true);
