/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * IPv4 network bring-up from device.json (static address or DHCPv4).
 */
#ifndef UC_NET_H_
#define UC_NET_H_

#include <stdbool.h>
#include <stddef.h>

#include "uc_config.h"

/** Configure the default interface: static address/netmask/gateway or
 *  start DHCPv4. Does not wait for the link. */
int uc_net_init(const struct uc_device_cfg *cfg);

/** Block until the interface has an IPv4 address or timeout_ms elapses.
 *  Returns 0 or -ETIMEDOUT. */
int uc_net_wait_ready(uint32_t timeout_ms);

/** Current IPv4 address of the default interface as text ("0.0.0.0" if
 *  none). */
void uc_net_ipv4_str(char *buf, size_t len);

#endif /* UC_NET_H_ */
