/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Internal interfaces between the BACnet node translation units:
 *
 *   uc_bn_node.c    thread, lifecycle, datalink, executor, status
 *   uc_bn_local.c   local objects, owner table, write hook
 *   uc_bn_client.c  blocking remote ReadProperty/WriteProperty, APDU
 *                   confirmation dispatch (ack/error/abort/reject/timeout)
 *   uc_bn_cov.c     application COV subscriptions (local and remote)
 *
 * Every *_locked function runs in the BACnet thread only.
 */
#ifndef UC_BN_INTERNAL_H_
#define UC_BN_INTERNAL_H_

#include <errno.h>
#include <stdbool.h>
#include <stdint.h>

#include <zephyr/kernel.h>

#include "bacnet/bacdef.h"
#include "bacnet/apdu.h"
#include "bacnet/bacapp.h"

#include "uc/uc_bacnet.h"
#include "uc/uc_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/* EREMOTEIO (BACnet Error/Reject/Abort) comes from uc_common.h (included
 * through uc_bacnet.h): the libc value where defined (glibc on native_sim),
 * else one value from picolibc's user range. Never define it here. */

/* Encoded application data of one written value (numeric, NULL or a
 * CharacterString of MAX_CHARACTER_STRING_BYTES). */
#define UC_BN_VALUE_BUF_SIZE (MAX_CHARACTER_STRING_BYTES + 16)

/* Default remote SubscribeCOV lifetime when the caller passes 0 (s). */
#define UC_BN_COV_LIFETIME_DEFAULT_S 300u

/* ---------------------------------------------------------------------- */
/* uc_bn_node.c                                                            */
/* ---------------------------------------------------------------------- */

/** True once the datalink is up (same as uc_bn_ready()). */
bool uc_bn_datalink_up(void);

/** Current device instance (thread-safe snapshot). */
uint32_t uc_bn_local_instance(void);

/** True when device refers to this device (UC_BN_DEVICE_LOCAL or the
 *  local instance). */
bool uc_bn_device_is_local(uint32_t device);

/** Wake the BACnet thread before its poll period expires. */
void uc_bn_wake(void);

/** True once uc_bn_start() created the BACnet thread. */
bool uc_bn_started(void);

/** Run fn(arg) in the BACnet thread and wait without a time limit
 *  (inline when already in the BACnet thread). -EAGAIN when the thread
 *  was not started or the executor queue is full. */
int uc_bn_call(uc_bn_fn fn, void *arg);

/** Bind a device for a confirmed request. Returns 0 and fills dest and
 *  max_apdu when the device is in the address cache (static binding or
 *  I-Am), otherwise registers a bind request, sends Who-Is when
 *  send_whois is set and returns -EINPROGRESS. */
int uc_bn_bind_locked(uint32_t device, BACNET_ADDRESS *dest,
		      unsigned int *max_apdu, bool send_whois);

/* ---------------------------------------------------------------------- */
/* uc_bn_local.c                                                           */
/* ---------------------------------------------------------------------- */

/** Reset the owner table, install the WriteProperty store callback and the
 *  CreateObject/DeleteObject policy (CONFIG_UC_BACNET_REMOTE_CREATE_DELETE).
 *  Called from the stack init callback, after bacnet_basic_init() has
 *  registered the stack's service handlers. */
void uc_bn_local_init_locked(void);

/** Map a BACnet error class/code of a local operation to a negative
 *  errno (see uc_common.h). */
int uc_bn_local_err(BACNET_ERROR_CLASS error_class, BACNET_ERROR_CODE error_code);

/** Send a BACnet-Error PDU for a confirmed request (reply to src). */
void uc_bn_reply_error(BACNET_ADDRESS *src, const BACNET_CONFIRMED_SERVICE_DATA *service_data,
		       BACNET_CONFIRMED_SERVICE service, BACNET_ERROR_CLASS error_class,
		       BACNET_ERROR_CODE error_code);

/* ---------------------------------------------------------------------- */
/* uc_bn_client.c                                                          */
/* ---------------------------------------------------------------------- */

/** Register the confirmed-service ack/error handlers, the I-Am binding
 *  handler, the abort/reject handlers and the TSM timeout handler. */
void uc_bn_client_init_locked(void);

/** Transaction state machine of the blocking client slots. */
void uc_bn_client_tick_locked(int64_t now_ms);

/* ---------------------------------------------------------------------- */
/* uc_bn_cov.c                                                             */
/* ---------------------------------------------------------------------- */

/** Register the COV notification and SubscribeCOV handlers. */
void uc_bn_cov_init_locked(void);

/** Subscription state machine, renewals, polling, local detection. */
void uc_bn_cov_tick_locked(int64_t now_ms);

/** ReadProperty-ACK for an invoke id not owned by the client slots.
 *  value is NULL when the ack could not be decoded. Returns true when
 *  the invoke id belonged to a COV poll. */
bool uc_bn_cov_rp_result_locked(uint8_t invoke_id, const BACNET_ADDRESS *src,
				int err, const BACNET_APPLICATION_DATA_VALUE *value);

/** Failure (Error, Reject, Abort: -EREMOTEIO; TSM timeout: -ETIMEDOUT) or
 *  success (err 0, SubscribeCOV simple ack) of a confirmed request not
 *  owned by the client slots. Returns true when consumed. */
bool uc_bn_cov_confirmed_result_locked(uint8_t invoke_id,
				       const BACNET_ADDRESS *src, int err);

#ifdef __cplusplus
}
#endif

#endif /* UC_BN_INTERNAL_H_ */
