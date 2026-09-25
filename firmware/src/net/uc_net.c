/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * IPv4 bring-up of the default interface from device.json: static address,
 * netmask and gateway, or DHCPv4.
 *
 * native_sim with Native Simulator Offloaded Sockets (NSOS): the offloaded
 * interface has no IPv4 configuration and no DHCP, but the BACnet/IP port
 * (bacnet-stack-zephyr bip-init.c) reads its own address and derives the
 * broadcast address from the interface. The address is only used for
 * bind() on the host, so it must be a host address: the static device.json
 * address when dhcp is false, else 127.0.0.1/8.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#if defined(CONFIG_NET_DHCPV4)
#include <zephyr/net/dhcpv4.h>
#endif

#include "uc/uc_common.h"
#include "uc/uc_net.h"

#if defined(CONFIG_NATIVE_SIM_REBOOT) && defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
#include <posix_native_task.h>
#include "nsi_host_trampolines.h"
#endif

LOG_MODULE_REGISTER(uc_net, CONFIG_UC_LOG_LEVEL);

#define UC_NET_POLL_MS 100
#define UC_NET_DEFAULT_NETMASK "255.255.255.0"

#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
#define UC_NET_NSOS_ADDR    "127.0.0.1"
#define UC_NET_NSOS_NETMASK "255.0.0.0"
#endif

static struct net_if *uc_iface;

static struct net_if *iface_get(void)
{
	return (uc_iface != NULL) ? uc_iface : net_if_get_default();
}

static int set_static(struct net_if *iface, const char *ip, const char *mask, const char *gw)
{
	struct net_in_addr addr;
	struct net_in_addr netmask;
	struct net_in_addr gateway;

	if (ip == NULL || ip[0] == '\0' || net_addr_pton(NET_AF_INET, ip, &addr) < 0) {
		LOG_ERR("invalid IPv4 address '%s'", (ip != NULL) ? ip : "");
		return -EINVAL;
	}
	if (mask == NULL || mask[0] == '\0') {
		mask = UC_NET_DEFAULT_NETMASK;
	}
	if (net_addr_pton(NET_AF_INET, mask, &netmask) < 0) {
		LOG_ERR("invalid netmask '%s'", mask);
		return -EINVAL;
	}

	if (net_if_ipv4_addr_add(iface, &addr, NET_ADDR_MANUAL, 0) == NULL) {
		LOG_ERR("cannot add %s to iface %d", ip, net_if_get_by_iface(iface));
		return -ENOMEM;
	}
	if (!net_if_ipv4_set_netmask_by_addr(iface, &addr, &netmask)) {
		LOG_WRN("cannot set netmask %s", mask);
	}

	if (gw != NULL && gw[0] != '\0') {
		if (net_addr_pton(NET_AF_INET, gw, &gateway) < 0) {
			LOG_WRN("invalid gateway '%s' ignored", gw);
		} else {
			net_if_ipv4_set_gw(iface, &gateway);
		}
	}

	LOG_INF("iface %d: static IPv4 %s/%s gw %s", net_if_get_by_iface(iface), ip, mask,
		(gw != NULL && gw[0] != '\0') ? gw : "-");
	return 0;
}

int uc_net_init(const struct uc_device_cfg *cfg)
{
	struct net_if *iface;

	if (cfg == NULL) {
		return -EINVAL;
	}

	iface = net_if_get_default();
	if (iface == NULL) {
		LOG_ERR("no network interface");
		return -ENODEV;
	}
	uc_iface = iface;

#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
	if (!cfg->dhcp && cfg->ipv4[0] != '\0') {
		return set_static(iface, cfg->ipv4, cfg->netmask, cfg->gateway);
	}
	LOG_INF("NSOS: no DHCP on the host socket interface, using loopback");
	return set_static(iface, UC_NET_NSOS_ADDR, UC_NET_NSOS_NETMASK, NULL);
#else
	if (!cfg->dhcp) {
		return set_static(iface, cfg->ipv4, cfg->netmask, cfg->gateway);
	}
#if defined(CONFIG_NET_DHCPV4)
	LOG_INF("iface %d: starting DHCPv4", net_if_get_by_iface(iface));
	net_dhcpv4_start(iface);
	return 0;
#else
	LOG_ERR("DHCPv4 requested but CONFIG_NET_DHCPV4 is disabled");
	return -ENOTSUP;
#endif
#endif
}

static bool net_ready(struct net_if *iface)
{
	if (iface == NULL || net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED) == NULL) {
		return false;
	}

	/* host sockets do not depend on the interface state */
	if (IS_ENABLED(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)) {
		return true;
	}

	return net_if_is_up(iface);
}

int uc_net_wait_ready(uint32_t timeout_ms)
{
	struct net_if *iface = iface_get();
	int64_t deadline = k_uptime_get() + (int64_t)timeout_ms;

	while (!net_ready(iface)) {
		if (k_uptime_get() >= deadline) {
			return -ETIMEDOUT;
		}
		k_msleep(UC_NET_POLL_MS);
		iface = iface_get();
	}

	return 0;
}

void uc_net_ipv4_str(char *buf, size_t len)
{
	struct net_if *iface = iface_get();
	struct net_in_addr *addr = NULL;

	if (buf == NULL || len == 0) {
		return;
	}

	if (iface != NULL) {
		addr = net_if_ipv4_get_global_addr(iface, NET_ADDR_ANY_STATE);
	}

	if (addr == NULL || net_addr_ntop(NET_AF_INET, addr, buf, len) == NULL) {
		(void)uc_strlcpy(buf, "0.0.0.0", len);
	}
}

#if defined(CONFIG_NATIVE_SIM_REBOOT) && defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
/*
 * native_sim reboot (sys_reboot() -> CONFIG_NATIVE_SIM_REBOOT) restarts the
 * executable with execv(). NSOS opens its host sockets with SOCK_CLOEXEC, but
 * a thread blocked in a socket call waits on a dup() of the descriptor
 * without FD_CLOEXEC (nsos_adapt_dup(), Zephyr 4.4). That copy survives
 * execv() and keeps the port bound: after the reboot SMP UDP fails with
 * "Could not bind to receive socket (IPv4), err: 98". Close every host
 * descriptor above stderr as the last exit task (after the flash and UART
 * cleanup); the new image opens what it needs.
 */
#define UC_NET_HOST_FD_MAX 4096

static void uc_net_close_host_fds(void)
{
	for (int fd = 3; fd < UC_NET_HOST_FD_MAX; fd++) {
		(void)nsi_host_close(fd);
	}
}

NATIVE_TASK(uc_net_close_host_fds, ON_EXIT, 999);
#endif
