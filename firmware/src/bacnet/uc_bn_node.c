/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * BACnet node: owns the BACnet thread. Initialises the bacnet-stack basic
 * server (device object, network port, service handlers) the way the
 * bacnet-stack-zephyr B-ASC sample does, brings up BACnet/IP on the UDP
 * port from device.json once the network has an IPv4 address, registers
 * as a foreign device and installs static bindings. Provides the executor
 * that other threads use to run code in the BACnet thread and a status
 * snapshot readable from any thread.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/net_ip.h>
#include <zephyr/net/socket.h>
#include <zephyr/spinlock.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/util.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacaddr.h"
#include "bacnet/datalink/bip.h"
#include "bacnet/datalink/bvlc.h"
#include "bacnet/datalink/datalink.h"
#include "bacnet/basic/bbmd/h_bbmd.h"
#include "bacnet/basic/binding/address.h"
#include "bacnet/basic/object/device.h"
#include "bacnet/basic/server/bacnet_basic.h"
#include "bacnet/basic/server/bacnet_port.h"
#include "bacnet/basic/services.h"
#include "bacnet/basic/tsm/tsm.h"
#include <bacnet_osif/bacnet_reinit.h>

#include "uc/uc_bacnet.h"
#include "uc/uc_config.h"
#include "uc/uc_io.h"
#include "uc/uc_net.h"
#include "uc_bn_internal.h"

LOG_MODULE_REGISTER(uc_bn_node, CONFIG_UC_LOG_LEVEL);

/* Maximum bacnet_basic_task() calls per loop while packets keep arriving. */
#define BN_RX_BURST 16
/* Period of datalink start attempts while no address is available (ms). */
#define BN_DATALINK_RETRY_MS 1000
/* Slice used to wait for the network without stalling the executor (ms). */
#define BN_NET_WAIT_SLICE_MS 50
/* Status snapshot refresh period (ms). */
#define BN_STATUS_PERIOD_MS 100
/* Default BACnet/IP port. */
#define BN_BIP_PORT_DEFAULT 0xBAC0
/* Foreign device registration: minimum re-registration interval (s). */
#define BN_FD_MIN_INTERVAL_S 5

/* ---------------------------------------------------------------------- */
/* Thread and lifecycle state                                              */
/* ---------------------------------------------------------------------- */

K_THREAD_STACK_DEFINE(bn_stack, CONFIG_UC_BACNET_THREAD_STACK_SIZE);
static struct k_thread bn_thread_data;
static k_tid_t bn_tid;
static atomic_t bn_started_flag;
static atomic_t bn_ready_flag;
static atomic_t bn_instance = ATOMIC_INIT(BACNET_MAX_INSTANCE);
static K_SEM_DEFINE(bn_ready_sem, 0, 1);
static K_SEM_DEFINE(bn_wake_sem, 0, 1);

/* device.json at uc_bn_start(); instance/network/port never change at run
 * time, so this copy is immutable once the thread runs. */
static struct uc_device_cfg bn_boot_cfg;
/* Working copy of the live options (BACnet thread only). */
static struct uc_device_cfg bn_run_cfg;
/* Scratch copies (BACnet thread only). */
static struct uc_device_cfg bn_new_cfg;
static struct uc_io_cfg bn_io_cfg;

/* Strings referenced (zero copy) by the device object. */
static char bn_dev_name[UC_NAME_MAX];
static char bn_dev_desc[UC_NAME_MAX];
static char bn_dev_loc[UC_NAME_MAX];

/* Datalink / foreign device state (BACnet thread only). */
static bool bn_nsos_fallback;
static bool bn_addr_wait_logged;
static int64_t bn_fd_next_ms;
static bool bn_fd_active;

#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
static void bn_nsos_find_sockets(void);
#endif

/* Status snapshot. */
static struct k_spinlock bn_status_lock;
static struct uc_bn_status bn_status;

/* Serialises uc_bn_apply_device_cfg() callers. */
static K_MUTEX_DEFINE(bn_apply_lock);
static struct uc_device_cfg bn_apply_cfg;

/* ---------------------------------------------------------------------- */
/* Executor                                                                */
/* ---------------------------------------------------------------------- */

enum exec_state {
	EXEC_FREE = 0,
	EXEC_QUEUED,
	EXEC_DONE,
};

struct exec_slot {
	uc_bn_fn fn;
	void *arg;
	struct k_sem done;
	uint8_t state;
	bool waiter;
};

BUILD_ASSERT(CONFIG_UC_BACNET_EXEC_QUEUE_LEN > 0 && CONFIG_UC_BACNET_EXEC_QUEUE_LEN <= 255,
	     "executor slot index must fit in uint8_t");

static struct exec_slot exec_slots[CONFIG_UC_BACNET_EXEC_QUEUE_LEN];
static struct k_spinlock exec_lock;
K_MSGQ_DEFINE(exec_q, sizeof(uint8_t), CONFIG_UC_BACNET_EXEC_QUEUE_LEN, 1);

static int exec_sys_init(void)
{
	for (size_t i = 0; i < ARRAY_SIZE(exec_slots); i++) {
		k_sem_init(&exec_slots[i].done, 0, 1);
	}

	return 0;
}

SYS_INIT(exec_sys_init, POST_KERNEL, 0);

static int exec_enqueue(uc_bn_fn fn, void *arg, bool waiter, uint8_t *out_idx)
{
	k_spinlock_key_t key;
	uint8_t idx = 0;
	bool found = false;

	key = k_spin_lock(&exec_lock);
	for (size_t i = 0; i < ARRAY_SIZE(exec_slots); i++) {
		if (exec_slots[i].state == EXEC_FREE) {
			idx = (uint8_t)i;
			exec_slots[i].state = EXEC_QUEUED;
			exec_slots[i].fn = fn;
			exec_slots[i].arg = arg;
			exec_slots[i].waiter = waiter;
			found = true;
			break;
		}
	}
	k_spin_unlock(&exec_lock, key);
	if (!found) {
		return -EAGAIN;
	}

	/* A late k_sem_give() for a previous user of the slot may be pending */
	k_sem_reset(&exec_slots[idx].done);
	if (k_msgq_put(&exec_q, &idx, K_NO_WAIT) != 0) {
		key = k_spin_lock(&exec_lock);
		exec_slots[idx].state = EXEC_FREE;
		k_spin_unlock(&exec_lock, key);
		return -EAGAIN;
	}
	uc_bn_wake();
	if (out_idx != NULL) {
		*out_idx = idx;
	}

	return 0;
}

static void exec_drain(void)
{
	uint8_t idx;

	/* Bounded: a function that posts again runs in the next loop. */
	for (size_t n = 0; n < ARRAY_SIZE(exec_slots); n++) {
		struct exec_slot *slot;
		k_spinlock_key_t key;
		bool give;

		if (k_msgq_get(&exec_q, &idx, K_NO_WAIT) != 0) {
			break;
		}
		if (idx >= ARRAY_SIZE(exec_slots)) {
			continue;
		}
		slot = &exec_slots[idx];
		slot->fn(slot->arg);

		key = k_spin_lock(&exec_lock);
		give = slot->waiter;
		slot->state = give ? EXEC_DONE : EXEC_FREE;
		k_spin_unlock(&exec_lock, key);
		if (give) {
			k_sem_give(&slot->done);
		}
	}
}

bool uc_bn_in_thread(void)
{
	return (bn_tid != NULL) && (k_current_get() == bn_tid);
}

int uc_bn_exec(uc_bn_fn fn, void *arg, k_timeout_t timeout)
{
	struct exec_slot *slot;
	k_timepoint_t end;
	uint8_t idx;
	int rc;

	if (fn == NULL) {
		return -EINVAL;
	}
	if (uc_bn_in_thread()) {
		fn(arg);
		return 0;
	}

	rc = exec_enqueue(fn, arg, true, &idx);
	if (rc != 0) {
		return rc;
	}
	slot = &exec_slots[idx];
	end = sys_timepoint_calc(timeout);

	for (;;) {
		k_spinlock_key_t key;

		rc = k_sem_take(&slot->done, sys_timepoint_timeout(end));
		key = k_spin_lock(&exec_lock);
		if (slot->state == EXEC_DONE) {
			slot->state = EXEC_FREE;
			k_spin_unlock(&exec_lock, key);
			return 0;
		}
		if (rc != 0) {
			/* fn still runs later; the BACnet thread frees the slot */
			slot->waiter = false;
			k_spin_unlock(&exec_lock, key);
			return -ETIMEDOUT;
		}
		/* spurious give left over from a previous user of the slot */
		k_spin_unlock(&exec_lock, key);
	}
}

int uc_bn_post(uc_bn_fn fn, void *arg)
{
	if (fn == NULL) {
		return -EINVAL;
	}

	return exec_enqueue(fn, arg, false, NULL);
}

int uc_bn_call(uc_bn_fn fn, void *arg)
{
	if (uc_bn_in_thread()) {
		fn(arg);
		return 0;
	}
	if (!uc_bn_started()) {
		return -EAGAIN;
	}

	return uc_bn_exec(fn, arg, K_FOREVER);
}

void uc_bn_wake(void)
{
	k_sem_give(&bn_wake_sem);
}

/* ---------------------------------------------------------------------- */
/* Small helpers                                                           */
/* ---------------------------------------------------------------------- */

bool uc_bn_started(void)
{
	return atomic_get(&bn_started_flag) != 0;
}

bool uc_bn_datalink_up(void)
{
	return atomic_get(&bn_ready_flag) != 0;
}

uint32_t uc_bn_local_instance(void)
{
	return (uint32_t)atomic_get(&bn_instance);
}

bool uc_bn_device_is_local(uint32_t device)
{
	return (device == UC_BN_DEVICE_LOCAL) || (device == uc_bn_local_instance());
}

static void bn_strcpy(char *dst, size_t size, const char *src)
{
	(void)snprintf(dst, size, "%s", (src != NULL) ? src : "");
}

static bool bn_parse_ipv4(const char *str, uint8_t out[4])
{
	struct net_in_addr addr;

	if ((str == NULL) || (str[0] == '\0')) {
		return false;
	}
	if (net_addr_pton(NET_AF_INET, str, &addr) != 0) {
		return false;
	}
	memcpy(out, addr.s4_addr, 4);

	return true;
}

int uc_bn_bind_locked(uint32_t device, BACNET_ADDRESS *dest, unsigned int *max_apdu,
		      bool send_whois)
{
	if (device >= BACNET_MAX_INSTANCE) {
		return -EINVAL;
	}
	if (address_get_by_device(device, max_apdu, dest)) {
		return 0;
	}
	/* registers a bind request that handler_i_am_bind() completes */
	if (address_bind_request(device, max_apdu, dest)) {
		return 0;
	}
	if (send_whois && uc_bn_datalink_up()) {
		Send_WhoIs((int32_t)device, (int32_t)device);
	}

	return -EINPROGRESS;
}

/* ---------------------------------------------------------------------- */
/* Device identity, bindings, foreign device registration                  */
/* ---------------------------------------------------------------------- */

static bool bn_device_strings_apply(const struct uc_device_cfg *cfg)
{
	const char *name = (cfg->name[0] != '\0') ? cfg->name : CONFIG_UC_DEVICE_NAME_DEFAULT;
	bool changed = false;

	if (strcmp(bn_dev_name, name) != 0) {
		bn_strcpy(bn_dev_name, sizeof(bn_dev_name), name);
		changed = true;
	}
	if (strcmp(bn_dev_desc, cfg->description) != 0) {
		bn_strcpy(bn_dev_desc, sizeof(bn_dev_desc), cfg->description);
		changed = true;
	}
	if (strcmp(bn_dev_loc, cfg->location) != 0) {
		bn_strcpy(bn_dev_loc, sizeof(bn_dev_loc), cfg->location);
		changed = true;
	}

	/* The device object keeps pointers to these buffers (zero copy);
	 * re-setting them also drops a copy made by a network write. */
	(void)Device_Object_Name_ANSI_Init(bn_dev_name);
	(void)Device_Set_Description(bn_dev_desc, sizeof(bn_dev_desc));
	(void)Device_Set_Location(bn_dev_loc, sizeof(bn_dev_loc));

	return changed;
}

static void bn_apdu_options_apply(const struct uc_device_cfg *cfg)
{
	if (cfg->apdu_timeout_ms > 0) {
		apdu_timeout_set(cfg->apdu_timeout_ms);
	}
	apdu_retries_set(cfg->apdu_retries);
}

static bool bn_binding_address(const struct uc_device_cfg *cfg,
			       const struct uc_static_binding *b, BACNET_ADDRESS *addr)
{
	BACNET_IP_ADDRESS ip = { 0 };

	if (!bn_parse_ipv4(b->address, ip.address)) {
		return false;
	}
	ip.port = (b->port != 0) ? b->port
				 : ((cfg->udp_port != 0) ? cfg->udp_port : BN_BIP_PORT_DEFAULT);

	return bvlc_ip_address_to_bacnet_local(addr, &ip);
}

static bool bn_binding_listed(const struct uc_device_cfg *cfg, uint32_t device)
{
	for (size_t i = 0; i < cfg->binding_count; i++) {
		if (cfg->bindings[i].device == device) {
			return true;
		}
	}

	return false;
}

static void bn_static_bindings_apply(const struct uc_device_cfg *old_cfg,
				     const struct uc_device_cfg *cfg)
{
	if (old_cfg != NULL) {
		for (size_t i = 0; i < old_cfg->binding_count; i++) {
			uint32_t dev = old_cfg->bindings[i].device;

			if (!bn_binding_listed(cfg, dev)) {
				address_remove_device(dev);
			}
		}
	}

	for (size_t i = 0; i < cfg->binding_count; i++) {
		const struct uc_static_binding *b = &cfg->bindings[i];
		BACNET_ADDRESS addr = { 0 };

		if ((b->device >= BACNET_MAX_INSTANCE) || (b->device == cfg->instance)) {
			LOG_WRN("static binding %u ignored", b->device);
			continue;
		}
		if (!bn_binding_address(cfg, b, &addr)) {
			LOG_WRN("static binding %u: bad address '%s'", b->device, b->address);
			continue;
		}
		address_add(b->device, MAX_APDU, &addr);
		address_set_device_TTL(b->device, 0, true);
		LOG_DBG("static binding %u -> %s:%u", b->device, b->address, b->port);
	}
}

static void bn_fd_register(int64_t now)
{
	BACNET_IP_ADDRESS bbmd = { 0 };
	uint16_t ttl = (bn_run_cfg.fd_ttl_s != 0) ? bn_run_cfg.fd_ttl_s : 60;
	uint32_t interval;
	int rc;

	if (!bn_run_cfg.fd_enabled || !uc_bn_datalink_up()) {
		bn_fd_active = false;
		return;
	}
	if (!bn_parse_ipv4(bn_run_cfg.fd_bbmd, bbmd.address)) {
		LOG_ERR("foreign device: bad BBMD address '%s'", bn_run_cfg.fd_bbmd);
		bn_fd_active = false;
		return;
	}
	bbmd.port = (bn_run_cfg.fd_port != 0) ? bn_run_cfg.fd_port : BN_BIP_PORT_DEFAULT;

	rc = bvlc_register_with_bbmd(&bbmd, ttl);
	if (rc <= 0) {
		LOG_WRN("foreign device registration with %s:%u failed (%d)", bn_run_cfg.fd_bbmd,
			bbmd.port, rc);
	} else if (!bn_fd_active) {
		LOG_INF("foreign device registration with %s:%u ttl %u s", bn_run_cfg.fd_bbmd,
			bbmd.port, ttl);
	}
	bn_fd_active = true;
	/* re-register well before the BBMD drops the entry */
	interval = MAX((uint32_t)ttl / 2U, (uint32_t)BN_FD_MIN_INTERVAL_S);
	bn_fd_next_ms = now + (int64_t)interval * MSEC_PER_SEC;
}

static bool bn_fd_cfg_changed(const struct uc_device_cfg *a, const struct uc_device_cfg *b)
{
	return (a->fd_enabled != b->fd_enabled) || (strcmp(a->fd_bbmd, b->fd_bbmd) != 0) ||
	       (a->fd_port != b->fd_port) || (a->fd_ttl_s != b->fd_ttl_s);
}

/* ---------------------------------------------------------------------- */
/* Stack initialisation                                                    */
/* ---------------------------------------------------------------------- */

static void bn_init_callback(void *context)
{
	ARG_UNUSED(context);

	(void)Device_Set_Object_Instance_Number(bn_run_cfg.instance);
	address_own_device_id_set(bn_run_cfg.instance);
	atomic_set(&bn_instance, (atomic_val_t)bn_run_cfg.instance);

	(void)bn_device_strings_apply(&bn_run_cfg);
	(void)Device_Set_Model_Name(CONFIG_BOARD, sizeof(CONFIG_BOARD));
	(void)Device_Set_Firmware_Revision(CONFIG_UC_FW_VERSION, sizeof(CONFIG_UC_FW_VERSION));
	(void)Device_Set_Application_Software_Version(CONFIG_UC_FW_VERSION,
						      sizeof(CONFIG_UC_FW_VERSION));
	bn_apdu_options_apply(&bn_run_cfg);

	address_init();
	bn_static_bindings_apply(NULL, &bn_run_cfg);

	uc_bn_local_init_locked();
	uc_bn_client_init_locked();
	uc_bn_cov_init_locked();

	bacnet_reinitialize_device_init(CONFIG_BACNET_REINIT_REBOOT_DELAY);
}

/* Unicast and broadcast address of the BACnet/IP datalink. */
static bool bn_datalink_address(BACNET_IP_ADDRESS *unicast, BACNET_IP_ADDRESS *bcast)
{
	struct net_if *iface = net_if_get_default();
	struct net_in_addr *addr = NULL;
	struct net_in_addr mask;

	if (iface != NULL) {
		addr = net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED);
	}
	if ((addr == NULL) || (addr->s_addr == 0)) {
		addr = NULL;
	}

#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
	/* native_sim with host sockets: the interface address set by uc_net
	 * (static address of the host, else 127.0.0.1); loopback if binding
	 * that address failed. Host sockets offer no SO_BROADCAST, so
	 * broadcasts go to the node's own address: use static bindings. */
	ARG_UNUSED(mask);
	if ((addr != NULL) && !bn_nsos_fallback) {
		memcpy(unicast->address, addr->s4_addr, 4);
	} else {
		(void)bvlc_address_set(unicast, 127, 0, 0, 1);
	}
	memcpy(bcast->address, unicast->address, 4);

	return true;
#else
	if (addr == NULL) {
		return false;
	}
	mask = net_if_ipv4_get_netmask_by_addr(iface, addr);
	for (int i = 0; i < 4; i++) {
		unicast->address[i] = addr->s4_addr[i];
		bcast->address[i] = addr->s4_addr[i] | (uint8_t)~mask.s4_addr[i];
	}

	return true;
#endif
}

static bool bn_datalink_start(void)
{
	BACNET_IP_ADDRESS unicast = { 0 };
	BACNET_IP_ADDRESS bcast = { 0 };
	uint16_t port = (bn_run_cfg.udp_port != 0) ? bn_run_cfg.udp_port : BN_BIP_PORT_DEFAULT;

	if (!bn_datalink_address(&unicast, &bcast)) {
		if (!bn_addr_wait_logged) {
			bn_addr_wait_logged = true;
			LOG_INF("BACnet/IP waiting for an IPv4 address");
		}
		return false;
	}
	unicast.port = port;
	bcast.port = port;
	/* bip_init() only queries the interface (and blocks on DHCP) when no
	 * address is set; setting it here keeps the start non-blocking. */
	bip_set_port(port);
	(void)bip_set_addr(&unicast);
	(void)bip_set_broadcast_addr(&bcast);

	if (!bacnet_port_init()) {
		bip_cleanup();
		LOG_WRN("BACnet/IP start on %u.%u.%u.%u:%u failed", unicast.address[0],
			unicast.address[1], unicast.address[2], unicast.address[3], port);
		if (IS_ENABLED(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)) {
			bn_nsos_fallback = true;
		}
		return false;
	}
	LOG_INF("BACnet/IP %u.%u.%u.%u:%u, device %u", unicast.address[0], unicast.address[1],
		unicast.address[2], unicast.address[3], port, Device_Object_Instance_Number());
#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
	bn_nsos_find_sockets();
#endif

	return true;
}

/* ---------------------------------------------------------------------- */
/* Status                                                                  */
/* ---------------------------------------------------------------------- */

static uint32_t bn_packet_count(void);

static void bn_status_update(void)
{
	struct uc_bn_status st = { 0 };
	BACNET_IP_ADDRESS ip = { 0 };
	k_spinlock_key_t key;
	uint32_t instance = Device_Object_Instance_Number();

	atomic_set(&bn_instance, (atomic_val_t)instance);
	st.ready = uc_bn_datalink_up();
	st.device_instance = instance;
	bn_strcpy(st.device_name, sizeof(st.device_name), Device_Object_Name_ANSI());
	if (st.ready && bip_get_addr(&ip)) {
		(void)snprintf(st.ipv4, sizeof(st.ipv4), "%u.%u.%u.%u", ip.address[0],
			       ip.address[1], ip.address[2], ip.address[3]);
		st.udp_port = ip.port;
	} else {
		uc_net_ipv4_str(st.ipv4, sizeof(st.ipv4));
		st.udp_port = bn_run_cfg.udp_port;
	}
	st.packets = bn_packet_count();
	st.objects = Device_Object_List_Count();
	st.uptime_s = (uint32_t)bacnet_basic_uptime_seconds();

	key = k_spin_lock(&bn_status_lock);
	bn_status = st;
	k_spin_unlock(&bn_status_lock, key);
}

void uc_bn_status_get(struct uc_bn_status *st)
{
	k_spinlock_key_t key;

	if (st == NULL) {
		return;
	}
	key = k_spin_lock(&bn_status_lock);
	*st = bn_status;
	k_spin_unlock(&bn_status_lock, key);
	st->ready = uc_bn_datalink_up();
}

bool uc_bn_ready(void)
{
	return uc_bn_datalink_up();
}

int uc_bn_wait_ready(k_timeout_t timeout)
{
	if (uc_bn_datalink_up()) {
		return 0;
	}
	if (k_sem_take(&bn_ready_sem, timeout) != 0) {
		return uc_bn_datalink_up() ? 0 : -ETIMEDOUT;
	}
	/* hand the token on to the next waiter */
	k_sem_give(&bn_ready_sem);

	return 0;
}

/* ---------------------------------------------------------------------- */
/* Live device configuration                                               */
/* ---------------------------------------------------------------------- */

static void bn_apply_cfg_locked(void *arg)
{
	int64_t now = k_uptime_get();

	ARG_UNUSED(arg);
	uc_config_get_device(&bn_new_cfg);

	if (bn_device_strings_apply(&bn_new_cfg)) {
		Device_Inc_Database_Revision();
		LOG_INF("device name/description/location updated ('%s')", bn_dev_name);
	}
	bn_apdu_options_apply(&bn_new_cfg);
	/* the stack keeps the device instance from boot */
	bn_new_cfg.instance = bn_run_cfg.instance;
	bn_static_bindings_apply(&bn_run_cfg, &bn_new_cfg);

	if (bn_fd_cfg_changed(&bn_run_cfg, &bn_new_cfg)) {
		bool was_active = bn_fd_active;

		bn_run_cfg.fd_enabled = bn_new_cfg.fd_enabled;
		memcpy(bn_run_cfg.fd_bbmd, bn_new_cfg.fd_bbmd, sizeof(bn_run_cfg.fd_bbmd));
		bn_run_cfg.fd_port = bn_new_cfg.fd_port;
		bn_run_cfg.fd_ttl_s = bn_new_cfg.fd_ttl_s;
		if (!bn_run_cfg.fd_enabled && was_active && uc_bn_datalink_up()) {
			(void)bvlc_delete_from_bbmd();
		}
		bn_fd_active = false;
		bn_fd_register(now);
	}

	/* keep instance/network/port of the running stack */
	memcpy(bn_run_cfg.name, bn_new_cfg.name, sizeof(bn_run_cfg.name));
	memcpy(bn_run_cfg.description, bn_new_cfg.description, sizeof(bn_run_cfg.description));
	memcpy(bn_run_cfg.location, bn_new_cfg.location, sizeof(bn_run_cfg.location));
	bn_run_cfg.apdu_timeout_ms = bn_new_cfg.apdu_timeout_ms;
	bn_run_cfg.apdu_retries = bn_new_cfg.apdu_retries;
	bn_run_cfg.binding_count = bn_new_cfg.binding_count;
	memcpy(bn_run_cfg.bindings, bn_new_cfg.bindings, sizeof(bn_run_cfg.bindings));
}

static bool bn_network_changed(const struct uc_device_cfg *a, const struct uc_device_cfg *b)
{
	if (a->dhcp != b->dhcp) {
		return true;
	}
	if (a->dhcp) {
		return false;
	}

	return (strcmp(a->ipv4, b->ipv4) != 0) || (strcmp(a->netmask, b->netmask) != 0) ||
	       (strcmp(a->gateway, b->gateway) != 0);
}

int uc_bn_apply_device_cfg(bool *reboot_required)
{
	bool reboot = false;

	if (!uc_bn_started()) {
		/* nothing runs yet: uc_bn_start() reads the new document */
		if (reboot_required != NULL) {
			*reboot_required = false;
		}
		return 0;
	}

	(void)k_mutex_lock(&bn_apply_lock, K_FOREVER);
	uc_config_get_device(&bn_apply_cfg);
	reboot = (bn_apply_cfg.instance != bn_boot_cfg.instance) ||
		 (bn_apply_cfg.udp_port != bn_boot_cfg.udp_port) ||
		 bn_network_changed(&bn_apply_cfg, &bn_boot_cfg);
	(void)k_mutex_unlock(&bn_apply_lock);

	if (reboot_required != NULL) {
		*reboot_required = reboot;
	}
	if (reboot) {
		LOG_INF("device.json: instance, network or UDP port changed, reboot required");
	}

	return uc_bn_call(bn_apply_cfg_locked, NULL);
}

/* ---------------------------------------------------------------------- */
/* Thread                                                                  */
/* ---------------------------------------------------------------------- */

static void bn_set_ready(void)
{
	atomic_set(&bn_ready_flag, 1);
	k_sem_give(&bn_ready_sem);
}

#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
/*
 * native_sim host sockets (NSOS, Zephyr 4.4) cannot be used with the
 * select() in bip_receive():
 *  - a zero-timeout poll never reports readiness (the host epoll result
 *    arrives through an interrupt only while a poll waits), so the
 *    receive in bacnet_basic_task() never sees a packet;
 *  - a waiting poll on the socket's own poll entry calls
 *    k_condvar_signal(poll->cond) with an uninitialised pointer
 *    (nsos_socket_create() uses k_malloc()) and crashes.
 * So the two BACnet/IP sockets are located after bip_init() and read here
 * with MSG_DONTWAIT (no poll involved), then passed through the same BVLC
 * and NPDU handlers as bip_receive() does.
 */
static uint8_t bn_rx_buf[MAX_MPDU];
static uint32_t bn_rx_count;
static int bn_nsos_fd[2] = { -1, -1 }; /* unicast, broadcast */

static void bn_nsos_find_sockets(void)
{
	BACNET_IP_ADDRESS me = { 0 };

	(void)bip_get_addr(&me);
	bn_nsos_fd[0] = -1;
	bn_nsos_fd[1] = -1;
	for (int fd = 0; fd < CONFIG_ZVFS_OPEN_MAX; fd++) {
		struct net_sockaddr_in sin = { 0 };
		net_socklen_t len = sizeof(sin);

		if (zsock_getsockname(fd, (struct net_sockaddr *)&sin, &len) != 0) {
			continue;
		}
		if ((sin.sin_family != NET_AF_INET) || (net_ntohs(sin.sin_port) != me.port)) {
			continue;
		}
		if ((bn_nsos_fd[0] < 0) && (memcmp(&sin.sin_addr, me.address, 4) == 0)) {
			bn_nsos_fd[0] = fd;
		} else if ((bn_nsos_fd[1] < 0) && (sin.sin_addr.s_addr == 0)) {
			bn_nsos_fd[1] = fd;
		}
	}
	if ((bn_nsos_fd[0] < 0) || (bn_nsos_fd[1] < 0)) {
		LOG_ERR("NSOS: BACnet/IP sockets not found (%d, %d)", bn_nsos_fd[0],
			bn_nsos_fd[1]);
	}
}

static bool bn_nsos_receive_one(int idx)
{
	struct net_sockaddr_in sin = { 0 };
	net_socklen_t sin_len = sizeof(sin);
	BACNET_IP_ADDRESS addr = { 0 };
	BACNET_ADDRESS src = { 0 };
	ssize_t len;
	int offset;

	if (bn_nsos_fd[idx] < 0) {
		return false;
	}
	len = zsock_recvfrom(bn_nsos_fd[idx], bn_rx_buf, sizeof(bn_rx_buf), ZSOCK_MSG_DONTWAIT,
			     (struct net_sockaddr *)&sin, &sin_len);
	if (len <= 0) {
		return false;
	}
	if ((len < 4) || (bn_rx_buf[0] != BVLL_TYPE_BACNET_IP)) {
		return true;
	}
	memcpy(addr.address, &sin.sin_addr, 4);
	addr.port = net_ntohs(sin.sin_port);
	offset = (idx == 0) ? bvlc_handler(&addr, &src, bn_rx_buf, (uint16_t)len)
			    : bvlc_broadcast_handler(&addr, &src, bn_rx_buf, (uint16_t)len);
	if ((offset > 0) && (offset < len)) {
		npdu_handler(&src, &bn_rx_buf[offset], (uint16_t)(len - offset));
		bn_rx_count++;
	}

	return true;
}

static void bn_nsos_receive(void)
{
	for (int n = 0; n < BN_RX_BURST; n++) {
		bool unicast = bn_nsos_receive_one(0);
		bool bcast = bn_nsos_receive_one(1);

		if (!unicast && !bcast) {
			break;
		}
	}
}
#endif

static uint32_t bn_packet_count(void)
{
	uint32_t count = (uint32_t)bacnet_basic_packet_count();

#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
	count += bn_rx_count;
#endif

	return count;
}

static void bn_stack_task(void)
{
#if defined(CONFIG_NET_NATIVE_OFFLOADED_SOCKETS)
	bn_nsos_receive();
#endif
	/* stack and datalink receive: one packet per bacnet_basic_task() */
	for (int n = 0; n < BN_RX_BURST; n++) {
		unsigned long before = bacnet_basic_packet_count();

		bacnet_basic_task();
		if (bacnet_basic_packet_count() == before) {
			break;
		}
	}
	bacnet_port_task();
}

static void bn_thread(void *p1, void *p2, void *p3)
{
	int64_t now = k_uptime_get();
	int64_t last = now;
	int64_t net_deadline = now + CONFIG_UC_NET_WAIT_MS;
	int64_t next_datalink = now;
	int64_t next_second = now + MSEC_PER_SEC;
	int64_t next_status = now;
	bool net_ok = false;
	int rc;

	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	/* Objects exist before the network is up: the stack is initialised
	 * first, the BACnet/IP datalink is started once an address exists. */
	bip_set_port((bn_run_cfg.udp_port != 0) ? bn_run_cfg.udp_port : BN_BIP_PORT_DEFAULT);
	bacnet_basic_init_callback_set(bn_init_callback, NULL);
	bacnet_basic_init();

	uc_config_get_io(&bn_io_cfg);
	rc = uc_io_apply_config_locked(&bn_io_cfg);
	if (rc < 0) {
		LOG_ERR("io.json not applied (%d)", rc);
	} else {
		LOG_INF("%d IO point(s) bound", rc);
	}
	bn_status_update();

	for (;;) {
		int64_t elapsed;

		if (uc_bn_datalink_up()) {
			(void)k_sem_take(&bn_wake_sem, K_MSEC(CONFIG_UC_BACNET_POLL_MS));
		} else if (!net_ok) {
			/* sliced wait keeps the executor and IO scan running */
			int64_t t0 = k_uptime_get();

			if (uc_net_wait_ready(BN_NET_WAIT_SLICE_MS) == 0) {
				net_ok = true;
			} else if (k_uptime_get() >= net_deadline) {
				LOG_WRN("no IPv4 address after %d ms, starting BACnet anyway",
					CONFIG_UC_NET_WAIT_MS);
				net_ok = true;
			} else if ((k_uptime_get() - t0) < CONFIG_UC_BACNET_POLL_MS) {
				/* never spin if the wait returns early */
				(void)k_sem_take(&bn_wake_sem, K_MSEC(CONFIG_UC_BACNET_POLL_MS));
			}
		} else {
			(void)k_sem_take(&bn_wake_sem, K_MSEC(CONFIG_UC_BACNET_POLL_MS));
		}

		now = k_uptime_get();
		elapsed = now - last;
		last = now;

		if (uc_bn_datalink_up()) {
			bn_stack_task();
		} else if (net_ok && (now >= next_datalink)) {
			next_datalink = now + BN_DATALINK_RETRY_MS;
			if (bn_datalink_start()) {
				bn_set_ready();
				bn_fd_register(now);
			}
		}

		exec_drain();

		if (uc_bn_datalink_up()) {
			tsm_timer_milliseconds((uint16_t)CLAMP(elapsed, 0, UINT16_MAX));
			if (now >= next_second) {
				int64_t late_s = (now - next_second) / MSEC_PER_SEC;
				uint16_t secs = (uint16_t)CLAMP(late_s + 1, 1, UINT16_MAX);

				next_second += (int64_t)secs * MSEC_PER_SEC;
				address_cache_timer(secs);
			}
			if (bn_fd_active && (now >= bn_fd_next_ms)) {
				bn_fd_register(now);
			}
			bacnet_reinitialize_device_task(NULL, NULL);
		}

		uc_bn_client_tick_locked(now);
		uc_bn_cov_tick_locked(now);
		uc_io_scan();

		if (now >= next_status) {
			next_status = now + BN_STATUS_PERIOD_MS;
			bn_status_update();
		}
	}
}

int uc_bn_start(void)
{
	if (!atomic_cas(&bn_started_flag, 0, 1)) {
		return -EALREADY;
	}

	uc_config_get_device(&bn_boot_cfg);
	bn_run_cfg = bn_boot_cfg;
	if (bn_run_cfg.instance >= BACNET_MAX_INSTANCE) {
		LOG_WRN("device instance %u invalid, using %d", bn_run_cfg.instance,
			CONFIG_UC_DEVICE_INSTANCE_DEFAULT);
		bn_run_cfg.instance = CONFIG_UC_DEVICE_INSTANCE_DEFAULT;
		bn_boot_cfg.instance = bn_run_cfg.instance;
	}
	atomic_set(&bn_instance, (atomic_val_t)bn_run_cfg.instance);

	bn_tid = k_thread_create(&bn_thread_data, bn_stack, K_THREAD_STACK_SIZEOF(bn_stack),
				 bn_thread, NULL, NULL, NULL, CONFIG_UC_BACNET_THREAD_PRIORITY, 0,
				 K_FOREVER);
	(void)k_thread_name_set(bn_tid, "bacnet");
	k_thread_start(bn_tid);

	return 0;
}
