/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * BACnet node: owns the BACnet stack thread. The BACnet stack is not thread
 * safe; every call into it happens in the BACnet thread. Other threads use
 * the executor (uc_bn_exec/uc_bn_post) or the thread-safe wrappers below,
 * which marshal into the BACnet thread themselves.
 *
 * BACnet thread loop (period CONFIG_UC_BACNET_POLL_MS):
 *   bacnet_basic_task() + bacnet_port_task()   stack, datalink receive
 *   drain the executor queue
 *   TSM timers, client transaction timeouts
 *   COV subscription renewals and poll fallback
 *   local COV change detection for app subscriptions
 *   uc_io_scan()                               IO <-> Present_Value
 */
#ifndef UC_BACNET_H_
#define UC_BACNET_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zephyr/kernel.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacapp.h"

#include "uc_common.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Owner of objects created by a BACnet client with CreateObject (only with
 * CONFIG_UC_BACNET_REMOTE_CREATE_DELETE). Complements the owner ids of
 * uc_common.h (between UC_OWNER_IO and UC_OWNER_APP_BASE); reported as
 * "network". Only these objects may be deleted with DeleteObject. */
#define UC_OWNER_NETWORK 3u

/* ---------------------------------------------------------------------- */
/* Lifecycle                                                               */
/* ---------------------------------------------------------------------- */

/** Start the BACnet thread. Reads device.json via uc_config_get_device(),
 *  waits for the network, initialises the stack (device object, network
 *  port, BACnet/IP on the configured UDP port, foreign device
 *  registration, static bindings), then calls uc_io_apply_config() for the
 *  cached io.json. Returns immediately. */
int uc_bn_start(void);

/** True once the stack is initialised and the datalink is up. */
bool uc_bn_ready(void);

/** Block until uc_bn_ready() or timeout. 0 or -ETIMEDOUT. */
int uc_bn_wait_ready(k_timeout_t timeout);

struct uc_bn_status {
	bool ready;
	uint32_t device_instance;
	char device_name[UC_NAME_MAX];
	char ipv4[16];
	uint16_t udp_port;
	uint32_t packets;
	uint32_t objects; /* snapshot, see uc_bn_obj_count() */
	uint32_t uptime_s;
};

/** Snapshot published by the BACnet thread (at most 100 ms old); never
 *  blocks. */
void uc_bn_status_get(struct uc_bn_status *st);

/** Number of objects of the device (including the device object), counted
 *  now in the BACnet thread. Falls back to the status snapshot when the
 *  BACnet thread does not answer within 500 ms or was not started. */
uint32_t uc_bn_obj_count(void);

/** Apply the cached device.json: name/description/location, APDU options,
 *  static bindings, foreign device registration and the ReinitializeDevice
 *  / DeviceCommunicationControl password (bacnet.password) immediately;
 *  sets *reboot_required when instance, network or UDP port changed. */
int uc_bn_apply_device_cfg(bool *reboot_required);

/* ---------------------------------------------------------------------- */
/* Executor                                                                */
/* ---------------------------------------------------------------------- */

typedef void (*uc_bn_fn)(void *arg);

/** Run fn(arg) in the BACnet thread and wait for it to return. Runs inline
 *  when called from the BACnet thread. -EAGAIN if the queue is full,
 *  -ETIMEDOUT if fn did not complete in time (it will still run; arg must
 *  then remain valid, so pass K_FOREVER unless arg is static). */
int uc_bn_exec(uc_bn_fn fn, void *arg, k_timeout_t timeout);

/** Queue fn(arg) without waiting. arg must stay valid until fn runs. */
int uc_bn_post(uc_bn_fn fn, void *arg);

/** True when the caller runs in the BACnet thread. */
bool uc_bn_in_thread(void);

/* ---------------------------------------------------------------------- */
/* Local objects (thread-safe wrappers; *_locked variants for the BACnet   */
/* thread itself, e.g. from uc_io_scan())                                  */
/* ---------------------------------------------------------------------- */

/** Create an object (AI, AO, AV, BI, BO, BV, MSI, MSO, MSV) with a name and
 *  record its owner. If it exists with the same owner: 0; with another
 *  owner: -EEXIST. */
int uc_bn_obj_create(uint16_t type, uint32_t instance, const char *name,
		     uint8_t owner);
int uc_bn_obj_create_locked(uint16_t type, uint32_t instance,
			    const char *name, uint8_t owner);

/** Delete an object. owner must match (UC_OWNER_NONE deletes anything
 *  except UC_OWNER_SYSTEM objects). The owner table entry is released. */
int uc_bn_obj_delete(uint16_t type, uint32_t instance, uint8_t owner);
int uc_bn_obj_delete_locked(uint16_t type, uint32_t instance, uint8_t owner);

/** Delete every object owned by owner. */
int uc_bn_obj_delete_owned(uint8_t owner);

/** Owner of an object, UC_OWNER_NONE if unknown or absent. */
uint8_t uc_bn_obj_owner(uint16_t type, uint32_t instance);

/** Read a property through Device_Read_Property() and decode the first
 *  application value. index < 0 means BACNET_ARRAY_ALL, 0 the array size;
 *  an index on a property that is not a BACnetARRAY is -EINVAL. */
int uc_bn_prop_read(uint16_t type, uint32_t instance, uint32_t prop,
		    int32_t index, BACNET_APPLICATION_DATA_VALUE *out);
int uc_bn_prop_read_locked(uint16_t type, uint32_t instance, uint32_t prop,
			   int32_t index, BACNET_APPLICATION_DATA_VALUE *out);

/** Read a property through Device_Read_Property() and copy its encoded
 *  value (the application data of a ReadProperty-ACK: every element of an
 *  array or list) into buf. index < 0 means BACNET_ARRAY_ALL, 0 the array
 *  size; an index on a property that is not a BACnetARRAY is -EINVAL.
 *  Returns the encoded length (0 for an empty list), -ENOSPC when the
 *  value exceeds buf or one APDU, or the errors of uc_bn_prop_read(). */
int uc_bn_prop_read_encoded(uint16_t type, uint32_t instance, uint32_t prop,
			    int32_t index, uint8_t *buf, size_t size);

/** Write a property through Device_Write_Property() (same checks and
 *  priority array semantics as a WriteProperty request; an index on a
 *  property that is not a BACnetARRAY is -EINVAL). priority 0 means
 *  "no priority". A NULL value relinquishes. Does not trigger the write
 *  hook for owner == writer loops: the hook receives writer_owner. */
int uc_bn_prop_write(uint16_t type, uint32_t instance, uint32_t prop,
		     int32_t index, const BACNET_APPLICATION_DATA_VALUE *value,
		     uint8_t priority, uint8_t writer_owner);
int uc_bn_prop_write_locked(uint16_t type, uint32_t instance, uint32_t prop,
			    int32_t index,
			    const BACNET_APPLICATION_DATA_VALUE *value,
			    uint8_t priority, uint8_t writer_owner);

/** Set Present_Value of an input object (AI/BI/MSI) directly, bypassing
 *  the Out_Of_Service write protection. BACnet thread only. */
int uc_bn_input_pv_set_locked(uint16_t type, uint32_t instance, double value);

/** Set units / COV increment of an analog object. BACnet thread only. */
int uc_bn_analog_setup_locked(uint16_t type, uint32_t instance,
			      uint16_t units, double cov_increment);

struct uc_bn_obj_info {
	uint16_t type;
	uint32_t instance;
	char name[UC_NAME_MAX];
	uint8_t owner;
	bool has_pv;
	BACNET_APPLICATION_DATA_VALUE pv;
};

/** Enumerate the device's object list (including the device object).
 *  Fills up to max entries starting at offset; *total gets the count. */
int uc_bn_obj_list(size_t offset, struct uc_bn_obj_info *out, size_t max,
		   size_t *total);

/** Called (in the BACnet thread) after a successful write to a local object
 *  by a BACnet client, the management interface or an application. owner
 *  is the object's owner, writer is the writer's owner id (UC_OWNER_NONE
 *  for network clients). */
typedef void (*uc_bn_write_hook_t)(uint8_t owner, uint8_t writer,
				   uint16_t type, uint32_t instance,
				   uint32_t prop, uint8_t priority,
				   const BACNET_APPLICATION_DATA_VALUE *value);
void uc_bn_set_write_hook(uc_bn_write_hook_t hook);

/* ---------------------------------------------------------------------- */
/* Remote client (blocking, call from any thread except the BACnet thread) */
/* ---------------------------------------------------------------------- */

/** ReadProperty. Binds the device (static binding, address cache or
 *  Who-Is) first. Returns 0, -ETIMEDOUT, -EHOSTUNREACH, -EREMOTEIO,
 *  -EBUSY (no free transaction slot), -EINVAL or -ECANCELED.
 *  cancel (optional): while it is non-zero no request is started, and a
 *  caller waiting for a confirmation gives up within
 *  UC_BN_CANCEL_POLL_MS once it becomes non-zero (the request is abandoned;
 *  a late confirmation is dropped). */
#define UC_BN_CANCEL_POLL_MS 50
int uc_bn_remote_read(uint32_t device, uint16_t type, uint32_t instance,
		      uint32_t prop, int32_t index,
		      BACNET_APPLICATION_DATA_VALUE *out, uint32_t timeout_ms,
		      const atomic_t *cancel);

/** WriteProperty (value NULL tag = relinquish); cancel as above. */
int uc_bn_remote_write(uint32_t device, uint16_t type, uint32_t instance,
		       uint32_t prop, int32_t index,
		       const BACNET_APPLICATION_DATA_VALUE *value,
		       uint8_t priority, uint32_t timeout_ms,
		       const atomic_t *cancel);

/* ---------------------------------------------------------------------- */
/* COV subscriptions for applications                                      */
/* ---------------------------------------------------------------------- */

/** Callback in the BACnet thread with the (recursive) COV lock held; must
 *  not block. It may call uc_bn_cov_subscribe()/uc_bn_cov_unsubscribe*()
 *  (also for its own subscription), but must not take a lock that another
 *  thread may hold while calling a uc_bn_cov_*() function (lock order:
 *  COV lock first). Once uc_bn_cov_unsubscribe() returns in another
 *  thread, the callback of that subscription is neither running nor
 *  called again. */
typedef void (*uc_bn_cov_cb_t)(void *ctx, int sub_id, uint32_t device,
			       uint16_t type, uint32_t instance, uint32_t prop,
			       double value);

/** Subscribe to Present_Value changes. device == UC_BN_DEVICE_LOCAL or the
 *  local instance: local change detection (value compared every loop,
 *  analog objects use their COV increment, else any change). Remote:
 *  SubscribeCOV (unconfirmed notifications) renewed at lifetime/2; if the
 *  remote device rejects COV, falls back to polling every
 *  CONFIG_UC_BACNET_COV_POLL_MS. The current value is delivered once right
 *  after subscribing. Returns sub id >= 0 or -ENOSPC. */
#define UC_BN_DEVICE_LOCAL 0xFFFFFFFFu
int uc_bn_cov_subscribe(uint32_t device, uint16_t type, uint32_t instance,
			uint32_t lifetime_s, uc_bn_cov_cb_t cb, void *ctx);
int uc_bn_cov_unsubscribe(int sub_id);
/** Remove every subscription whose ctx matches. */
void uc_bn_cov_unsubscribe_ctx(void *ctx);

#ifdef __cplusplus
}
#endif

#endif /* UC_BACNET_H_ */
