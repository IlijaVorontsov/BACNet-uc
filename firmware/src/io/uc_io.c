/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * IO channels from the devicetree catalog (compatible "uc,io-channels",
 * dts/bindings/uc,io-channels.yaml) and their binding to BACnet objects
 * (io.json).
 *
 * Channel table: generated at build time from every status "okay" catalog
 * node; the channel id is the index in that table, the name is the child
 * node name. The hardware of a channel follows its kind unless it is
 * "simulated": di/do -> GPIO, ai -> ADC (value in mV), ao -> PWM (duty
 * 0..100 %). Channels whose driver subsystem is not built (CONFIG_GPIO,
 * CONFIG_ADC, CONFIG_PWM) fail with -EIO at run time.
 *
 * Values and overrides (per channel, protected by io_lock):
 *   value        simulated value; outputs: commanded value (last write or
 *                BACnet Present_Value)
 *   force_value  override while forced: inputs return it instead of the
 *                hardware, outputs drive it and only record commands
 * On simulated inputs uc_io_force() also sets the simulated value, which
 * therefore persists after uc_io_release(). Outputs (hardware or simulated)
 * return to the commanded value on release; Present_Value changes while
 * forced are recorded and take effect then.
 *
 * Points (io.json bindings) are owned by the BACnet thread: created in
 * uc_io_apply_config_locked(), serviced in uc_io_scan(). ADC conversions
 * run outside io_lock (the ADC driver serialises itself), so a slow
 * conversion in one thread does not block the others on io_lock.
 */

#include <errno.h>
#include <math.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <zephyr/devicetree.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#if defined(CONFIG_GPIO)
#include <zephyr/drivers/gpio.h>
#endif
#if defined(CONFIG_ADC)
#include <zephyr/drivers/adc.h>
#endif
#if defined(CONFIG_PWM)
#include <zephyr/drivers/pwm.h>
#endif

#include "bacnet/bacdef.h"
#include "bacnet/bacenum.h"
#include "bacnet/bacapp.h"
#include "bacnet/basic/object/ai.h"
#include "bacnet/basic/object/ao.h"
#include "bacnet/basic/object/av.h"
#include "bacnet/basic/object/bi.h"
#include "bacnet/basic/object/bo.h"
#include "bacnet/basic/object/bv.h"
#include "bacnet/basic/object/ms-input.h"

#include "uc/uc_bacnet.h"
#include "uc/uc_common.h"
#include "uc/uc_config.h"
#include "uc/uc_io.h"

LOG_MODULE_REGISTER(uc_io, CONFIG_UC_LOG_LEVEL);

/* ---------------------------------------------------------------------- */
/* Channel table from the devicetree                                       */
/* ---------------------------------------------------------------------- */

#if DT_HAS_COMPAT_STATUS_OKAY(uc_io_channels)
#define IO_BOARD_NAME                                                                    \
	DT_PROP_OR(DT_COMPAT_GET_ANY_STATUS_OKAY(uc_io_channels), board_name, CONFIG_BOARD)
#else
#define IO_BOARD_NAME CONFIG_BOARD
#endif

struct io_chan {
	const char *name;
	const char *desc;
	uint8_t kind; /* enum uc_io_kind */
	uint8_t hw;   /* enum uc_io_hw */
	int32_t initial;
#if defined(CONFIG_GPIO)
	struct gpio_dt_spec gpio;
#endif
#if defined(CONFIG_ADC)
	struct adc_dt_spec adc;
#endif
#if defined(CONFIG_PWM)
	struct pwm_dt_spec pwm;
#endif
};

/* Hardware of a channel: simulated, else given by its kind. The enum order
 * of the binding's "kind" (di, do, ai, ao) equals enum uc_io_kind.
 */
#define IO_CHAN_KIND(node) DT_ENUM_IDX(node, kind)
#define IO_CHAN_HW(node)                                                                 \
	(DT_PROP(node, simulated)             ? UC_IO_HW_SIM                             \
	 : (IO_CHAN_KIND(node) <= UC_IO_DO)   ? UC_IO_HW_GPIO                            \
	 : (IO_CHAN_KIND(node) == UC_IO_AI)   ? UC_IO_HW_ADC                             \
					      : UC_IO_HW_PWM)

/* Initialiser of a hardware spec: only for non-simulated channels that
 * have the property (a simulated channel never references a controller).
 */
#define IO_SPEC_IF(node, prop, spec)                                                     \
	COND_CODE_1(DT_PROP(node, simulated), ({0}),                                     \
		    (COND_CODE_1(DT_NODE_HAS_PROP(node, prop), spec, ({0}))))

#if defined(CONFIG_GPIO)
#define IO_GPIO_INIT(node) .gpio = IO_SPEC_IF(node, gpios, (GPIO_DT_SPEC_GET(node, gpios))),
#else
#define IO_GPIO_INIT(node)
#endif
#if defined(CONFIG_ADC)
#define IO_ADC_INIT(node) .adc = IO_SPEC_IF(node, io_channels, (ADC_DT_SPEC_GET(node))),
#else
#define IO_ADC_INIT(node)
#endif
#if defined(CONFIG_PWM)
#define IO_PWM_INIT(node) .pwm = IO_SPEC_IF(node, pwms, (PWM_DT_SPEC_GET(node))),
#else
#define IO_PWM_INIT(node)
#endif

#define IO_CHAN_ENTRY(node)                                                              \
	{                                                                                \
		.name = DT_NODE_FULL_NAME(node),                                         \
		.desc = DT_PROP_OR(node, description, ""),                               \
		.kind = IO_CHAN_KIND(node),                                              \
		.hw = IO_CHAN_HW(node),                                                  \
		.initial = DT_PROP(node, initial_value),                                 \
		IO_GPIO_INIT(node) IO_ADC_INIT(node) IO_PWM_INIT(node)                   \
	},

#define IO_CATALOG_ENTRIES(inst) DT_FOREACH_CHILD_STATUS_OKAY(inst, IO_CHAN_ENTRY)

/* Build-time checks of the catalog: name length and hardware per kind. */
#define IO_CHAN_HW_OK(node)                                                              \
	(DT_PROP(node, simulated) ||                                                     \
	 ((IO_CHAN_KIND(node) <= UC_IO_DO) && DT_NODE_HAS_PROP(node, gpios)) ||          \
	 ((IO_CHAN_KIND(node) == UC_IO_AI) && DT_NODE_HAS_PROP(node, io_channels)) ||    \
	 ((IO_CHAN_KIND(node) == UC_IO_AO) && DT_NODE_HAS_PROP(node, pwms)))

#define IO_CHAN_CHECK(node)                                                              \
	BUILD_ASSERT((sizeof(DT_NODE_FULL_NAME(node)) <= UC_CHANNEL_NAME_MAX) &&         \
			     IO_CHAN_HW_OK(node),                                        \
		     "uc,io-channels: " DT_NODE_FULL_NAME(node)                          \
		     ": name longer than 15 characters, or no gpios (di/do), "            \
		     "io-channels (ai), pwms (ao) and not simulated");

#define IO_CATALOG_CHECKS(inst) DT_FOREACH_CHILD_STATUS_OKAY(inst, IO_CHAN_CHECK)

DT_FOREACH_STATUS_OKAY(uc_io_channels, IO_CATALOG_CHECKS)

/* The terminating entry (name NULL) keeps the array non-empty on boards
 * without a catalog.
 */
static const struct io_chan io_chans[] = {
	DT_FOREACH_STATUS_OKAY(uc_io_channels, IO_CATALOG_ENTRIES)
	{.name = NULL},
};

#define IO_CHAN_COUNT (ARRAY_SIZE(io_chans) - 1U)

/* ---------------------------------------------------------------------- */
/* Channel state                                                           */
/* ---------------------------------------------------------------------- */

struct io_state {
	bool ready;  /* hardware initialised */
	bool forced;
	bool bound;  /* bound to a BACnet object (io.json) */
	uint16_t obj_type;
	uint32_t obj_instance;
	double value;
	double force_value;
};

static K_MUTEX_DEFINE(io_lock);
static struct io_state io_st[ARRAY_SIZE(io_chans)];
static bool io_initialised;

static bool kind_is_output(uint8_t kind)
{
	return (kind == UC_IO_DO) || (kind == UC_IO_AO);
}

/* Normalise an engineering value for a channel kind. */
static double io_normalise(uint8_t kind, double v)
{
	switch (kind) {
	case UC_IO_DI:
	case UC_IO_DO:
		return (v != 0.0) ? 1.0 : 0.0;
	case UC_IO_AO:
		return CLAMP(v, 0.0, 100.0);
	default:
		return v;
	}
}

static int io_check_id(int id)
{
	if ((id < 0) || ((size_t)id >= IO_CHAN_COUNT)) {
		return -ENOENT;
	}

	return 0;
}

/* ---------------------------------------------------------------------- */
/* Hardware access                                                         */
/* ---------------------------------------------------------------------- */

#if defined(CONFIG_PWM)
static int pwm_duty_set(const struct pwm_dt_spec *spec, double pct)
{
	uint64_t pulse = (uint64_t)(((double)spec->period * CLAMP(pct, 0.0, 100.0)) / 100.0 + 0.5);

	if (pulse > spec->period) {
		pulse = spec->period;
	}

	return (pwm_set_pulse_dt(spec, (uint32_t)pulse) < 0) ? -EIO : 0;
}
#endif

#if defined(CONFIG_ADC)
static int adc_read_mv(const struct adc_dt_spec *spec, double *mv)
{
	int16_t sample = 0;
	struct adc_sequence seq = {
		.buffer = &sample,
		.buffer_size = sizeof(sample),
	};
	int32_t val;
	int rc;

	rc = adc_sequence_init_dt(spec, &seq);
	if (rc < 0) {
		return -EIO;
	}
	rc = adc_read_dt(spec, &seq);
	if (rc < 0) {
		return -EIO;
	}
	val = spec->channel_cfg.differential ? (int32_t)sample : (int32_t)(uint16_t)sample;
	rc = adc_raw_to_millivolts_dt(spec, &val);
	if (rc < 0) {
		return -EIO;
	}
	*mv = (double)val;

	return 0;
}
#endif

static int chan_hw_init(const struct io_chan *c)
{
	switch (c->hw) {
	case UC_IO_HW_SIM:
		return 0;
	case UC_IO_HW_GPIO:
#if defined(CONFIG_GPIO)
		if (!gpio_is_ready_dt(&c->gpio)) {
			return -ENODEV;
		}
		return gpio_pin_configure_dt(&c->gpio, (c->kind == UC_IO_DI)
								? GPIO_INPUT
								: GPIO_OUTPUT_INACTIVE);
#else
		return -ENOTSUP;
#endif
	case UC_IO_HW_ADC:
#if defined(CONFIG_ADC)
		if (!adc_is_ready_dt(&c->adc)) {
			return -ENODEV;
		}
		return adc_channel_setup_dt(&c->adc);
#else
		return -ENOTSUP;
#endif
	case UC_IO_HW_PWM:
#if defined(CONFIG_PWM)
		if (!pwm_is_ready_dt(&c->pwm)) {
			return -ENODEV;
		}
		return pwm_duty_set(&c->pwm, 0.0);
#else
		return -ENOTSUP;
#endif
	default:
		return -ENOTSUP;
	}
}

/* Drive an output with v (normalised). io_lock held. */
static int out_apply_locked(size_t id, double v)
{
	const struct io_chan *c = &io_chans[id];

	switch (c->hw) {
	case UC_IO_HW_SIM:
		return 0;
	case UC_IO_HW_GPIO:
#if defined(CONFIG_GPIO)
		return (gpio_pin_set_dt(&c->gpio, (v != 0.0) ? 1 : 0) < 0) ? -EIO : 0;
#else
		return -EIO;
#endif
	case UC_IO_HW_PWM:
#if defined(CONFIG_PWM)
		return pwm_duty_set(&c->pwm, v);
#else
		return -EIO;
#endif
	default:
		return -EIO;
	}
}

/* Current channel value (forced value if forced). Any thread. */
static int chan_read(size_t id, double *value)
{
	const struct io_chan *c = &io_chans[id];
	struct io_state *s = &io_st[id];
	bool adc = false;
	int rc = 0;

	k_mutex_lock(&io_lock, K_FOREVER);
	if (!s->ready) {
		rc = -EIO;
	} else if (s->forced) {
		*value = s->force_value;
	} else {
		switch (c->hw) {
		case UC_IO_HW_SIM:
		case UC_IO_HW_PWM:
			*value = s->value;
			break;
		case UC_IO_HW_GPIO:
#if defined(CONFIG_GPIO)
			rc = gpio_pin_get_dt(&c->gpio);
			if (rc >= 0) {
				*value = (rc != 0) ? 1.0 : 0.0;
				rc = 0;
			} else if (c->kind == UC_IO_DO) {
				/* no read-back: report the commanded level */
				*value = s->value;
				rc = 0;
			} else {
				rc = -EIO;
			}
#else
			rc = -EIO;
#endif
			break;
		case UC_IO_HW_ADC:
			adc = true;
			break;
		default:
			rc = -EIO;
			break;
		}
	}
	k_mutex_unlock(&io_lock);

	if (adc) {
#if defined(CONFIG_ADC)
		rc = adc_read_mv(&c->adc, value);
#else
		rc = -EIO;
#endif
	}

	return rc;
}

/* Record a command for an output and drive it unless forced. */
static int out_command(size_t id, double v)
{
	struct io_state *s = &io_st[id];
	int rc = 0;

	k_mutex_lock(&io_lock, K_FOREVER);
	if (!s->ready) {
		rc = -EIO;
	} else {
		s->value = v;
		if (!s->forced) {
			rc = out_apply_locked(id, v);
		}
	}
	k_mutex_unlock(&io_lock);

	return rc;
}

/* ---------------------------------------------------------------------- */
/* Public channel API                                                      */
/* ---------------------------------------------------------------------- */

const char *uc_io_kind_str(enum uc_io_kind kind)
{
	switch (kind) {
	case UC_IO_DI:
		return "di";
	case UC_IO_DO:
		return "do";
	case UC_IO_AI:
		return "ai";
	case UC_IO_AO:
		return "ao";
	default:
		return "?";
	}
}

const char *uc_io_hw_str(enum uc_io_hw hw)
{
	switch (hw) {
	case UC_IO_HW_GPIO:
		return "gpio";
	case UC_IO_HW_ADC:
		return "adc";
	case UC_IO_HW_PWM:
		return "pwm";
	case UC_IO_HW_SIM:
		return "sim";
	default:
		return "?";
	}
}

const char *uc_io_board_name(void)
{
	return IO_BOARD_NAME;
}

int uc_io_init(void)
{
	int first_err = 0;
	size_t ok = 0;

	k_mutex_lock(&io_lock, K_FOREVER);
	if (io_initialised) {
		k_mutex_unlock(&io_lock);
		return 0;
	}
	for (size_t i = 0; i < IO_CHAN_COUNT; i++) {
		const struct io_chan *c = &io_chans[i];
		struct io_state *s = &io_st[i];
		int rc = chan_hw_init(c);

		memset(s, 0, sizeof(*s));
		s->value = (c->hw == UC_IO_HW_SIM) ? io_normalise(c->kind, (double)c->initial)
						    : 0.0;
		s->ready = (rc == 0);
		if (rc < 0) {
			LOG_ERR("channel %s (%s/%s) init failed: %d", c->name,
				uc_io_kind_str((enum uc_io_kind)c->kind),
				uc_io_hw_str((enum uc_io_hw)c->hw), rc);
			if (first_err == 0) {
				first_err = (rc == -ENOTSUP) ? -ENOTSUP : -EIO;
			}
		} else {
			ok++;
		}
	}
	io_initialised = true;
	k_mutex_unlock(&io_lock);

	LOG_INF("IO catalog %s: %u channel(s), %u ready", IO_BOARD_NAME,
		(unsigned int)IO_CHAN_COUNT, (unsigned int)ok);

	return first_err;
}

size_t uc_io_channel_count(void)
{
	return IO_CHAN_COUNT;
}

int uc_io_channel_info(int id, struct uc_io_channel_info *out)
{
	const struct io_chan *c;
	const struct io_state *s;
	int rc = io_check_id(id);

	if (rc < 0) {
		return rc;
	}
	if (out == NULL) {
		return -EINVAL;
	}
	c = &io_chans[id];
	s = &io_st[id];

	memset(out, 0, sizeof(*out));
	out->id = id;
	out->name = c->name;
	out->desc = c->desc;
	out->kind = (enum uc_io_kind)c->kind;
	out->hw = (enum uc_io_hw)c->hw;

	k_mutex_lock(&io_lock, K_FOREVER);
	out->forced = s->forced;
	out->bound = s->bound;
	out->obj_type = s->obj_type;
	out->obj_instance = s->obj_instance;
	k_mutex_unlock(&io_lock);

	return 0;
}

int uc_io_find(const char *name)
{
	if (name == NULL) {
		return -EINVAL;
	}
	for (size_t i = 0; i < IO_CHAN_COUNT; i++) {
		if (strcmp(io_chans[i].name, name) == 0) {
			return (int)i;
		}
	}

	return -ENOENT;
}

int uc_io_read(int id, double *value)
{
	int rc = io_check_id(id);

	if (rc < 0) {
		return rc;
	}
	if (value == NULL) {
		return -EINVAL;
	}

	return chan_read((size_t)id, value);
}

int uc_io_write(int id, double value)
{
	int rc = io_check_id(id);

	if (rc < 0) {
		return rc;
	}
	if (!kind_is_output(io_chans[id].kind)) {
		return -EACCES;
	}
	if (!isfinite(value)) {
		return -EINVAL;
	}

	return out_command((size_t)id, io_normalise(io_chans[id].kind, value));
}

int uc_io_force(int id, double value)
{
	const struct io_chan *c;
	struct io_state *s;
	int rc = io_check_id(id);

	if (rc < 0) {
		return rc;
	}
	if (!isfinite(value)) {
		return -EINVAL;
	}
	c = &io_chans[id];
	s = &io_st[id];
	value = io_normalise(c->kind, value);

	k_mutex_lock(&io_lock, K_FOREVER);
	if (!s->ready) {
		rc = -EIO;
	} else {
		s->forced = true;
		s->force_value = value;
		if ((c->hw == UC_IO_HW_SIM) && !kind_is_output(c->kind)) {
			/* the simulated input value itself */
			s->value = value;
		}
		if (kind_is_output(c->kind)) {
			rc = out_apply_locked((size_t)id, value);
		}
	}
	k_mutex_unlock(&io_lock);

	if (rc == 0) {
		LOG_INF("%s forced to %g", c->name, value);
	}

	return rc;
}

int uc_io_release(int id)
{
	const struct io_chan *c;
	struct io_state *s;
	bool was_forced;
	int rc = io_check_id(id);

	if (rc < 0) {
		return rc;
	}
	c = &io_chans[id];
	s = &io_st[id];

	k_mutex_lock(&io_lock, K_FOREVER);
	was_forced = s->forced;
	s->forced = false;
	if (was_forced && s->ready && kind_is_output(c->kind)) {
		rc = out_apply_locked((size_t)id, s->value);
	}
	k_mutex_unlock(&io_lock);

	if (was_forced) {
		LOG_INF("%s released", c->name);
	}

	return rc;
}

/* ---------------------------------------------------------------------- */
/* io.json bindings (BACnet thread)                                        */
/* ---------------------------------------------------------------------- */

struct io_point {
	uint16_t ch;
	uint8_t kind;
	bool invert;
	bool has_min;
	bool has_max;
	bool err_logged;
	bool out_valid;
	int8_t di_stable;    /* debounced level, -1 before the first sample */
	int8_t di_candidate; /* last sampled level */
	uint16_t obj_type;
	uint32_t obj_instance;
	uint32_t sample_ms;
	uint32_t debounce_ms;
	double scale;
	double offset;
	double min;
	double max;
	double out_last; /* Present_Value last applied to the output */
	int64_t next_ms;
	int64_t di_since;
	/* Description strings are referenced (not copied) by the objects. */
	char desc[UC_NAME_MAX];
};

static struct io_point io_points[CONFIG_UC_IO_POINTS_MAX];
static size_t io_point_count;

#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_INPUT)
/* State texts of a multi-state input bound to a di: 1 = inactive, 2 = active. */
static const char io_msi_states[] = "Inactive\0Active\0";
#endif

static bool io_type_ok(uint8_t kind, uint16_t type)
{
	switch (kind) {
	case UC_IO_DI:
		return (type == OBJECT_BINARY_INPUT) || (type == OBJECT_MULTI_STATE_INPUT);
	case UC_IO_DO:
		return (type == OBJECT_BINARY_OUTPUT) || (type == OBJECT_BINARY_VALUE);
	case UC_IO_AI:
		return type == OBJECT_ANALOG_INPUT;
	case UC_IO_AO:
		return (type == OBJECT_ANALOG_OUTPUT) || (type == OBJECT_ANALOG_VALUE);
	default:
		return false;
	}
}

static bool io_desc_set_locked(uint16_t type, uint32_t instance, const char *desc)
{
	switch (type) {
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_INPUT)
	case OBJECT_ANALOG_INPUT:
		return Analog_Input_Description_Set(instance, desc);
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_OUTPUT)
	case OBJECT_ANALOG_OUTPUT:
		return Analog_Output_Description_Set(instance, desc);
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_VALUE)
	case OBJECT_ANALOG_VALUE:
		return Analog_Value_Description_Set(instance, desc);
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_INPUT)
	case OBJECT_BINARY_INPUT:
		return Binary_Input_Description_Set(instance, desc);
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_OUTPUT)
	case OBJECT_BINARY_OUTPUT:
		return Binary_Output_Description_Set(instance, desc);
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_VALUE)
	case OBJECT_BINARY_VALUE:
		return Binary_Value_Description_Set(instance, desc);
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_INPUT)
	case OBJECT_MULTI_STATE_INPUT:
		return Multistate_Input_Description_Set(instance, desc);
#endif
	default:
		ARG_UNUSED(instance);
		ARG_UNUSED(desc);
		return false;
	}
}

static void io_unbind_all(void)
{
	k_mutex_lock(&io_lock, K_FOREVER);
	for (size_t i = 0; i < IO_CHAN_COUNT; i++) {
		io_st[i].bound = false;
		io_st[i].obj_type = 0;
		io_st[i].obj_instance = 0;
	}
	k_mutex_unlock(&io_lock);
	io_point_count = 0;
}

static bool io_object_taken(uint16_t type, uint32_t instance)
{
	for (size_t i = 0; i < io_point_count; i++) {
		if ((io_points[i].obj_type == type) && (io_points[i].obj_instance == instance)) {
			return true;
		}
	}

	return false;
}

/* Bind one io.json point. Returns 0 or a negative errno (logged). */
static int io_bind_point(size_t idx, const struct uc_io_point_cfg *p)
{
	const char *type_str = uc_obj_type_to_str(p->object_type);
	struct io_point *pt;
	const struct io_chan *c;
	const char *name;
	int id;
	int rc;

	id = uc_io_find(p->channel);
	if (id < 0) {
		LOG_ERR("io.json point %u: unknown channel '%s'", (unsigned int)idx, p->channel);
		return -ENOENT;
	}
	c = &io_chans[id];
	if (!io_type_ok(c->kind, p->object_type)) {
		LOG_ERR("io.json point %u: %s channel %s cannot be bound to %s", (unsigned int)idx,
			uc_io_kind_str((enum uc_io_kind)c->kind), c->name, type_str);
		return -EINVAL;
	}
	if (io_st[id].bound) {
		LOG_ERR("io.json point %u: channel %s is already bound", (unsigned int)idx,
			c->name);
		return -EEXIST;
	}
	if (io_object_taken(p->object_type, p->object_instance)) {
		LOG_ERR("io.json point %u: %s %u is already bound", (unsigned int)idx, type_str,
			p->object_instance);
		return -EEXIST;
	}
	if ((c->kind == UC_IO_AO) && (p->scale == 0.0)) {
		LOG_ERR("io.json point %u: scale 0 is invalid for output %s", (unsigned int)idx,
			c->name);
		return -EINVAL;
	}
	if (!io_st[id].ready) {
		LOG_WRN("io.json point %u: channel %s has no working hardware", (unsigned int)idx,
			c->name);
	}

	name = (p->name[0] != '\0') ? p->name : c->name;
	rc = uc_bn_obj_create_locked(p->object_type, p->object_instance, name, UC_OWNER_IO);
	if (rc < 0) {
		LOG_ERR("io.json point %u: creating %s %u failed: %d (%s)", (unsigned int)idx,
			type_str, p->object_instance, rc, uc_err_str(rc));
		return rc;
	}

	pt = &io_points[io_point_count];
	memset(pt, 0, sizeof(*pt));
	pt->ch = (uint16_t)id;
	pt->kind = c->kind;
	pt->invert = p->invert;
	pt->has_min = p->has_min;
	pt->has_max = p->has_max;
	pt->min = p->min;
	pt->max = p->max;
	pt->scale = p->scale;
	pt->offset = p->offset;
	pt->obj_type = p->object_type;
	pt->obj_instance = p->object_instance;
	pt->sample_ms = MAX(p->sample_ms, 1U);
	pt->debounce_ms = p->debounce_ms;
	pt->di_stable = -1;
	pt->di_candidate = -1;
	pt->next_ms = 0;
	(void)uc_strlcpy(pt->desc, (p->description[0] != '\0') ? p->description : c->desc,
			 sizeof(pt->desc));

	if (uc_obj_type_is_analog(p->object_type)) {
		rc = uc_bn_analog_setup_locked(p->object_type, p->object_instance, p->units,
					       p->cov_increment);
		if (rc < 0) {
			LOG_WRN("%s %u: units/COV increment not set (%d)", type_str,
				p->object_instance, rc);
		}
	}
#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_INPUT)
	if ((p->object_type == OBJECT_MULTI_STATE_INPUT) &&
	    !Multistate_Input_State_Text_List_Set(p->object_instance, io_msi_states)) {
		LOG_WRN("%s %u: state texts not set", type_str, p->object_instance);
	}
#endif
	if ((pt->desc[0] != '\0') &&
	    !io_desc_set_locked(p->object_type, p->object_instance, pt->desc)) {
		LOG_DBG("%s %u: description not set", type_str, p->object_instance);
	}

	k_mutex_lock(&io_lock, K_FOREVER);
	io_st[id].bound = true;
	io_st[id].obj_type = p->object_type;
	io_st[id].obj_instance = p->object_instance;
	k_mutex_unlock(&io_lock);
	io_point_count++;

	LOG_INF("%s -> %s %u \"%s\"", c->name, type_str, p->object_instance, name);

	return 0;
}

int uc_io_apply_config_locked(const struct uc_io_cfg *cfg)
{
	size_t count;
	int rc;

	rc = uc_bn_obj_delete_owned(UC_OWNER_IO);
	if (rc < 0) {
		LOG_WRN("deleting IO objects failed: %d", rc);
	} else if (rc > 0) {
		LOG_DBG("%d IO object(s) deleted", rc);
	}
	io_unbind_all();

	if (cfg == NULL) {
		return -EINVAL;
	}
	count = MIN(cfg->count, (size_t)CONFIG_UC_IO_POINTS_MAX);
	for (size_t i = 0; i < count; i++) {
		(void)io_bind_point(i, &cfg->points[i]);
	}

	return (int)io_point_count;
}

struct io_apply_args {
	const struct uc_io_cfg *cfg;
	int rc;
};

static void io_apply_fn(void *arg)
{
	struct io_apply_args *a = arg;

	a->rc = uc_io_apply_config_locked(a->cfg);
}

int uc_io_apply_config(void)
{
	/* ~7 KB: heap, not the caller's stack */
	struct uc_io_cfg *cfg = k_malloc(sizeof(*cfg));
	struct io_apply_args a = { .rc = -EIO };
	int rc;

	if (cfg == NULL) {
		return -ENOMEM;
	}
	uc_config_get_io(cfg);
	a.cfg = cfg;
	rc = uc_bn_exec(io_apply_fn, &a, K_FOREVER);
	k_free(cfg);

	return (rc < 0) ? rc : a.rc;
}

/* ---------------------------------------------------------------------- */
/* Scan (BACnet thread)                                                    */
/* ---------------------------------------------------------------------- */

static void io_point_error(struct io_point *pt, const char *what, int rc)
{
	if (!pt->err_logged) {
		pt->err_logged = true;
		LOG_ERR("%s (%s %u): %s failed: %d", io_chans[pt->ch].name,
			uc_obj_type_to_str(pt->obj_type), pt->obj_instance, what, rc);
	}
}

static void io_point_ok(struct io_point *pt)
{
	if (pt->err_logged) {
		pt->err_logged = false;
		LOG_INF("%s (%s %u): recovered", io_chans[pt->ch].name,
			uc_obj_type_to_str(pt->obj_type), pt->obj_instance);
	}
}

static int io_prop_double(const struct io_point *pt, uint32_t prop, double *out)
{
	BACNET_APPLICATION_DATA_VALUE v;
	int rc;

	memset(&v, 0, sizeof(v));
	rc = uc_bn_prop_read_locked(pt->obj_type, pt->obj_instance, prop, -1, &v);
	if (rc < 0) {
		return rc;
	}

	return uc_value_to_double(&v, out);
}

/* Out_Of_Service decouples Present_Value from the hardware. */
static bool io_out_of_service(const struct io_point *pt)
{
	double oos = 0.0;

	return (io_prop_double(pt, PROP_OUT_OF_SERVICE, &oos) == 0) && (oos != 0.0);
}

static void io_scan_di(struct io_point *pt, int64_t now)
{
	double raw;
	int8_t level;
	int rc = chan_read(pt->ch, &raw);

	if (rc < 0) {
		io_point_error(pt, "read", rc);
		return;
	}
	level = (int8_t)(((raw != 0.0) ? 1 : 0) ^ (pt->invert ? 1 : 0));
	if (level != pt->di_candidate) {
		pt->di_candidate = level;
		pt->di_since = now;
	}
	if (level != pt->di_stable) {
		if ((pt->di_stable < 0) || (pt->debounce_ms == 0U) ||
		    ((now - pt->di_since) >= (int64_t)pt->debounce_ms)) {
			pt->di_stable = level;
		} else {
			/* sample again as soon as the debounce time has elapsed */
			pt->next_ms = MIN(pt->next_ms, pt->di_since + (int64_t)pt->debounce_ms);
		}
	}
	if (pt->di_stable < 0) {
		return;
	}
	if (io_out_of_service(pt)) {
		io_point_ok(pt);
		return;
	}
	rc = uc_bn_input_pv_set_locked(pt->obj_type, pt->obj_instance,
				       (pt->obj_type == OBJECT_MULTI_STATE_INPUT)
					       ? (double)pt->di_stable + 1.0
					       : (double)pt->di_stable);
	if (rc < 0) {
		io_point_error(pt, "Present_Value update", rc);
		return;
	}
	io_point_ok(pt);
}

static void io_scan_ai(struct io_point *pt)
{
	double mv;
	int rc = chan_read(pt->ch, &mv);

	if (rc < 0) {
		io_point_error(pt, "read", rc);
		return;
	}
	if (io_out_of_service(pt)) {
		io_point_ok(pt);
		return;
	}
	rc = uc_bn_input_pv_set_locked(pt->obj_type, pt->obj_instance,
				       mv * pt->scale + pt->offset);
	if (rc < 0) {
		io_point_error(pt, "Present_Value update", rc);
		return;
	}
	io_point_ok(pt);
}

static void io_scan_out(struct io_point *pt)
{
	double pv;
	double raw;
	int rc;

	if (io_out_of_service(pt)) {
		return;
	}
	rc = io_prop_double(pt, PROP_PRESENT_VALUE, &pv);
	if (rc < 0) {
		io_point_error(pt, "Present_Value read", rc);
		return;
	}
	if (pt->out_valid && (pv == pt->out_last)) {
		return;
	}

	if (pt->kind == UC_IO_DO) {
		raw = (double)(((pv != 0.0) ? 1 : 0) ^ (pt->invert ? 1 : 0));
	} else {
		double eng = pv;

		if (pt->has_min && (eng < pt->min)) {
			eng = pt->min;
		}
		if (pt->has_max && (eng > pt->max)) {
			eng = pt->max;
		}
		raw = CLAMP((eng - pt->offset) / pt->scale, 0.0, 100.0);
	}

	rc = out_command(pt->ch, raw);
	if (rc < 0) {
		/* retried at the next sample */
		pt->out_valid = false;
		io_point_error(pt, "output", rc);
		return;
	}
	pt->out_valid = true;
	pt->out_last = pv;
	io_point_ok(pt);
	LOG_DBG("%s = %g (PV %g)", io_chans[pt->ch].name, raw, pv);
}

void uc_io_scan(void)
{
	int64_t now = k_uptime_get();

	for (size_t i = 0; i < io_point_count; i++) {
		struct io_point *pt = &io_points[i];

		if (now < pt->next_ms) {
			continue;
		}
		pt->next_ms = now + (int64_t)pt->sample_ms;

		switch (pt->kind) {
		case UC_IO_DI:
			io_scan_di(pt, now);
			break;
		case UC_IO_AI:
			io_scan_ai(pt);
			break;
		case UC_IO_DO:
		case UC_IO_AO:
			io_scan_out(pt);
			break;
		default:
			break;
		}
	}
}
