/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * uc_point.h - header-only "input point" helper: follows the
 * Present_Value of a local or remote object through a COV subscription and
 * falls back to polling.
 *
 *   - uc_point_start() subscribes (lifetime 300 s, renewed by the host). The
 *     host delivers the current value right after subscribing.
 *   - Every notification (uc_point_on_cov) refreshes the value.
 *   - uc_point_tick() reads the value (uc_prop_read / uc_remote_read) when
 *     none arrived for poll_ms: COV never set up, notifications lost, or
 *     the value does not change (no notifications). A failed subscription
 *     is retried every retry_ms.
 *   - uc_point_fresh() tells whether the last good value is recent enough.
 *
 * Usage:
 *   static struct uc_point temp;
 *   init:   uc_point_setup(&temp, dev, UC_OBJ_ANALOG_INPUT, 1, 10000);
 *           uc_point_start(&temp, uc_uptime_ms());
 *   on_cov: if (uc_point_on_cov(&temp, sub_id, type, instance, value,
 *                               uc_uptime_ms())) { ... }
 *   tick:   uc_point_tick(&temp, now_ms);
 *           if (uc_point_fresh(&temp, now_ms, 60000)) use(temp.value);
 */
#ifndef UC_POINT_H
#define UC_POINT_H

#include <stdbool.h>
#include <stdint.h>

#include "bacnet_uc.h"
#include "uc_util.h"

#ifdef __cplusplus
extern "C" {
#endif

#define UC_POINT_LIFETIME_S       300u
#define UC_POINT_RETRY_MS         30000u
#define UC_POINT_READ_TIMEOUT_MS  2000u

struct uc_point {
	/* configuration (uc_point_setup, may be changed before start) */
	uint32_t device;     /**< UC_DEVICE_LOCAL or a device instance */
	uint32_t type;
	uint32_t instance;
	uint32_t poll_ms;    /**< poll after this long without a value, 0 = never */
	uint32_t retry_ms;   /**< resubscribe period after a failure, 0 = never */
	uint32_t timeout_ms; /**< remote read timeout */
	uint32_t lifetime_s; /**< subscription lifetime */
	/* state */
	int32_t sub_id;      /**< >= 0 while subscribed */
	int32_t sub_err;     /**< last subscribe error, 0 = ok */
	int32_t read_err;    /**< last poll error, 0 = ok */
	bool valid;          /**< value holds a good value */
	double value;
	uint64_t updated_ms; /**< time of the last good value */
	uint64_t next_poll_ms;
	uint64_t next_sub_ms;
	uint32_t covs;       /**< notifications received */
	uint32_t polls;      /**< successful polls */
};

static inline void uc_point_setup(struct uc_point *p, uint32_t device, uint32_t type,
				  uint32_t instance, uint32_t poll_ms)
{
	memset(p, 0, sizeof(*p));
	p->device = device;
	p->type = type;
	p->instance = instance;
	p->poll_ms = poll_ms;
	p->retry_ms = UC_POINT_RETRY_MS;
	p->timeout_ms = UC_POINT_READ_TIMEOUT_MS;
	p->lifetime_s = UC_POINT_LIFETIME_S;
	p->sub_id = -1;
}

static inline int32_t uc_point_subscribe(struct uc_point *p, uint64_t now)
{
	int32_t rc = uc_cov_subscribe(p->device, p->type, p->instance, p->lifetime_s);

	if (rc >= 0) {
		p->sub_id = rc;
		p->sub_err = 0;
	} else {
		p->sub_id = -1;
		p->sub_err = rc;
		p->next_sub_ms = now + p->retry_ms;
	}
	return rc;
}

/** Subscribe. Without a subscription the first poll is due immediately.
 *  Returns the subscription id or the subscribe error. */
static inline int32_t uc_point_start(struct uc_point *p, uint64_t now)
{
	int32_t rc = uc_point_subscribe(p, now);

	p->next_poll_ms = (rc >= 0) ? now + p->poll_ms : now;
	return rc;
}

/** Feed a notification. Returns true when it belongs to this point. */
static inline bool uc_point_on_cov(struct uc_point *p, int32_t sub_id, uint32_t type,
				   uint32_t instance, double value, uint64_t now)
{
	if ((p->sub_id < 0) || (sub_id != p->sub_id) || (type != p->type) ||
	    (instance != p->instance)) {
		return false;
	}
	p->value = value;
	p->valid = true;
	p->updated_ms = now;
	p->read_err = 0;
	p->next_poll_ms = now + p->poll_ms;
	p->covs++;
	return true;
}

/**
 * Periodic work: resubscribe when due, poll when no value arrived for
 * poll_ms. Returns 1 when a poll delivered a value, 0 when nothing was
 * due and the (negative) read error of a failed poll.
 */
static inline int32_t uc_point_tick(struct uc_point *p, uint64_t now)
{
	double v;
	int32_t rc;

	if ((p->sub_id < 0) && (p->retry_ms != 0u) && (now >= p->next_sub_ms)) {
		(void)uc_point_subscribe(p, now);
	}
	if ((p->poll_ms == 0u) || (now < p->next_poll_ms)) {
		return 0;
	}
	p->next_poll_ms = now + p->poll_ms;

	rc = uc_pv_read_dev(p->device, p->type, p->instance, &v, p->timeout_ms);
	if (rc < 0) {
		p->read_err = rc;
		return rc;
	}
	p->value = v;
	p->valid = true;
	p->updated_ms = now;
	p->read_err = 0;
	p->polls++;
	return 1;
}

/** True when a good value is at most max_age_ms old (0: any age). */
static inline bool uc_point_fresh(const struct uc_point *p, uint64_t now, uint32_t max_age_ms)
{
	if (!p->valid) {
		return false;
	}
	return (max_age_ms == 0u) || (now < p->updated_ms) ||
	       ((now - p->updated_ms) <= (uint64_t)max_age_ms);
}

/** Cancel the subscription (the host also cancels it when the app stops). */
static inline void uc_point_stop(struct uc_point *p)
{
	if (p->sub_id >= 0) {
		(void)uc_cov_unsubscribe(p->sub_id);
		p->sub_id = -1;
	}
}

#ifdef __cplusplus
}
#endif

#endif /* UC_POINT_H */
