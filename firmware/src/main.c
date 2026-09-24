/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * BACnet-uc boot sequence:
 *
 *   storage (/lfs) -> configuration cache -> log level from device.json ->
 *   network -> IO channels -> BACnet thread -> WebAssembly apps -> SMP
 *   groups -> "ready"
 *
 * Every step logs its result. A failing step does not stop the boot: the
 * node stays reachable through the shell and SMP (and falls back to the
 * Kconfig defaults without storage) so that it can be repaired remotely.
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>

/*
 * The guest ABI header provides UC_API_VERSION. Its guest-side prototypes
 * uc_io_find/uc_io_read/uc_io_write collide with the firmware's uc_io.h, so
 * they are renamed while it is included (only the macros are used here).
 */
#define uc_io_find  uc_guest_io_find
#define uc_io_read  uc_guest_io_read
#define uc_io_write uc_guest_io_write
#include "../../wasm/sdk/include/bacnet_uc.h"
#undef uc_io_find
#undef uc_io_read
#undef uc_io_write

#include "uc/uc_common.h"
#include "uc/uc_storage.h"
#include "uc/uc_config.h"
#include "uc/uc_net.h"
#include "uc/uc_io.h"
#include "uc/uc_bacnet.h"
#if defined(CONFIG_UC_APPS)
#include "uc/uc_apps.h"
#endif
#if defined(CONFIG_UC_MGMT)
#include "uc/uc_mgmt.h"
#endif

LOG_MODULE_REGISTER(uc_main, CONFIG_UC_LOG_LEVEL);

static const char *log_level_name(uint8_t level)
{
	switch (level) {
	case LOG_LEVEL_ERR:
		return "err";
	case LOG_LEVEL_WRN:
		return "wrn";
	case LOG_LEVEL_INF:
		return "inf";
	case LOG_LEVEL_DBG:
		return "dbg";
	default:
		return "?";
	}
}

/* Runtime filter of every log source for every backend. Sources compiled
 * with a lower level keep their compiled level.
 */
static void apply_log_level(uint8_t level)
{
#if defined(CONFIG_LOG_RUNTIME_FILTERING)
	uint32_t count = log_src_cnt_get(Z_LOG_LOCAL_DOMAIN_ID);

	for (uint32_t i = 0; i < count; i++) {
		(void)log_filter_set(NULL, Z_LOG_LOCAL_DOMAIN_ID, (int16_t)i, level);
	}
	LOG_INF("log level %s (%u sources)", log_level_name(level), count);
#else
	LOG_WRN("CONFIG_LOG_RUNTIME_FILTERING disabled, log level %s not applied",
		log_level_name(level));
#endif
}

static void step_result(const char *step, int rc)
{
	if (rc < 0) {
		LOG_ERR("%s failed: %d (%s)", step, rc, uc_err_str(rc));
	} else {
		LOG_INF("%s ok", step);
	}
}

int main(void)
{
	static struct uc_device_cfg dev;
	char ip[16];
	int rc;

	LOG_INF("BACnet-uc %s on %s, WASM API %u.%u", CONFIG_UC_FW_VERSION,
		CONFIG_BOARD_TARGET, UC_API_VERSION_MAJOR, UC_API_VERSION_MINOR);

	rc = uc_storage_init();
	step_result("storage", rc);

	/* defaults are used for missing or rejected documents */
	rc = uc_config_init();
	if (rc < 0) {
		LOG_WRN("configuration: %d (%s), defaults in use for rejected documents", rc,
			uc_err_str(rc));
	} else {
		LOG_INF("configuration ok");
	}

	uc_config_get_device(&dev);
	apply_log_level(dev.log_level);
	LOG_INF("device %u \"%s\", %s, BACnet/IP UDP %u", dev.instance, dev.name,
		dev.dhcp ? "DHCPv4" : dev.ipv4, dev.udp_port);

	rc = uc_net_init(&dev);
	step_result("network", rc);

	rc = uc_io_init();
	step_result("io", rc);

	/* returns immediately; the BACnet thread waits for the network */
	rc = uc_bn_start();
	step_result("bacnet", rc);

#if defined(CONFIG_UC_APPS)
	rc = uc_apps_init();
	step_result("apps", rc);
#endif

#if defined(CONFIG_UC_MGMT)
	rc = uc_mgmt_init();
	step_result("mgmt", rc);
#endif

	rc = uc_net_wait_ready(CONFIG_UC_NET_WAIT_MS);
	uc_net_ipv4_str(ip, sizeof(ip));
	if (rc < 0) {
		LOG_WRN("no IPv4 address after %d ms", CONFIG_UC_NET_WAIT_MS);
	}
#if defined(CONFIG_MCUMGR_TRANSPORT_UDP)
	LOG_INF("ready: device %u, IPv4 %s, BACnet/IP UDP %u, SMP UDP %d", dev.instance, ip,
		dev.udp_port, CONFIG_MCUMGR_TRANSPORT_UDP_PORT);
#else
	LOG_INF("ready: device %u, IPv4 %s, BACnet/IP UDP %u", dev.instance, ip, dev.udp_port);
#endif

	return 0;
}
