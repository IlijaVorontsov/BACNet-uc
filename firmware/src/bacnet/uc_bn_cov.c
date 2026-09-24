/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Present_Value change subscriptions for applications.
 *
 * Local objects: the value is compared every BACnet loop (analog objects
 * with their COV_Increment when readable, other objects on any change).
 *
 * Remote objects: SubscribeCOV with unconfirmed notifications and the slot
 * index as subscriber process identifier, renewed at lifetime/2. Incoming
 * (un)confirmed COV notifications are matched by process identifier,
 * device and object. When the device answers SubscribeCOV with an
 * Error/Reject/Abort the subscription is polled with ReadProperty every
 * CONFIG_UC_BACNET_COV_POLL_MS; after a timeout it is polled and
 * SubscribeCOV is retried periodically.
 *
 * The current value is delivered once after subscribing, later only
 * changes. Callbacks run in the BACnet thread with cov_lock held, so once
 * uc_bn_cov_unsubscribe() returns, the callback of that subscription is
 * neither running nor called again.
 */
#include <errno.h>
#include <math.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacaddr.h"
#include "bacnet/bacapp.h"
#include "bacnet/apdu.h"
#include "bacnet/cov.h"
#include "bacnet/basic/services.h"
#include "bacnet/basic/tsm/tsm.h"

#include "uc/uc_bacnet.h"
#include "uc/uc_common.h"
#include "uc_bn_internal.h"

LOG_MODULE_REGISTER(uc_bn_cov, CONFIG_UC_LOG_LEVEL);

/* Address binding retry period (ms). */
#define COV_BIND_RETRY_MS 2000
/* Read the value when no initial notification arrived after a successful
 * SubscribeCOV within this time (ms). */
#define COV_INITIAL_READ_MS 2000
/* Retry SubscribeCOV after a timeout fallback to polling (ms). */
#define COV_RESUBSCRIBE_MS 60000
/* Shortest remote lifetime (s): renewals at most every 5 s. */
#define COV_LIFETIME_MIN_S 10u
/* Invoke ids of requests whose subscription is gone. */
#define COV_ORPHANS_MAX 8

enum cov_state {
	COV_FREE = 0,
	COV_NEW,         /* set by uc_bn_cov_subscribe() */
	COV_LOCAL,       /* local change detection */
	COV_BINDING,     /* remote: waiting for an address binding */
	COV_SUBSCRIBING, /* remote: first SubscribeCOV sent */
	COV_SUBSCRIBED,  /* remote: confirmed, renewed at lifetime/2 */
	COV_POLLING,     /* remote: ReadProperty fallback */
	COV_CANCEL,      /* set by unsubscribe, finalised by the BACnet thread */
};

struct cov_sub {
	uint8_t state;
	bool remote;
	bool have_value;
	bool confirmed;    /* the remote device holds our subscription */
	bool poll_forever; /* the remote device refused COV */
	uint8_t sub_invoke;
	uint8_t read_invoke;
	uint16_t type;
	uint32_t device;
	uint32_t instance;
	uint32_t lifetime_s;
	uc_bn_cov_cb_t cb;
	void *ctx;
	double last;
	int64_t next_ms;
	int64_t resubscribe_ms;
	int64_t initial_read_ms;
	BACNET_ADDRESS dest;
	unsigned int max_apdu;
};

BUILD_ASSERT(CONFIG_UC_BACNET_COV_SUBS_MAX > 0, "at least one COV subscription");

/* Protects cov_subs. Recursive: a callback may unsubscribe. */
static K_MUTEX_DEFINE(cov_lock);
static struct cov_sub cov_subs[CONFIG_UC_BACNET_COV_SUBS_MAX];

/* BACnet thread only. */
static uint8_t cov_orphans[COV_ORPHANS_MAX];
static BACNET_APPLICATION_DATA_VALUE cov_value;
static BACNET_COV_NOTIFICATION cov_ucov_node;
static BACNET_COV_NOTIFICATION cov_ccov_node;

/* ---------------------------------------------------------------------- */
/* Helpers                                                                 */
/* ---------------------------------------------------------------------- */

static bool cov_same(double a, double b)
{
	if (isnan(a) || isnan(b)) {
		return isnan(a) && isnan(b);
	}

	return a == b;
}

static int cov_id(const struct cov_sub *s)
{
	return (int)(s - cov_subs);
}

static void cov_deliver(struct cov_sub *s, double value)
{
	uint32_t device = s->remote ? s->device : uc_bn_local_instance();

	s->last = value;
	s->have_value = true;
	if (s->cb != NULL) {
		s->cb(s->ctx, cov_id(s), device, s->type, s->instance, PROP_PRESENT_VALUE, value);
	}
}

static void cov_orphan_add(uint8_t invoke_id)
{
	if (invoke_id == 0) {
		return;
	}
	for (size_t i = 0; i < ARRAY_SIZE(cov_orphans); i++) {
		if (cov_orphans[i] == 0) {
			cov_orphans[i] = invoke_id;
			return;
		}
	}
	/* table full: give the TSM entry up now, a late reply is ignored */
	tsm_free_invoke_id(invoke_id);
}

static bool cov_orphan_take(uint8_t invoke_id)
{
	if (invoke_id == 0) {
		return false;
	}
	for (size_t i = 0; i < ARRAY_SIZE(cov_orphans); i++) {
		if (cov_orphans[i] == invoke_id) {
			cov_orphans[i] = 0;
			return true;
		}
	}

	return false;
}

static bool cov_src_match(const struct cov_sub *s, const BACNET_ADDRESS *src)
{
	return (src == NULL) || bacnet_address_same(&s->dest, src);
}

static uint8_t cov_send_subscribe(struct cov_sub *s, bool cancel)
{
	BACNET_SUBSCRIBE_COV_DATA data;

	memset(&data, 0, sizeof(data));
	data.subscriberProcessIdentifier = (uint32_t)cov_id(s);
	data.monitoredObjectIdentifier.type = (BACNET_OBJECT_TYPE)s->type;
	data.monitoredObjectIdentifier.instance = s->instance;
	data.cancellationRequest = cancel;
	data.issueConfirmedNotifications = false;
	data.lifetime = s->lifetime_s;

	return Send_COV_Subscribe_Address(&s->dest, (uint16_t)MIN(s->max_apdu, UINT16_MAX),
					  &data);
}

static void cov_subscribe_start(struct cov_sub *s, int64_t now)
{
	uint8_t id = cov_send_subscribe(s, false);

	if (id == 0) {
		/* no TSM slot: try again shortly */
		s->next_ms = now + CONFIG_UC_BACNET_POLL_MS * 10;
		return;
	}
	s->sub_invoke = id;
}

static void cov_poll_start(struct cov_sub *s)
{
	uint8_t id = Send_Read_Property_Request_Address(
		&s->dest, (uint16_t)MIN(s->max_apdu, UINT16_MAX), (BACNET_OBJECT_TYPE)s->type,
		s->instance, PROP_PRESENT_VALUE, BACNET_ARRAY_ALL);

	s->read_invoke = id;
}

static void cov_fallback_to_polling(struct cov_sub *s, int64_t now, bool forever)
{
	if (s->state != COV_POLLING) {
		LOG_INF("sub %d: device %u %u:%u polled every %d ms (%s)", cov_id(s), s->device,
			s->type, s->instance, CONFIG_UC_BACNET_COV_POLL_MS,
			forever ? "COV refused" : "no answer");
	}
	s->state = COV_POLLING;
	s->confirmed = false;
	s->poll_forever = s->poll_forever || forever;
	s->next_ms = now;
	s->resubscribe_ms = now + COV_RESUBSCRIBE_MS;
}

/* ---------------------------------------------------------------------- */
/* Local change detection                                                  */
/* ---------------------------------------------------------------------- */

static int cov_local_value(const struct cov_sub *s, double *value)
{
	int rc = uc_bn_prop_read_locked(s->type, s->instance, PROP_PRESENT_VALUE, -1, &cov_value);

	if (rc != 0) {
		return rc;
	}

	return uc_value_to_double(&cov_value, value);
}

static bool cov_local_changed(const struct cov_sub *s, double value)
{
	double increment;

	if (cov_same(value, s->last)) {
		return false;
	}
	if ((s->type == OBJECT_ANALOG_INPUT) || (s->type == OBJECT_ANALOG_OUTPUT) ||
	    (s->type == OBJECT_ANALOG_VALUE)) {
		if ((uc_bn_prop_read_locked(s->type, s->instance, PROP_COV_INCREMENT, -1,
					    &cov_value) == 0) &&
		    (uc_value_to_double(&cov_value, &increment) == 0) && (increment > 0.0) &&
		    !isnan(value) && !isnan(s->last)) {
			return fabs(value - s->last) >= increment;
		}
	}

	return true;
}

static void cov_local_tick(struct cov_sub *s)
{
	double value;

	if (cov_local_value(s, &value) != 0) {
		/* object absent (yet) or not numeric */
		return;
	}
	if (!s->have_value || cov_local_changed(s, value)) {
		cov_deliver(s, value);
	}
}

/* ---------------------------------------------------------------------- */
/* Remote state machine                                                    */
/* ---------------------------------------------------------------------- */

static void cov_remote_tick(struct cov_sub *s, int64_t now)
{
	switch (s->state) {
	case COV_BINDING:
		if (!uc_bn_datalink_up() || (now < s->next_ms)) {
			break;
		}
		if (uc_bn_bind_locked(s->device, &s->dest, &s->max_apdu, true) != 0) {
			s->next_ms = now + COV_BIND_RETRY_MS;
			break;
		}
		s->state = COV_SUBSCRIBING;
		s->next_ms = now;
		__fallthrough;
	case COV_SUBSCRIBING:
		if ((s->sub_invoke == 0) && (now >= s->next_ms)) {
			cov_subscribe_start(s, now);
		}
		break;
	case COV_SUBSCRIBED:
		if ((s->sub_invoke == 0) && (now >= s->next_ms)) {
			/* renewal */
			cov_subscribe_start(s, now);
		}
		if (!s->have_value && (s->read_invoke == 0) && (now >= s->initial_read_ms)) {
			/* the device sent no initial notification */
			s->initial_read_ms = now + COV_INITIAL_READ_MS;
			cov_poll_start(s);
		}
		break;
	case COV_POLLING:
		if ((s->read_invoke == 0) && (now >= s->next_ms)) {
			s->next_ms = now + CONFIG_UC_BACNET_COV_POLL_MS;
			cov_poll_start(s);
		}
		if (!s->poll_forever && (s->sub_invoke == 0) && (now >= s->resubscribe_ms)) {
			s->resubscribe_ms = now + COV_RESUBSCRIBE_MS;
			cov_subscribe_start(s, now);
		}
		break;
	default:
		break;
	}
}

static void cov_finalise_cancel(struct cov_sub *s)
{
	if (s->remote && uc_bn_datalink_up()) {
		if (s->confirmed) {
			/* tell the device; the answer is consumed as orphan */
			cov_orphan_add(cov_send_subscribe(s, true));
		}
		cov_orphan_add(s->sub_invoke);
		cov_orphan_add(s->read_invoke);
	}
	memset(s, 0, sizeof(*s));
}

void uc_bn_cov_tick_locked(int64_t now_ms)
{
	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(cov_subs); i++) {
		struct cov_sub *s = &cov_subs[i];

		switch (s->state) {
		case COV_FREE:
			break;
		case COV_CANCEL:
			cov_finalise_cancel(s);
			break;
		case COV_NEW:
			s->remote = !uc_bn_device_is_local(s->device);
			if (!s->remote) {
				s->state = COV_LOCAL;
				cov_local_tick(s);
			} else {
				s->state = COV_BINDING;
				s->next_ms = now_ms;
				cov_remote_tick(s, now_ms);
			}
			break;
		case COV_LOCAL:
			cov_local_tick(s);
			break;
		default:
			cov_remote_tick(s, now_ms);
			break;
		}
	}
	(void)k_mutex_unlock(&cov_lock);
}

/* ---------------------------------------------------------------------- */
/* Confirmations and notifications (BACnet thread)                         */
/* ---------------------------------------------------------------------- */

bool uc_bn_cov_confirmed_result_locked(uint8_t invoke_id, const BACNET_ADDRESS *src, int err)
{
	int64_t now = k_uptime_get();
	bool found = false;

	if (invoke_id == 0) {
		return false;
	}
	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	for (size_t i = 0; (i < ARRAY_SIZE(cov_subs)) && !found; i++) {
		struct cov_sub *s = &cov_subs[i];

		if (!s->remote || !cov_src_match(s, src)) {
			continue;
		}
		if (s->sub_invoke == invoke_id) {
			found = true;
			s->sub_invoke = 0;
			if (err == 0) {
				if (s->state != COV_SUBSCRIBED) {
					LOG_DBG("sub %d: SubscribeCOV accepted by %u", cov_id(s),
						s->device);
					s->initial_read_ms = now + COV_INITIAL_READ_MS;
				}
				s->state = COV_SUBSCRIBED;
				s->confirmed = true;
				s->next_ms = now + (int64_t)s->lifetime_s * MSEC_PER_SEC / 2;
			} else {
				cov_fallback_to_polling(s, now, err != -ETIMEDOUT);
			}
		} else if (s->read_invoke == invoke_id) {
			/* failed poll (success arrives as ReadProperty-ACK) */
			found = true;
			s->read_invoke = 0;
		}
	}
	(void)k_mutex_unlock(&cov_lock);

	return found || cov_orphan_take(invoke_id);
}

bool uc_bn_cov_rp_result_locked(uint8_t invoke_id, const BACNET_ADDRESS *src, int err,
				const BACNET_APPLICATION_DATA_VALUE *value)
{
	bool found = false;

	if (invoke_id == 0) {
		return false;
	}
	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	for (size_t i = 0; (i < ARRAY_SIZE(cov_subs)) && !found; i++) {
		struct cov_sub *s = &cov_subs[i];
		double v;

		if (!s->remote || (s->read_invoke != invoke_id) || !cov_src_match(s, src)) {
			continue;
		}
		found = true;
		s->read_invoke = 0;
		if ((err != 0) || (value == NULL) || (uc_value_to_double(value, &v) != 0)) {
			LOG_DBG("sub %d: poll of %u failed (%d)", cov_id(s), s->device, err);
			continue;
		}
		if ((s->state == COV_CANCEL) || (s->state == COV_FREE)) {
			continue;
		}
		if (!s->have_value || !cov_same(v, s->last)) {
			cov_deliver(s, v);
		}
	}
	(void)k_mutex_unlock(&cov_lock);

	return found || cov_orphan_take(invoke_id);
}

static void cov_notification(BACNET_COV_DATA *data)
{
	const BACNET_PROPERTY_VALUE *pv;
	struct cov_sub *s;
	double v;

	if ((data == NULL) || (data->subscriberProcessIdentifier >= ARRAY_SIZE(cov_subs))) {
		return;
	}
	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	s = &cov_subs[data->subscriberProcessIdentifier];
	if (s->remote &&
	    ((s->state == COV_SUBSCRIBING) || (s->state == COV_SUBSCRIBED) ||
	     (s->state == COV_POLLING)) &&
	    (s->device == data->initiatingDeviceIdentifier) &&
	    (s->type == (uint16_t)data->monitoredObjectIdentifier.type) &&
	    (s->instance == data->monitoredObjectIdentifier.instance)) {
		for (pv = data->listOfValues; pv != NULL; pv = pv->next) {
			if (pv->propertyIdentifier != PROP_PRESENT_VALUE) {
				continue;
			}
			if ((uc_value_to_double(&pv->value, &v) == 0) &&
			    (!s->have_value || !cov_same(v, s->last))) {
				cov_deliver(s, v);
			}
			break;
		}
	}
	(void)k_mutex_unlock(&cov_lock);
}

static void cov_subscribe_ack_handler(BACNET_ADDRESS *src, uint8_t invoke_id)
{
	(void)uc_bn_cov_confirmed_result_locked(invoke_id, src, 0);
}

void uc_bn_cov_init_locked(void)
{
	memset(cov_orphans, 0, sizeof(cov_orphans));
	cov_ucov_node.callback = cov_notification;
	handler_ucov_notification_add(&cov_ucov_node);
	cov_ccov_node.callback = cov_notification;
	handler_ccov_notification_add(&cov_ccov_node);
	apdu_set_unconfirmed_handler(SERVICE_UNCONFIRMED_COV_NOTIFICATION,
				     handler_ucov_notification);
	apdu_set_confirmed_handler(SERVICE_CONFIRMED_COV_NOTIFICATION, handler_ccov_notification);
	apdu_set_confirmed_simple_ack_handler(SERVICE_CONFIRMED_SUBSCRIBE_COV,
					      cov_subscribe_ack_handler);
	/* Error for SubscribeCOV, Reject, Abort and TSM timeouts reach
	 * uc_bn_cov_confirmed_result_locked() through uc_bn_client.c. */
}

/* ---------------------------------------------------------------------- */
/* Public API (any thread)                                                 */
/* ---------------------------------------------------------------------- */

int uc_bn_cov_subscribe(uint32_t device, uint16_t type, uint32_t instance, uint32_t lifetime_s,
			uc_bn_cov_cb_t cb, void *ctx)
{
	int id = -ENOSPC;

	if ((cb == NULL) || (type >= MAX_BACNET_OBJECT_TYPE) || (instance >= BACNET_MAX_INSTANCE)) {
		return -EINVAL;
	}
	if ((device != UC_BN_DEVICE_LOCAL) && (device >= BACNET_MAX_INSTANCE)) {
		return -EINVAL;
	}
	if (lifetime_s == 0) {
		lifetime_s = UC_BN_COV_LIFETIME_DEFAULT_S;
	}
	lifetime_s = MAX(lifetime_s, COV_LIFETIME_MIN_S);

	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(cov_subs); i++) {
		struct cov_sub *s = &cov_subs[i];

		if (s->state != COV_FREE) {
			continue;
		}
		memset(s, 0, sizeof(*s));
		s->device = device;
		s->type = type;
		s->instance = instance;
		s->lifetime_s = lifetime_s;
		s->cb = cb;
		s->ctx = ctx;
		s->state = COV_NEW;
		id = (int)i;
		break;
	}
	(void)k_mutex_unlock(&cov_lock);

	if (id >= 0) {
		uc_bn_wake();
	}

	return id;
}

int uc_bn_cov_unsubscribe(int sub_id)
{
	struct cov_sub *s;
	int rc = 0;

	if ((sub_id < 0) || (sub_id >= (int)ARRAY_SIZE(cov_subs))) {
		return -EINVAL;
	}
	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	s = &cov_subs[sub_id];
	if ((s->state == COV_FREE) || (s->state == COV_CANCEL)) {
		rc = -ENOENT;
	} else if (s->state == COV_NEW) {
		/* never seen by the BACnet thread */
		memset(s, 0, sizeof(*s));
	} else {
		s->cb = NULL;
		s->ctx = NULL;
		s->state = COV_CANCEL;
	}
	(void)k_mutex_unlock(&cov_lock);
	uc_bn_wake();

	return rc;
}

void uc_bn_cov_unsubscribe_ctx(void *ctx)
{
	(void)k_mutex_lock(&cov_lock, K_FOREVER);
	for (size_t i = 0; i < ARRAY_SIZE(cov_subs); i++) {
		struct cov_sub *s = &cov_subs[i];

		if ((s->state == COV_FREE) || (s->state == COV_CANCEL) || (s->ctx != ctx)) {
			continue;
		}
		if (s->state == COV_NEW) {
			memset(s, 0, sizeof(*s));
		} else {
			s->cb = NULL;
			s->ctx = NULL;
			s->state = COV_CANCEL;
		}
	}
	(void)k_mutex_unlock(&cov_lock);
	uc_bn_wake();
}
