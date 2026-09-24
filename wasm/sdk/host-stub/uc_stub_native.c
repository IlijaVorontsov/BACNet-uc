/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Binds the application callbacks linked into a native test executable
 * to the stub. The exports are optional (bacnet_uc.h), so they are weak
 * references: an application that does not define one leaves it NULL,
 * exactly like a missing export of a WebAssembly module.
 */

#include "uc_stub.h"

extern uint32_t uc_app_api_version(void) __attribute__((weak));
extern int32_t uc_app_init(void) __attribute__((weak));
extern void uc_app_tick(uint64_t now_ms) __attribute__((weak));
extern void uc_app_on_cov(int32_t sub_id, uint32_t device, uint32_t type, uint32_t instance,
			  uint32_t prop, double value) __attribute__((weak));
extern void uc_app_on_write(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority,
			    double value) __attribute__((weak));
extern void uc_app_deinit(void) __attribute__((weak));

const struct uc_stub_app *uc_stub_native_app(void)
{
	static struct uc_stub_app app;

	app.api_version = uc_app_api_version;
	app.init = uc_app_init;
	app.tick = uc_app_tick;
	app.on_cov = uc_app_on_cov;
	app.on_write = uc_app_on_write;
	app.deinit = uc_app_deinit;
	app.failed = NULL;
	return &app;
}
