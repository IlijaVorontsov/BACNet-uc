/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Network readiness: the MQTT session is only attempted while the default
 * interface is up with an IPv4 address, and is torn down when that changes.
 */

#include <errno.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/net_event.h>
#include <zephyr/net/dhcpv4.h>
#if defined(CONFIG_NET_CONNECTION_MANAGER)
#include <zephyr/net/conn_mgr_monitor.h>
#endif

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

#define NET_UP_BIT BIT(0)

static K_EVENT_DEFINE(net_state);

#if defined(CONFIG_APP_WAIT_FOR_NETWORK)

#define L4_EVENT_MASK (NET_EVENT_L4_CONNECTED | NET_EVENT_L4_DISCONNECTED)

static struct net_mgmt_event_callback l4_cb;

static void log_ipv4_address(struct net_if *iface)
{
	char buf[NET_IPV4_ADDR_LEN];
	struct in_addr *addr;

	addr = net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED);
	if (addr != NULL) {
		LOG_INF("IPv4 address: %s",
			net_addr_ntop(AF_INET, addr, buf, sizeof(buf)));
	}
}

static void l4_event_handler(struct net_mgmt_event_callback *cb,
			     uint64_t mgmt_event, struct net_if *iface)
{
	ARG_UNUSED(cb);

	switch (mgmt_event) {
	case NET_EVENT_L4_CONNECTED:
		LOG_INF("Network connected");
		log_ipv4_address(iface);
		k_event_post(&net_state, NET_UP_BIT);
		break;
	case NET_EVENT_L4_DISCONNECTED:
		LOG_WRN("Network disconnected");
		k_event_clear(&net_state, NET_UP_BIT);
		break;
	default:
		break;
	}
}

#endif /* CONFIG_APP_WAIT_FOR_NETWORK */

void app_net_init(void)
{
#if defined(CONFIG_APP_WAIT_FOR_NETWORK)
	net_mgmt_init_event_callback(&l4_cb, l4_event_handler, L4_EVENT_MASK);
	net_mgmt_add_event_callback(&l4_cb);
	/* The interface may already be connected (e.g. static address set
	 * by net_config before main): replay the current state.
	 */
	conn_mgr_mon_resend_status();
#else
	/* No connectivity monitoring (e.g. native_sim offloaded sockets,
	 * which use the host's network directly): assume the network is up.
	 */
	k_event_post(&net_state, NET_UP_BIT);
#endif

#if defined(CONFIG_APP_DHCPV4)
	struct net_if *iface = net_if_get_default();

	if (iface != NULL) {
		/* DHCPv4 follows interface up/down itself, so starting it once
		 * also covers a cable that is plugged in later.
		 */
		LOG_INF("Starting DHCPv4");
		net_dhcpv4_start(iface);
	} else {
		LOG_ERR("No network interface");
	}
#endif
}

bool app_net_is_up(void)
{
	return k_event_test(&net_state, NET_UP_BIT) != 0;
}

int app_net_wait_up(k_timeout_t timeout)
{
	if (k_event_wait(&net_state, NET_UP_BIT, false, timeout) == 0) {
		return -EAGAIN;
	}

	return 0;
}
