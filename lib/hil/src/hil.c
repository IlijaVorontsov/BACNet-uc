/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * lib/hil: boot and network-ready lines plus timing markers for instrumented DUT images
 * (docs/HIL.md 8.3, contract FW-15). The rig's instrumented tier waits on these lines instead
 * of on application log text (FW-03 freezes only three app lines):
 *
 *   HIL-BOOT board=<board target> zephyr=<version> reset=0x<hwinfo cause> uid=<hex>
 *       once, from SYS_INIT(APPLICATION, 99), i.e. just before main(). The reset cause is
 *       cleared afterwards, so every boot reports only its own cause (RST-01/RST-04).
 *   HIL-READY ip=<IPv4> mac=<xx:xx:xx:xx:xx:xx>
 *       on every NET_EVENT_IPV4_ADDR_ADD (DHCP lease or static address), and once at boot if
 *       the default interface already has an address (native_sim --ipv4-addr).
 *
 * Marker m0 toggles right before each line, so the logic analyzer sees both milestones.
 * The markers are the hil-marker-gpios of /zephyr,user (snippets/hil board overlay); a board
 * without them prints the lines only. The lines go through printk: with CONFIG_LOG_PRINTK
 * they are routed through the log core without a log prefix, on the console of both apps.
 */
#include <errno.h>

#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/util.h>
#include <zephyr/version.h>

#if defined(CONFIG_NET_IPV4)
#include <zephyr/net/net_event.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/net_mgmt.h>
#endif

#include <hil/hil.h>

#define HIL_USER_NODE DT_PATH(zephyr_user)

#if defined(CONFIG_GPIO) && DT_NODE_HAS_PROP(HIL_USER_NODE, hil_marker_gpios)
#define HIL_HAS_MARKERS 1
#define HIL_MARKER_SPEC(node, prop, idx) GPIO_DT_SPEC_GET_BY_IDX(node, prop, idx),
static const struct gpio_dt_spec markers[] = {
	DT_FOREACH_PROP_ELEM(HIL_USER_NODE, hil_marker_gpios, HIL_MARKER_SPEC)};
#define HIL_MARKERS ARRAY_SIZE(markers)
/* Commanded marker levels (bit n = marker n), shared by the net_mgmt thread and the shell. */
static atomic_t marker_levels;
#else
#define HIL_HAS_MARKERS 0
#define HIL_MARKERS 0
#endif

static uint32_t boot_cause;
static char uid_hex[2 * 16 + 1] = "-";

size_t hil_marker_count(void)
{
	return HIL_MARKERS;
}

int hil_mark(size_t n, int level)
{
#if HIL_HAS_MARKERS
	bool on;
	int rc;

	if (n >= HIL_MARKERS) {
		return -ENOENT;
	}
	if (level == HIL_MARK_TOGGLE) {
		on = !atomic_test_bit(&marker_levels, n);
	} else {
		on = level != 0;
	}
	rc = gpio_pin_set_dt(&markers[n], on);
	if (rc != 0) {
		return rc;
	}
	atomic_set_bit_to(&marker_levels, n, on);
	return on ? 1 : 0;
#else
	ARG_UNUSED(n);
	ARG_UNUSED(level);
	return -ENOENT;
#endif
}

uint32_t hil_boot_reset_cause(void)
{
	return boot_cause;
}

const char *hil_uid_hex(void)
{
	return uid_hex;
}

#if defined(CONFIG_NET_IPV4)

char *hil_ipv4_str(char *buf, size_t len)
{
	struct net_if *iface = net_if_get_default();
	struct net_in_addr *addr =
		iface != NULL ? net_if_ipv4_get_global_addr(iface, NET_ADDR_ANY_STATE) : NULL;

	if (addr == NULL || net_addr_ntop(NET_AF_INET, addr, buf, len) == NULL) {
		(void)snprintk(buf, len, "-");
	}
	return buf;
}

static void print_ready(struct net_if *iface, const struct net_in_addr *addr)
{
	char ip[NET_IPV4_ADDR_LEN];
	char mac[sizeof("xx:xx:xx:xx:xx:xx")] = "-";
	const struct net_linkaddr *ll = net_if_get_link_addr(iface);

	if (addr == NULL || net_addr_ntop(NET_AF_INET, addr, ip, sizeof(ip)) == NULL) {
		return;
	}
	if (ll != NULL && ll->len == 6U) {
		(void)snprintk(mac, sizeof(mac), "%02x:%02x:%02x:%02x:%02x:%02x", ll->addr[0],
			       ll->addr[1], ll->addr[2], ll->addr[3], ll->addr[4], ll->addr[5]);
	}
	(void)hil_mark(0, HIL_MARK_TOGGLE);
	printk("HIL-READY ip=%s mac=%s\n", ip, mac);
}

#if defined(CONFIG_NET_MGMT_EVENT)
static struct net_mgmt_event_callback ipv4_cb;

static void ipv4_event(struct net_mgmt_event_callback *cb, uint64_t mgmt_event,
		       struct net_if *iface)
{
	const struct net_in_addr *addr = NULL;

	if (mgmt_event != NET_EVENT_IPV4_ADDR_ADD) {
		return;
	}
#if defined(CONFIG_NET_MGMT_EVENT_INFO)
	/* the added address itself; the interface may hold more than one */
	if (cb->info != NULL && cb->info_length == sizeof(struct net_in_addr)) {
		addr = cb->info;
	}
#else
	ARG_UNUSED(cb);
#endif
	if (addr == NULL) {
		addr = net_if_ipv4_get_global_addr(iface, NET_ADDR_ANY_STATE);
	}
	print_ready(iface, addr);
}
#endif /* CONFIG_NET_MGMT_EVENT */

static void net_ready_init(void)
{
	struct net_if *iface = net_if_get_default();

#if defined(CONFIG_NET_MGMT_EVENT)
	net_mgmt_init_event_callback(&ipv4_cb, ipv4_event, NET_EVENT_IPV4_ADDR_ADD);
	net_mgmt_add_event_callback(&ipv4_cb);
#endif
	/* an address assigned before this point (static, command line) raised no event for us */
	if (iface != NULL) {
		print_ready(iface, net_if_ipv4_get_global_addr(iface, NET_ADDR_ANY_STATE));
	}
}

#else /* !CONFIG_NET_IPV4 */

char *hil_ipv4_str(char *buf, size_t len)
{
	(void)snprintk(buf, len, "-");
	return buf;
}

static void net_ready_init(void)
{
}

#endif /* CONFIG_NET_IPV4 */

static void uid_init(void)
{
	uint8_t id[16];
	ssize_t n = hwinfo_get_device_id(id, sizeof(id));

	if (n <= 0) {
		return;
	}
	for (ssize_t i = 0; i < n; i++) {
		(void)snprintk(&uid_hex[2 * i], 3, "%02x", id[i]);
	}
}

static int hil_init(void)
{
	if (hwinfo_get_reset_cause(&boot_cause) != 0) {
		boot_cause = 0;
	}
	(void)hwinfo_clear_reset_cause();
	uid_init();

#if HIL_HAS_MARKERS
	for (size_t i = 0; i < HIL_MARKERS; i++) {
		if (gpio_is_ready_dt(&markers[i])) {
			(void)gpio_pin_configure_dt(&markers[i], GPIO_OUTPUT_INACTIVE);
		}
	}
#endif
	(void)hil_mark(0, HIL_MARK_TOGGLE);
	printk("HIL-BOOT board=%s zephyr=%s reset=0x%08x uid=%s\n", CONFIG_BOARD_TARGET,
	       KERNEL_VERSION_STRING, boot_cause, uid_hex);

	net_ready_init();
	return 0;
}

SYS_INIT(hil_init, APPLICATION, 99);
