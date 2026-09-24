/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Remote BACnet client: blocking ReadProperty/WriteProperty for threads
 * other than the BACnet thread, and the dispatch of confirmations
 * (ReadProperty-ACK, SimpleACK, Error, Reject, Abort, TSM timeout) to the
 * client slots or to the COV module.
 *
 * A request occupies a slot with its own static semaphore. The caller
 * fills the slot and waits; the BACnet thread binds the device (static
 * binding, address cache or Who-Is), sends the request, and completes the
 * slot from the confirmation handlers or on deadline. A caller that gives
 * up marks the slot abandoned; the BACnet thread then only frees it and
 * never touches the caller's memory again.
 */
#include <errno.h>
#include <string.h>

#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/spinlock.h>
#include <zephyr/sys/util.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacaddr.h"
#include "bacnet/bacapp.h"
#include "bacnet/apdu.h"
#include "bacnet/rp.h"
#include "bacnet/basic/services.h"
#include "bacnet/basic/tsm/tsm.h"

#include "uc/uc_bacnet.h"
#include "uc_bn_internal.h"

LOG_MODULE_REGISTER(uc_bn_client, CONFIG_UC_LOG_LEVEL);

/* Who-Is repetition while a device is not bound (ms). */
#define CL_WHOIS_INTERVAL_MS 1000
/* Extra time the caller waits beyond the deadline the BACnet thread
 * enforces itself (ms). */
#define CL_WAIT_MARGIN_MS 500
/* timeout_ms == 0 */
#define CL_TIMEOUT_DEFAULT_MS 5000
#define CL_TIMEOUT_MAX_MS 600000

enum cl_state {
	CL_FREE = 0,
	CL_RESERVED,  /* being filled by the caller */
	CL_NEW,       /* queued for the BACnet thread */
	CL_BINDING,   /* waiting for an address binding or a TSM slot */
	CL_WAIT,      /* request sent, waiting for the confirmation */
	CL_DONE,      /* result valid, semaphore given */
	CL_ABANDONED, /* caller gave up; the BACnet thread frees the slot */
};

struct cl_slot {
	struct k_sem sem;
	uint8_t state;
	bool write;
	bool send_failed;
	uint8_t priority;
	uint8_t invoke_id;
	uint16_t type;
	uint32_t device;
	uint32_t instance;
	uint32_t prop;
	int32_t index;
	int wlen;
	int result;
	int64_t deadline;
	int64_t next_whois;
	BACNET_APPLICATION_DATA_VALUE *out;
	BACNET_ADDRESS dest;
	unsigned int max_apdu;
	uint8_t wbuf[UC_BN_VALUE_BUF_SIZE];
};

static struct cl_slot cl_slots[CONFIG_UC_BACNET_CLIENT_SLOTS];
static struct k_spinlock cl_lock;
/* Decode scratch (BACnet thread only). */
static BACNET_APPLICATION_DATA_VALUE cl_value;

static int cl_sys_init(void)
{
	for (size_t i = 0; i < ARRAY_SIZE(cl_slots); i++) {
		k_sem_init(&cl_slots[i].sem, 0, 1);
	}

	return 0;
}

SYS_INIT(cl_sys_init, POST_KERNEL, 0);

/* ---------------------------------------------------------------------- */
/* BACnet thread side                                                      */
/* ---------------------------------------------------------------------- */

static uint8_t cl_state_get(struct cl_slot *s)
{
	k_spinlock_key_t key = k_spin_lock(&cl_lock);
	uint8_t st = s->state;

	k_spin_unlock(&cl_lock, key);

	return st;
}

/* Transition from one BACnet-thread state to another unless the caller
 * abandoned the slot in the meantime. */
static bool cl_state_move(struct cl_slot *s, uint8_t from, uint8_t to)
{
	k_spinlock_key_t key = k_spin_lock(&cl_lock);
	bool ok = (s->state == from);

	if (ok) {
		s->state = to;
	}
	k_spin_unlock(&cl_lock, key);

	return ok;
}

static void cl_complete(struct cl_slot *s, int result, const BACNET_APPLICATION_DATA_VALUE *value)
{
	k_spinlock_key_t key = k_spin_lock(&cl_lock);
	bool give = false;

	s->invoke_id = 0;
	if (s->state == CL_ABANDONED) {
		s->state = CL_FREE;
	} else if ((s->state == CL_NEW) || (s->state == CL_BINDING) || (s->state == CL_WAIT)) {
		if ((result == 0) && (value != NULL) && (s->out != NULL)) {
			*s->out = *value;
			s->out->next = NULL;
		}
		s->result = result;
		s->state = CL_DONE;
		give = true;
	}
	k_spin_unlock(&cl_lock, key);

	if (give) {
		k_sem_give(&s->sem);
	}
}

static struct cl_slot *cl_find_wait(uint8_t invoke_id, const BACNET_ADDRESS *src)
{
	if (invoke_id == 0) {
		return NULL;
	}
	for (size_t i = 0; i < ARRAY_SIZE(cl_slots); i++) {
		struct cl_slot *s = &cl_slots[i];

		if ((cl_state_get(s) == CL_WAIT) && (s->invoke_id == invoke_id) &&
		    ((src == NULL) || bacnet_address_same(&s->dest, src))) {
			return s;
		}
	}

	return NULL;
}

static void cl_send(struct cl_slot *s, uint8_t from_state)
{
	uint32_t index = (s->index < 0) ? BACNET_ARRAY_ALL : (uint32_t)s->index;
	uint16_t max_apdu = (uint16_t)MIN(s->max_apdu, (unsigned int)UINT16_MAX);
	uint8_t id;

	if (s->write) {
		id = Send_Write_Property_Request_Data_Address(
			&s->dest, max_apdu, (BACNET_OBJECT_TYPE)s->type, s->instance,
			(BACNET_PROPERTY_ID)s->prop, s->wbuf, s->wlen, s->priority, index);
	} else {
		id = Send_Read_Property_Request_Address(&s->dest, max_apdu,
							(BACNET_OBJECT_TYPE)s->type, s->instance,
							(BACNET_PROPERTY_ID)s->prop, index);
	}
	if (id == 0) {
		/* no free TSM slot or communication disabled: retry */
		s->send_failed = true;
		(void)cl_state_move(s, from_state, CL_BINDING);
		return;
	}
	s->invoke_id = id;
	if (!cl_state_move(s, from_state, CL_WAIT)) {
		/* abandoned meanwhile: the transaction is no longer wanted */
		tsm_free_invoke_id(id);
		s->invoke_id = 0;
	}
}

void uc_bn_client_tick_locked(int64_t now_ms)
{
	for (size_t i = 0; i < ARRAY_SIZE(cl_slots); i++) {
		struct cl_slot *s = &cl_slots[i];
		uint8_t st = cl_state_get(s);
		bool whois;
		int rc;

		switch (st) {
		case CL_ABANDONED:
			if (s->invoke_id != 0) {
				tsm_free_invoke_id(s->invoke_id);
			}
			cl_complete(s, -ETIMEDOUT, NULL);
			break;
		case CL_NEW:
		case CL_BINDING:
			if (now_ms >= s->deadline) {
				cl_complete(s, s->send_failed ? -EBUSY : -EHOSTUNREACH, NULL);
				break;
			}
			if (!uc_bn_datalink_up()) {
				break;
			}
			whois = (st == CL_NEW) || (now_ms >= s->next_whois);
			rc = uc_bn_bind_locked(s->device, &s->dest, &s->max_apdu, whois);
			if (rc == 0) {
				cl_send(s, st);
			} else if (rc == -EINPROGRESS) {
				if (whois) {
					s->next_whois = now_ms + CL_WHOIS_INTERVAL_MS;
				}
				(void)cl_state_move(s, st, CL_BINDING);
			} else {
				cl_complete(s, rc, NULL);
			}
			break;
		case CL_WAIT:
			if (now_ms >= s->deadline) {
				tsm_free_invoke_id(s->invoke_id);
				cl_complete(s, -ETIMEDOUT, NULL);
			}
			break;
		default:
			break;
		}
	}
}

/* ---------------------------------------------------------------------- */
/* Confirmation handlers (BACnet thread, called from apdu_handler())       */
/* ---------------------------------------------------------------------- */

static bool cl_failure(uint8_t invoke_id, const BACNET_ADDRESS *src, int err)
{
	struct cl_slot *s = cl_find_wait(invoke_id, src);

	if (s != NULL) {
		cl_complete(s, err, NULL);
		return true;
	}

	return uc_bn_cov_confirmed_result_locked(invoke_id, src, err);
}

static void cl_rp_ack_handler(uint8_t *service_request, uint16_t service_len,
			      BACNET_ADDRESS *src, BACNET_CONFIRMED_SERVICE_ACK_DATA *service_data)
{
	BACNET_READ_PROPERTY_DATA rp = { 0 };
	const BACNET_APPLICATION_DATA_VALUE *value = NULL;
	struct cl_slot *s;
	int len;

	len = rp_ack_decode_service_request(service_request, service_len, &rp);
	if ((len > 0) && (rp.application_data != NULL) && (rp.application_data_len > 0)) {
		memset(&cl_value, 0, sizeof(cl_value));
		if (bacapp_decode_application_data(rp.application_data,
						   (uint32_t)rp.application_data_len,
						   &cl_value) > 0) {
			cl_value.next = NULL;
			value = &cl_value;
		}
	}

	s = cl_find_wait(service_data->invoke_id, src);
	if (s != NULL) {
		cl_complete(s, (value != NULL) ? 0 : -EBADMSG, value);
		return;
	}
	(void)uc_bn_cov_rp_result_locked(service_data->invoke_id, src,
					 (value != NULL) ? 0 : -EBADMSG, value);
}

static void cl_wp_ack_handler(BACNET_ADDRESS *src, uint8_t invoke_id)
{
	struct cl_slot *s = cl_find_wait(invoke_id, src);

	if (s != NULL) {
		cl_complete(s, 0, NULL);
	}
}

static void cl_error_handler(BACNET_ADDRESS *src, uint8_t invoke_id,
			     BACNET_ERROR_CLASS error_class, BACNET_ERROR_CODE error_code)
{
	LOG_DBG("invoke %u: error class %u code %u", invoke_id, error_class, error_code);
	(void)cl_failure(invoke_id, src, -EREMOTEIO);
}

static void cl_abort_handler(BACNET_ADDRESS *src, uint8_t invoke_id, uint8_t abort_reason,
			     bool server)
{
	if (!server) {
		/* aborts a transaction in which this device is the server */
		return;
	}
	LOG_DBG("invoke %u: abort reason %u", invoke_id, abort_reason);
	(void)cl_failure(invoke_id, src, -EREMOTEIO);
}

static void cl_reject_handler(BACNET_ADDRESS *src, uint8_t invoke_id, uint8_t reject_reason)
{
	LOG_DBG("invoke %u: reject reason %u", invoke_id, reject_reason);
	(void)cl_failure(invoke_id, src, -EREMOTEIO);
}

/* The TSM gave up after apdu_retries: the entry is IDLE with the invoke id
 * still set. Free it when it belongs to us; other owners (the COV server
 * in h_cov.c) poll tsm_invoke_id_failed() themselves. */
static void cl_timeout_handler(uint8_t invoke_id)
{
	if (cl_failure(invoke_id, NULL, -ETIMEDOUT)) {
		tsm_free_invoke_id(invoke_id);
	}
}

void uc_bn_client_init_locked(void)
{
	/* complete Who-Is bind requests from I-Am */
	apdu_set_unconfirmed_handler(SERVICE_UNCONFIRMED_I_AM, handler_i_am_bind);
	apdu_set_confirmed_ack_handler(SERVICE_CONFIRMED_READ_PROPERTY, cl_rp_ack_handler);
	apdu_set_error_handler(SERVICE_CONFIRMED_READ_PROPERTY, cl_error_handler);
	apdu_set_confirmed_simple_ack_handler(SERVICE_CONFIRMED_WRITE_PROPERTY,
					      cl_wp_ack_handler);
	apdu_set_error_handler(SERVICE_CONFIRMED_WRITE_PROPERTY, cl_error_handler);
	apdu_set_error_handler(SERVICE_CONFIRMED_SUBSCRIBE_COV, cl_error_handler);
	apdu_set_abort_handler(cl_abort_handler);
	apdu_set_reject_handler(cl_reject_handler);
	tsm_set_timeout_handler(cl_timeout_handler);
}

/* ---------------------------------------------------------------------- */
/* Caller side                                                             */
/* ---------------------------------------------------------------------- */

static int cl_local(bool write, uint16_t type, uint32_t instance, uint32_t prop, int32_t index,
		    BACNET_APPLICATION_DATA_VALUE *out,
		    const BACNET_APPLICATION_DATA_VALUE *value, uint8_t priority)
{
	int rc;

	if (write) {
		rc = uc_bn_prop_write(type, instance, prop, index, value, priority,
				      UC_OWNER_NONE);
	} else {
		rc = uc_bn_prop_read(type, instance, prop, index, out);
	}
	/* report like a remote device would: an Error PDU */
	if ((rc == 0) || (rc == -EINVAL) || (rc == -EBUSY) || (rc == -EBADMSG)) {
		return rc;
	}
	if (rc == -EAGAIN) {
		return -EBUSY;
	}

	return -EREMOTEIO;
}

static int cl_transact(bool write, uint32_t device, uint16_t type, uint32_t instance,
		       uint32_t prop, int32_t index, BACNET_APPLICATION_DATA_VALUE *out,
		       const BACNET_APPLICATION_DATA_VALUE *value, uint8_t priority,
		       uint32_t timeout_ms)
{
	BACNET_APPLICATION_DATA_VALUE null_value = { 0 };
	struct cl_slot *s = NULL;
	k_spinlock_key_t key;
	k_timepoint_t end;
	int len = 0;

	if (uc_bn_in_thread()) {
		/* the BACnet thread would wait for itself */
		return -EINVAL;
	}
	if ((device > BACNET_MAX_INSTANCE) && (device != UC_BN_DEVICE_LOCAL)) {
		return -EINVAL;
	}
	if ((type >= MAX_BACNET_OBJECT_TYPE) || (instance > BACNET_MAX_INSTANCE) ||
	    (prop > MAX_BACNET_PROPERTY_ID) || (priority > BACNET_MAX_PRIORITY)) {
		return -EINVAL;
	}
	if (!write && (out == NULL)) {
		return -EINVAL;
	}
	if (uc_bn_device_is_local(device)) {
		return cl_local(write, type, instance, prop, index, out, value, priority);
	}
	if (device == BACNET_MAX_INSTANCE) {
		return -EINVAL;
	}
	if (!uc_bn_started()) {
		return -EHOSTUNREACH;
	}
	if (timeout_ms == 0) {
		timeout_ms = CL_TIMEOUT_DEFAULT_MS;
	}
	timeout_ms = MIN(timeout_ms, (uint32_t)CL_TIMEOUT_MAX_MS);

	if (write) {
		if (value == NULL) {
			null_value.tag = BACNET_APPLICATION_TAG_NULL;
			value = &null_value;
		}
		if (value->context_specific) {
			return -EINVAL;
		}
		len = bacapp_encode_application_data(NULL, value);
		if ((len <= 0) || (len > UC_BN_VALUE_BUF_SIZE)) {
			return -EINVAL;
		}
	}

	key = k_spin_lock(&cl_lock);
	for (size_t i = 0; i < ARRAY_SIZE(cl_slots); i++) {
		if (cl_slots[i].state == CL_FREE) {
			s = &cl_slots[i];
			s->state = CL_RESERVED;
			break;
		}
	}
	k_spin_unlock(&cl_lock, key);
	if (s == NULL) {
		return -EBUSY;
	}

	s->write = write;
	s->send_failed = false;
	s->priority = priority;
	s->invoke_id = 0;
	s->device = device;
	s->type = type;
	s->instance = instance;
	s->prop = prop;
	s->index = index;
	s->out = out;
	s->result = -EIO;
	s->deadline = k_uptime_get() + timeout_ms;
	s->next_whois = 0;
	s->max_apdu = 0;
	memset(&s->dest, 0, sizeof(s->dest));
	s->wlen = 0;
	if (write) {
		s->wlen = bacapp_encode_application_data(s->wbuf, value);
	}
	/* drop a late give of a previous user of this slot */
	k_sem_reset(&s->sem);

	key = k_spin_lock(&cl_lock);
	s->state = CL_NEW;
	k_spin_unlock(&cl_lock, key);
	uc_bn_wake();

	end = sys_timepoint_calc(K_MSEC(timeout_ms + CL_WAIT_MARGIN_MS));
	for (;;) {
		int rc = k_sem_take(&s->sem, sys_timepoint_timeout(end));

		key = k_spin_lock(&cl_lock);
		if (s->state == CL_DONE) {
			int result = s->result;

			s->out = NULL;
			s->state = CL_FREE;
			k_spin_unlock(&cl_lock, key);
			return result;
		}
		if (rc != 0) {
			/* the BACnet thread did not answer in time */
			s->out = NULL;
			s->state = CL_ABANDONED;
			k_spin_unlock(&cl_lock, key);
			uc_bn_wake();
			return -ETIMEDOUT;
		}
		k_spin_unlock(&cl_lock, key);
	}
}

int uc_bn_remote_read(uint32_t device, uint16_t type, uint32_t instance, uint32_t prop,
		      int32_t index, BACNET_APPLICATION_DATA_VALUE *out, uint32_t timeout_ms)
{
	return cl_transact(false, device, type, instance, prop, index, out, NULL, 0, timeout_ms);
}

int uc_bn_remote_write(uint32_t device, uint16_t type, uint32_t instance, uint32_t prop,
		       int32_t index, const BACNET_APPLICATION_DATA_VALUE *value,
		       uint8_t priority, uint32_t timeout_ms)
{
	return cl_transact(true, device, type, instance, prop, index, NULL, value, priority,
			   timeout_ms);
}
