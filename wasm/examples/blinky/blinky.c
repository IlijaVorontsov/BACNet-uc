/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * blinky - toggles a binary object, or a raw digital output channel,
 * every period_ms.
 *
 * Parameters (apps.json "params", all optional):
 *   type       object type, default 5 (binary-value)
 *   instance   object instance, default 1
 *   name       object name if the app creates the object, default "blinky"
 *   priority   write priority 1..16 for a commandable object (AO, BO,
 *              MSO), 0 = none (default); value objects ignore it
 *   period_ms  toggle period, 10..3600000, default 1000
 *   on, off    values written for the two states, default 1 and 0
 *   channel    raw IO channel name ("do0"): toggle the channel instead
 *              of an object (needs permission "io")
 *
 * A value object type (AV, BV, MSV) that does not exist is created and
 * owned by the app. Other types (e.g. binary-output:1 bound to a LED in
 * io.json) must exist. On a regular stop the app relinquishes its
 * priority of a commandable object, and writes "off" otherwise (value
 * object, no priority, channel).
 *
 * Permissions: bacnet.local (object mode) or io (channel mode).
 */

#include <bacnet_uc.h>
#include <uc_util.h>

UC_APP_DECLARE()

static struct {
	uint32_t type;
	uint32_t instance;
	uint32_t priority;
	int32_t channel; /* >= 0: raw channel mode */
	double on;
	double off;
	bool state;
	uint32_t toggles;
	int32_t last_err;
} g;

static bool is_value_type(uint32_t type)
{
	return (type == UC_OBJ_ANALOG_VALUE) || (type == UC_OBJ_BINARY_VALUE) ||
	       (type == UC_OBJ_MULTI_STATE_VALUE);
}

static int32_t output(bool state)
{
	double v = state ? g.on : g.off;

	if (g.channel >= 0) {
		return uc_io_write(g.channel, v);
	}
	return uc_pv_write(g.type, g.instance, v, g.priority);
}

static int32_t setup_object(void)
{
	char name[UC_PARAM_VALUE_MAX + 1] = "blinky";
	double v;
	int32_t rc;

	(void)uc_param_u32("type", 0, UC_OBJ_TYPE_MAX, UC_OBJ_BINARY_VALUE, &g.type);
	(void)uc_param_u32("instance", 0, UC_INSTANCE_MAX, 1, &g.instance);
	(void)uc_param_u32("priority", 0, 16, 0, &g.priority);
	if (uc_param_text("name", name, sizeof(name)) <= 0) {
		memcpy(name, "blinky", sizeof("blinky"));
	}

	if (is_value_type(g.type)) {
		rc = uc_obj_create_str(g.type, g.instance, name);
		if ((rc < 0) && (rc != UC_ERR_EXISTS)) {
			uc_logf(UC_LOG_ERR, "create %s:%u failed (%d)", uc_obj_type_abbr(g.type),
				(unsigned int)g.instance, (int)rc);
			return rc;
		}
	} else {
		rc = uc_pv_read(g.type, g.instance, &v);
		if (rc < 0) {
			uc_logf(UC_LOG_ERR, "%s:%u not available (%d)", uc_obj_type_abbr(g.type),
				(unsigned int)g.instance, (int)rc);
			return rc;
		}
	}
	return UC_OK;
}

UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
	char channel[16];
	uint32_t period;
	int32_t rc;

	memset(&g, 0, sizeof(g));
	g.channel = -1;
	(void)uc_param_double("on", -1e9, 1e9, 1.0, &g.on);
	(void)uc_param_double("off", -1e9, 1e9, 0.0, &g.off);
	(void)uc_param_u32("period_ms", 10, 3600000, 1000, &period);

	if (uc_param_text("channel", channel, sizeof(channel)) > 0) {
		g.channel = uc_io_find_str(channel);
		if (g.channel < 0) {
			uc_logf(UC_LOG_ERR, "channel %s not available (%d)", channel,
				(int)g.channel);
			return g.channel;
		}
		uc_logf(UC_LOG_INF, "toggling channel %s every %u ms", channel,
			(unsigned int)period);
	} else {
		rc = setup_object();
		if (rc < 0) {
			return rc;
		}
		uc_logf(UC_LOG_INF, "toggling %s:%u every %u ms (priority %u)",
			uc_obj_type_abbr(g.type), (unsigned int)g.instance, (unsigned int)period,
			(unsigned int)g.priority);
	}

	rc = uc_set_tick_period(period);
	if (rc < 0) {
		return rc;
	}
	g.last_err = output(false);
	return 0;
}

UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
	int32_t rc;

	(void)now_ms;
	g.state = !g.state;
	rc = output(g.state);
	if ((rc < 0) && (g.last_err >= 0)) {
		uc_logf(UC_LOG_WRN, "write failed (%d)", (int)rc);
	} else if ((rc >= 0) && (g.last_err < 0)) {
		uc_log_str(UC_LOG_INF, "write ok again");
	}
	g.last_err = rc;
	g.toggles++;
	uc_logf(UC_LOG_DBG, "%s (%u)", g.state ? "on" : "off", (unsigned int)g.toggles);
}

UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
	if ((g.channel < 0) && (g.priority != UC_PRIORITY_NONE) && uc_obj_is_commandable(g.type)) {
		(void)uc_prop_write_null(g.type, g.instance, UC_PROP_PRESENT_VALUE, g.priority);
	} else {
		(void)output(false);
	}
	uc_logf(UC_LOG_INF, "stopped after %u toggles", (unsigned int)g.toggles);
}
