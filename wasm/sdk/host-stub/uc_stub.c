/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Native implementation of the "bacnet_uc" host functions (bacnet_uc.h)
 * plus the test control API of uc_stub.h. See uc_stub.h for the model.
 */

#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "uc_stub.h"

#define PARAM_KEY_MAX   23
#define PARAM_VALUE_MAX 95
#define KV_KEY_MAX      31
#define IO_NAME_MAX     15
#define INSTANCE_MAX    4194303u /* BACNET_MAX_INSTANCE */
#define TYPE_MAX        1023u
#define PRIORITY_MAX    16u
#define PERIOD_MIN_MS   10u
#define PERIOD_MAX_MS   3600000u
#define LOG_BURST       20u
#define LOG_WINDOW_MS   1000u

/* status codes of uc_stub_start() */
#define START_BAD_VERSION -100
#define START_TRAP        -101
#define START_RUNNING     -102

struct stub_obj {
	bool used;
	uint32_t type;
	uint32_t instance;
	char name[UC_STUB_NAME_MAX];
	int owner;
	bool commandable;
	double value; /* non-commandable Present_Value */
	double relinquish;
	bool prio_set[PRIORITY_MAX];
	double prio[PRIORITY_MAX];
	uint32_t n_props;
	uint32_t prop_id[UC_STUB_MAX_PROPS];
	double prop_val[UC_STUB_MAX_PROPS];
	uint32_t writes;
};

struct stub_remote {
	bool used;
	uint32_t device;
	uint32_t type;
	uint32_t instance;
	double value;
	int32_t fail;
	uint32_t reads;
	uint32_t writes;
	uint32_t last_priority;
	bool relinquished;
};

struct stub_sub {
	bool used;
	int32_t id;
	uint32_t device; /* as reported: local instance for local subscriptions */
	bool local;
	uint32_t type;
	uint32_t instance;
};

struct stub_param {
	char key[PARAM_KEY_MAX + 1];
	char value[PARAM_VALUE_MAX + 1];
};

struct stub_kv {
	bool used;
	char key[KV_KEY_MAX + 1];
	uint8_t val[UC_STUB_KV_VALUE_MAX];
	uint32_t len;
};

struct stub_io {
	char name[IO_NAME_MAX + 1];
	double value;
	bool output;
};

enum ev_kind {
	EV_COV,
	EV_WRITE,
};

struct stub_event {
	enum ev_kind kind;
	int32_t sub_id;
	uint32_t device;
	uint32_t type;
	uint32_t instance;
	uint32_t prop;
	uint32_t priority;
	double value;
};

struct stub_log {
	int32_t level;
	char text[UC_STUB_LOG_LINE_MAX + 1];
};

static struct {
	const struct uc_stub_app *app;
	uint32_t perms;
	uint32_t local_device;
	uint32_t cfg_period;
	bool verbose;

	bool running;
	bool accept;
	uint64_t now;
	uint32_t period;
	bool period_changed;
	uint64_t next_tick;
	uint32_t ticks;
	uint32_t events;
	uint32_t errors;
	uint32_t dropped;

	struct stub_obj objs[UC_STUB_MAX_OBJECTS];
	struct stub_remote remote[UC_STUB_MAX_REMOTE];
	struct stub_sub subs[UC_STUB_MAX_SUBS];
	int32_t next_sub_id;
	struct stub_param params[UC_STUB_MAX_PARAMS];
	size_t n_params;
	struct stub_kv kv[UC_STUB_MAX_KV];
	struct stub_io io[UC_STUB_MAX_IO];
	size_t n_io;

	struct stub_event queue[UC_STUB_QUEUE_LEN];
	size_t q_head;
	size_t q_len;

	struct stub_log log[UC_STUB_MAX_LOG];
	size_t n_log;
	size_t log_excess;
	uint64_t log_window;
	uint32_t log_count;

	int32_t fail_err[UC_STUB_FN_COUNT];
	int32_t fail_count[UC_STUB_FN_COUNT];
} st;

/* ---------------------------------------------------------------------- */
/* Helpers                                                                 */
/* ---------------------------------------------------------------------- */

static int32_t ret(int32_t rc)
{
	if (rc < 0) {
		st.errors++;
	}
	return rc;
}

/* Lookups (uc_param_get*, uc_kv_get): a missing key is not counted as an
 * error, like the firmware (uc_app_host_api.c). */
static int32_t ret_lookup(int32_t rc)
{
	return (rc == UC_ERR_NOT_FOUND) ? rc : ret(rc);
}

static bool injected(enum uc_stub_fn fn, int32_t *err)
{
	if (st.fail_count[fn] == 0) {
		return false;
	}
	if (st.fail_count[fn] > 0) {
		st.fail_count[fn]--;
	}
	*err = st.fail_err[fn];
	return true;
}

static bool has_perm(uint32_t perm)
{
	return (st.perms & perm) == perm;
}

static bool is_local(uint32_t device)
{
	return (device == UC_DEVICE_LOCAL) || (device == st.local_device);
}

static bool object_id_ok(uint32_t type, uint32_t instance)
{
	return (type <= TYPE_MAX) && (instance <= INSTANCE_MAX);
}

static bool type_creatable(uint32_t type)
{
	return (type <= UC_OBJ_BINARY_VALUE) || (type == UC_OBJ_MULTI_STATE_INPUT) ||
	       (type == UC_OBJ_MULTI_STATE_OUTPUT) || (type == UC_OBJ_MULTI_STATE_VALUE);
}

/* Present_Value with a priority array: the output objects. The value
 * objects (AV, BV, MSV) of the firmware's BACnet stack have none. */
static bool type_commandable(uint32_t type)
{
	return (type == UC_OBJ_ANALOG_OUTPUT) || (type == UC_OBJ_BINARY_OUTPUT) ||
	       (type == UC_OBJ_MULTI_STATE_OUTPUT);
}

/* Present_Value that BACnet allows to be commandable (135 clause 19.2):
 * a NULL written to it where it is not commandable changes nothing and
 * succeeds (clause 15.9.2). */
static bool type_optionally_commandable(uint32_t type)
{
	return type_commandable(type) || (type == UC_OBJ_ANALOG_VALUE) ||
	       (type == UC_OBJ_BINARY_VALUE) || (type == UC_OBJ_MULTI_STATE_VALUE);
}

static bool type_binary(uint32_t type)
{
	return (type == UC_OBJ_BINARY_INPUT) || (type == UC_OBJ_BINARY_OUTPUT) ||
	       (type == UC_OBJ_BINARY_VALUE);
}

static bool type_multistate(uint32_t type)
{
	return (type == UC_OBJ_MULTI_STATE_INPUT) || (type == UC_OBJ_MULTI_STATE_OUTPUT) ||
	       (type == UC_OBJ_MULTI_STATE_VALUE);
}

static const char *type_abbr(uint32_t type)
{
	static const char *const names[] = {"AI", "AO", "AV", "BI", "BO", "BV"};

	if (type < 6u) {
		return names[type];
	}
	switch (type) {
	case UC_OBJ_MULTI_STATE_INPUT:
		return "MSI";
	case UC_OBJ_MULTI_STATE_OUTPUT:
		return "MSO";
	case UC_OBJ_MULTI_STATE_VALUE:
		return "MSV";
	default:
		return "OBJ";
	}
}

/* Present_Value as the firmware stores it (uc_value_from_double). */
static int32_t pv_normalise(uint32_t type, double in, double *out)
{
	if (type_binary(type)) {
		*out = (in != 0.0) ? 1.0 : 0.0;
		return UC_OK;
	}
	if (type_multistate(type)) {
		double t = trunc(in);

		if (isnan(in) || (t < 1.0) || (t > (double)UINT32_MAX)) {
			return UC_ERR_INVALID;
		}
		*out = t;
		return UC_OK;
	}
	/* REAL */
	*out = (double)(float)in;
	return UC_OK;
}

static bool key_valid(const char *key, size_t max)
{
	size_t n = strlen(key);

	if ((n == 0) || (n > max)) {
		return false;
	}
	for (size_t i = 0; i < n; i++) {
		unsigned char c = (unsigned char)key[i];

		if (!isalnum(c) && (c != '_') && (c != '.') && (c != '-')) {
			return false;
		}
	}
	return true;
}

/* (pointer, length) string argument into buf; false like app_str(). */
static bool arg_str(const char *s, uint32_t len, char *buf, size_t size)
{
	if ((s == NULL) || (len == 0u) || (len >= size)) {
		return false;
	}
	memcpy(buf, s, len);
	buf[len] = '\0';
	return strlen(buf) == len;
}

/* ---------------------------------------------------------------------- */
/* Objects                                                                 */
/* ---------------------------------------------------------------------- */

static struct stub_obj *obj_find(uint32_t type, uint32_t instance)
{
	for (size_t i = 0; i < UC_STUB_MAX_OBJECTS; i++) {
		struct stub_obj *o = &st.objs[i];

		if (o->used && (o->type == type) && (o->instance == instance)) {
			return o;
		}
	}
	return NULL;
}

static double obj_pv(const struct stub_obj *o)
{
	if (!o->commandable) {
		return o->value;
	}
	for (size_t i = 0; i < PRIORITY_MAX; i++) {
		if (o->prio_set[i]) {
			return o->prio[i];
		}
	}
	return o->relinquish;
}

static struct stub_obj *obj_new(uint32_t type, uint32_t instance, const char *name, int owner,
				double value)
{
	for (size_t i = 0; i < UC_STUB_MAX_OBJECTS; i++) {
		struct stub_obj *o = &st.objs[i];

		if (o->used) {
			continue;
		}
		memset(o, 0, sizeof(*o));
		o->used = true;
		o->type = type;
		o->instance = instance;
		o->owner = owner;
		o->commandable = type_commandable(type);
		if ((name != NULL) && (name[0] != '\0')) {
			snprintf(o->name, sizeof(o->name), "%s", name);
		} else {
			snprintf(o->name, sizeof(o->name), "%s-%u", type_abbr(type),
				 (unsigned int)instance);
		}
		if (pv_normalise(type, value, &value) < 0) {
			value = type_multistate(type) ? 1.0 : 0.0;
		}
		o->value = value;
		o->relinquish = value;
		return o;
	}
	return NULL;
}

static void queue_event(const struct stub_event *ev)
{
	size_t tail;

	if (!st.accept) {
		return;
	}
	if (st.q_len >= UC_STUB_QUEUE_LEN) {
		st.dropped++;
		st.errors++;
		return;
	}
	tail = (st.q_head + st.q_len) % UC_STUB_QUEUE_LEN;
	st.queue[tail] = *ev;
	st.q_len++;
}

static void notify_subs(bool local, uint32_t device, uint32_t type, uint32_t instance, double value)
{
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		struct stub_sub *s = &st.subs[i];
		struct stub_event ev = {0};

		if (!s->used || (s->local != local) || (s->type != type) ||
		    (s->instance != instance) || (!local && (s->device != device))) {
			continue;
		}
		ev.kind = EV_COV;
		ev.sub_id = s->id;
		ev.device = s->device;
		ev.type = type;
		ev.instance = instance;
		ev.prop = UC_PROP_PRESENT_VALUE;
		ev.value = value;
		queue_event(&ev);
	}
}

static void obj_changed(struct stub_obj *o, double old)
{
	double now = obj_pv(o);

	if ((now != old) && !(isnan(now) && isnan(old))) {
		notify_subs(true, st.local_device, o->type, o->instance, now);
	}
}

/* Write Present_Value (value == NULL: relinquish). */
static int32_t obj_write_pv(struct stub_obj *o, const double *value, uint32_t priority)
{
	double old = obj_pv(o);
	double v = 0.0;
	int32_t rc;

	if (value != NULL) {
		rc = pv_normalise(o->type, *value, &v);
		if (rc < 0) {
			return rc;
		}
	}
	if (priority == 0u) {
		priority = PRIORITY_MAX;
	}
	if (!o->commandable) {
		if (value == NULL) {
			/* like the firmware: no change (AV, BV, MSV), else no NULL */
			return type_optionally_commandable(o->type) ? UC_OK : UC_ERR_TYPE;
		}
		/* the priority is ignored */
		o->value = v;
	} else if (value == NULL) {
		o->prio_set[priority - 1u] = false;
	} else {
		o->prio_set[priority - 1u] = true;
		o->prio[priority - 1u] = v;
	}
	if (value != NULL) {
		o->writes++;
	}
	obj_changed(o, old);
	return UC_OK;
}

static int32_t obj_prop_read(struct stub_obj *o, uint32_t prop, double *out)
{
	switch (prop) {
	case UC_PROP_PRESENT_VALUE:
		*out = obj_pv(o);
		return UC_OK;
	case UC_PROP_RELINQUISH_DEFAULT:
		if (!o->commandable) {
			return UC_ERR_NOT_FOUND;
		}
		*out = o->relinquish;
		return UC_OK;
	case UC_PROP_OBJECT_NAME:
	case UC_PROP_DESCRIPTION:
	case UC_PROP_STATUS_FLAGS:
		return UC_ERR_TYPE;
	default:
		break;
	}
	for (uint32_t i = 0; i < o->n_props; i++) {
		if (o->prop_id[i] == prop) {
			*out = o->prop_val[i];
			return UC_OK;
		}
	}
	if (prop == UC_PROP_OUT_OF_SERVICE) {
		*out = 0.0;
		return UC_OK;
	}
	return UC_ERR_NOT_FOUND;
}

static int32_t obj_prop_write(struct stub_obj *o, uint32_t prop, double value)
{
	double old = obj_pv(o);

	if (prop == UC_PROP_RELINQUISH_DEFAULT) {
		if (!o->commandable) {
			return UC_ERR_NOT_FOUND;
		}
		if (pv_normalise(o->type, value, &value) < 0) {
			return UC_ERR_INVALID;
		}
		o->relinquish = value;
		obj_changed(o, old);
		return UC_OK;
	}
	if ((prop == UC_PROP_OBJECT_NAME) || (prop == UC_PROP_DESCRIPTION) ||
	    (prop == UC_PROP_STATUS_FLAGS)) {
		return UC_ERR_TYPE;
	}
	for (uint32_t i = 0; i < o->n_props; i++) {
		if (o->prop_id[i] == prop) {
			o->prop_val[i] = value;
			return UC_OK;
		}
	}
	if (o->n_props >= UC_STUB_MAX_PROPS) {
		return UC_ERR_NO_MEM;
	}
	o->prop_id[o->n_props] = prop;
	o->prop_val[o->n_props] = value;
	o->n_props++;
	return UC_OK;
}

/* ---------------------------------------------------------------------- */
/* Remote points                                                           */
/* ---------------------------------------------------------------------- */

static struct stub_remote *remote_find(uint32_t device, uint32_t type, uint32_t instance)
{
	for (size_t i = 0; i < UC_STUB_MAX_REMOTE; i++) {
		struct stub_remote *r = &st.remote[i];

		if (r->used && (r->device == device) && (r->type == type) &&
		    (r->instance == instance)) {
			return r;
		}
	}
	return NULL;
}

static bool device_known(uint32_t device)
{
	for (size_t i = 0; i < UC_STUB_MAX_REMOTE; i++) {
		if (st.remote[i].used && (st.remote[i].device == device)) {
			return true;
		}
	}
	return false;
}

/* Remote point lookup with the firmware's failure modes. A blocking
 * failure (timeout, binding) advances the clock by timeout_ms. */
static int32_t remote_access(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
			     uint32_t timeout_ms, struct stub_remote **out)
{
	struct stub_remote *r;

	if (!device_known(device)) {
		st.now += timeout_ms;
		return UC_ERR_NO_ROUTE;
	}
	r = remote_find(device, type, instance);
	if (r == NULL) {
		return UC_ERR_BACNET; /* unknown-object */
	}
	if (r->fail < 0) {
		if ((r->fail == UC_ERR_TIMEOUT) || (r->fail == UC_ERR_NO_ROUTE)) {
			st.now += timeout_ms;
		}
		return r->fail;
	}
	if (prop != UC_PROP_PRESENT_VALUE) {
		return UC_ERR_BACNET; /* unknown-property: only PV is modelled */
	}
	*out = r;
	return UC_OK;
}

/* ---------------------------------------------------------------------- */
/* bacnet_uc host functions                                                */
/* ---------------------------------------------------------------------- */

void uc_log(int32_t level, const char *msg, uint32_t len)
{
	struct stub_log *l;
	uint32_t n;

	if (len == 0u) {
		return;
	}
	if (msg == NULL) {
		(void)ret(UC_ERR_INVALID);
		return;
	}
	if ((st.now - st.log_window) >= LOG_WINDOW_MS) {
		st.log_window = st.now;
		st.log_count = 0;
	}
	if (st.log_count >= LOG_BURST) {
		st.log_excess++;
		if (st.verbose) {
			fprintf(stderr, "[%8llu] (log line dropped)\n", (unsigned long long)st.now);
		}
		return;
	}
	st.log_count++;
	if (st.n_log >= UC_STUB_MAX_LOG) {
		return;
	}
	l = &st.log[st.n_log++];
	n = (len > UC_STUB_LOG_LINE_MAX) ? UC_STUB_LOG_LINE_MAX : len;
	memcpy(l->text, msg, n);
	while ((n > 0u) && ((l->text[n - 1u] == '\n') || (l->text[n - 1u] == '\r'))) {
		n--;
	}
	l->text[n] = '\0';
	for (uint32_t i = 0; i < n; i++) {
		unsigned char c = (unsigned char)l->text[i];

		if ((c < 0x20u) || (c == 0x7Fu)) {
			l->text[i] = ' ';
		}
	}
	l->level = level;
	if (st.verbose) {
		static const char *const lv[] = {"???", "ERR", "WRN", "INF", "DBG"};

		fprintf(stderr, "[%8llu] %s %s\n", (unsigned long long)st.now,
			lv[(level >= 1 && level <= 4) ? level : 0], l->text);
	}
}

uint64_t uc_uptime_ms(void)
{
	return st.now;
}

int32_t uc_set_tick_period(uint32_t period_ms)
{
	if ((period_ms != 0u) && ((period_ms < PERIOD_MIN_MS) || (period_ms > PERIOD_MAX_MS))) {
		return ret(UC_ERR_INVALID);
	}
	st.period = period_ms;
	st.period_changed = true;
	return UC_OK;
}

static const char *param_lookup(const char *key, uint32_t key_len, int32_t *err)
{
	char k[PARAM_KEY_MAX + 1];

	if (!arg_str(key, key_len, k, sizeof(k)) || !key_valid(k, PARAM_KEY_MAX)) {
		*err = UC_ERR_INVALID;
		return NULL;
	}
	for (size_t i = 0; i < st.n_params; i++) {
		if (strcmp(st.params[i].key, k) == 0) {
			return st.params[i].value;
		}
	}
	*err = UC_ERR_NOT_FOUND;
	return NULL;
}

int32_t uc_param_get(const char *key, uint32_t key_len, char *buf, uint32_t buf_len)
{
	int32_t err = UC_OK;
	const char *value;
	size_t vlen;

	if ((buf_len > 0u) && (buf == NULL)) {
		return ret(UC_ERR_INVALID);
	}
	value = param_lookup(key, key_len, &err);
	if (value == NULL) {
		return ret_lookup(err);
	}
	vlen = strlen(value);
	if (vlen > buf_len) {
		return ret(UC_ERR_INVALID);
	}
	memcpy(buf, value, vlen);
	if (vlen < buf_len) {
		buf[vlen] = '\0';
	}
	return (int32_t)vlen;
}

int32_t uc_param_get_number(const char *key, uint32_t key_len, double *out)
{
	int32_t err = UC_OK;
	const char *value;
	char *end;
	double d;

	if (out == NULL) {
		return ret(UC_ERR_INVALID);
	}
	value = param_lookup(key, key_len, &err);
	if (value == NULL) {
		return ret_lookup(err);
	}
	d = strtod(value, &end);
	if (end == value) {
		return ret(UC_ERR_TYPE);
	}
	*out = d;
	return UC_OK;
}

int32_t uc_obj_create(uint32_t type, uint32_t instance, const char *name, uint32_t name_len)
{
	char n[UC_STUB_NAME_MAX];
	struct stub_obj *o;
	int32_t err;

	if (!has_perm(UC_STUB_PERM_LOCAL)) {
		return ret(UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance) || (instance == INSTANCE_MAX) || !type_creatable(type)) {
		return ret(UC_ERR_INVALID);
	}
	n[0] = '\0';
	if ((name_len > 0u) && !arg_str(name, name_len, n, sizeof(n))) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_OBJ_CREATE, &err)) {
		return ret(err);
	}
	o = obj_find(type, instance);
	if (o != NULL) {
		return (o->owner == UC_STUB_OWNER_APP) ? UC_OK : ret(UC_ERR_EXISTS);
	}
	o = obj_new(type, instance, n, UC_STUB_OWNER_APP, 0.0);
	return (o != NULL) ? UC_OK : ret(UC_ERR_NO_MEM);
}

int32_t uc_obj_delete(uint32_t type, uint32_t instance)
{
	struct stub_obj *o;

	if (!has_perm(UC_STUB_PERM_LOCAL)) {
		return ret(UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance)) {
		return ret(UC_ERR_INVALID);
	}
	o = obj_find(type, instance);
	if (o == NULL) {
		return ret(UC_ERR_NOT_FOUND);
	}
	if (o->owner != UC_STUB_OWNER_APP) {
		return ret(UC_ERR_PERM);
	}
	o->used = false;
	return UC_OK;
}

int32_t uc_prop_read(uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index,
		     double *out)
{
	struct stub_obj *o;
	int32_t err;

	if (!has_perm(UC_STUB_PERM_LOCAL)) {
		return ret(UC_ERR_PERM);
	}
	if ((out == NULL) || !object_id_ok(type, instance)) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_PROP_READ, &err)) {
		return ret(err);
	}
	o = obj_find(type, instance);
	if (o == NULL) {
		return ret(UC_ERR_NOT_FOUND);
	}
	if (array_index != UC_ARRAY_ALL) {
		return ret(UC_ERR_TYPE);
	}
	return ret(obj_prop_read(o, prop, out));
}

static int32_t local_write(uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index,
			   const double *value, uint32_t priority)
{
	struct stub_obj *o;
	int32_t err;

	if (!has_perm(UC_STUB_PERM_LOCAL)) {
		return ret(UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance) || (priority > PRIORITY_MAX)) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_PROP_WRITE, &err)) {
		return ret(err);
	}
	o = obj_find(type, instance);
	if (o == NULL) {
		return ret(UC_ERR_NOT_FOUND);
	}
	if (array_index != UC_ARRAY_ALL) {
		return ret(UC_ERR_INVALID);
	}
	if (prop == UC_PROP_PRESENT_VALUE) {
		return ret(obj_write_pv(o, value, priority));
	}
	if (value == NULL) {
		/* NULL is not a value of any other property */
		return ret(UC_ERR_TYPE);
	}
	return ret(obj_prop_write(o, prop, *value));
}

int32_t uc_prop_write(uint32_t type, uint32_t instance, uint32_t prop, int32_t array_index,
		      double value, uint32_t priority)
{
	return local_write(type, instance, prop, array_index, &value, priority);
}

int32_t uc_prop_write_null(uint32_t type, uint32_t instance, uint32_t prop, uint32_t priority)
{
	return local_write(type, instance, prop, UC_ARRAY_ALL, NULL, priority);
}

int32_t uc_prop_write_string(uint32_t type, uint32_t instance, uint32_t prop, const char *str,
			     uint32_t len)
{
	struct stub_obj *o;

	if (!has_perm(UC_STUB_PERM_LOCAL)) {
		return ret(UC_ERR_PERM);
	}
	if (((len > 0u) && ((str == NULL) || (memchr(str, '\0', len) != NULL))) ||
	    !object_id_ok(type, instance)) {
		return ret(UC_ERR_INVALID);
	}
	o = obj_find(type, instance);
	if (o == NULL) {
		return ret(UC_ERR_NOT_FOUND);
	}
	if (prop == UC_PROP_OBJECT_NAME) {
		if ((len == 0u) || (len >= sizeof(o->name))) {
			return ret(UC_ERR_INVALID);
		}
		memcpy(o->name, str, len);
		o->name[len] = '\0';
		return UC_OK;
	}
	if (prop == UC_PROP_DESCRIPTION) {
		return UC_OK;
	}
	return ret(UC_ERR_TYPE);
}

int32_t uc_remote_read(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
		       int32_t array_index, double *out, uint32_t timeout_ms)
{
	struct stub_remote *r = NULL;
	struct stub_obj *o;
	int32_t err;

	if ((out == NULL) || !object_id_ok(type, instance)) {
		return ret(UC_ERR_INVALID);
	}
	if (is_local(device)) {
		if (!has_perm(UC_STUB_PERM_LOCAL)) {
			return ret(UC_ERR_PERM);
		}
		o = obj_find(type, instance);
		if (o == NULL) {
			return ret(UC_ERR_NOT_FOUND);
		}
		if (array_index != UC_ARRAY_ALL) {
			return ret(UC_ERR_TYPE);
		}
		return ret(obj_prop_read(o, prop, out));
	}
	if (!has_perm(UC_STUB_PERM_REMOTE)) {
		return ret(UC_ERR_PERM);
	}
	if (device >= INSTANCE_MAX) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_REMOTE_READ, &err)) {
		if (err == UC_ERR_TIMEOUT) {
			st.now += timeout_ms;
		}
		return ret(err);
	}
	err = remote_access(device, type, instance, prop, timeout_ms, &r);
	if (err < 0) {
		return ret(err);
	}
	(void)array_index;
	r->reads++;
	*out = r->value;
	return UC_OK;
}

static int32_t remote_write(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
			    int32_t array_index, const double *value, uint32_t priority,
			    uint32_t timeout_ms)
{
	struct stub_remote *r = NULL;
	int32_t err;

	if (!object_id_ok(type, instance) || (priority > PRIORITY_MAX)) {
		return ret(UC_ERR_INVALID);
	}
	if (is_local(device)) {
		return local_write(type, instance, prop, array_index, value, priority);
	}
	if (!has_perm(UC_STUB_PERM_REMOTE)) {
		return ret(UC_ERR_PERM);
	}
	if (device >= INSTANCE_MAX) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_REMOTE_WRITE, &err)) {
		if (err == UC_ERR_TIMEOUT) {
			st.now += timeout_ms;
		}
		return ret(err);
	}
	err = remote_access(device, type, instance, prop, timeout_ms, &r);
	if (err < 0) {
		return ret(err);
	}
	r->last_priority = priority;
	if (value == NULL) {
		r->relinquished = true;
		return UC_OK;
	}
	r->writes++;
	r->relinquished = false;
	if (r->value != *value) {
		r->value = *value;
		notify_subs(false, device, type, instance, r->value);
	}
	return UC_OK;
}

int32_t uc_remote_write(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
			int32_t array_index, double value, uint32_t priority, uint32_t timeout_ms)
{
	return remote_write(device, type, instance, prop, array_index, &value, priority,
			    timeout_ms);
}

int32_t uc_remote_write_null(uint32_t device, uint32_t type, uint32_t instance, uint32_t prop,
			     uint32_t priority, uint32_t timeout_ms)
{
	return remote_write(device, type, instance, prop, UC_ARRAY_ALL, NULL, priority, timeout_ms);
}

int32_t uc_cov_subscribe(uint32_t device, uint32_t type, uint32_t instance, uint32_t lifetime_s)
{
	bool local = is_local(device);
	struct stub_sub *s = NULL;
	struct stub_event ev = {0};
	int32_t err;

	(void)lifetime_s;
	if (!has_perm(local ? UC_STUB_PERM_LOCAL : UC_STUB_PERM_REMOTE)) {
		return ret(UC_ERR_PERM);
	}
	if (!object_id_ok(type, instance) || (!local && (device >= INSTANCE_MAX))) {
		return ret(UC_ERR_INVALID);
	}
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		if (!st.subs[i].used) {
			s = &st.subs[i];
			break;
		}
	}
	if (s == NULL) {
		return ret(UC_ERR_NO_MEM);
	}
	if (injected(UC_STUB_FN_COV_SUBSCRIBE, &err)) {
		return ret(err);
	}
	s->used = true;
	s->id = st.next_sub_id++;
	s->local = local;
	s->device = local ? st.local_device : device;
	s->type = type;
	s->instance = instance;

	/* the current value is delivered once right after subscribing */
	ev.kind = EV_COV;
	ev.sub_id = s->id;
	ev.device = s->device;
	ev.type = type;
	ev.instance = instance;
	ev.prop = UC_PROP_PRESENT_VALUE;
	if (local) {
		struct stub_obj *o = obj_find(type, instance);

		if (o != NULL) {
			ev.value = obj_pv(o);
			queue_event(&ev);
		}
	} else {
		struct stub_remote *r = remote_find(device, type, instance);

		if ((r != NULL) && (r->fail == 0)) {
			ev.value = r->value;
			queue_event(&ev);
		}
	}
	return s->id;
}

int32_t uc_cov_unsubscribe(int32_t sub_id)
{
	if (sub_id < 0) {
		return ret(UC_ERR_INVALID);
	}
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		if (st.subs[i].used && (st.subs[i].id == sub_id)) {
			st.subs[i].used = false;
			return UC_OK;
		}
	}
	return ret(UC_ERR_NOT_FOUND);
}

int32_t uc_io_find(const char *name, uint32_t len)
{
	char n[IO_NAME_MAX + 1];

	if (!has_perm(UC_STUB_PERM_IO)) {
		return ret(UC_ERR_PERM);
	}
	if ((len == 0u) || (name == NULL)) {
		return ret(UC_ERR_INVALID);
	}
	if (len >= sizeof(n)) {
		return ret(UC_ERR_NOT_FOUND);
	}
	if (!arg_str(name, len, n, sizeof(n))) {
		return ret(UC_ERR_INVALID);
	}
	for (size_t i = 0; i < st.n_io; i++) {
		if (strcmp(st.io[i].name, n) == 0) {
			return (int32_t)i;
		}
	}
	return ret(UC_ERR_NOT_FOUND);
}

int32_t uc_io_read(int32_t channel, double *out)
{
	if (!has_perm(UC_STUB_PERM_IO)) {
		return ret(UC_ERR_PERM);
	}
	if (out == NULL) {
		return ret(UC_ERR_INVALID);
	}
	if ((channel < 0) || ((size_t)channel >= st.n_io)) {
		return ret(UC_ERR_NOT_FOUND);
	}
	*out = st.io[channel].value;
	return UC_OK;
}

int32_t uc_io_write(int32_t channel, double value)
{
	if (!has_perm(UC_STUB_PERM_IO)) {
		return ret(UC_ERR_PERM);
	}
	if ((channel < 0) || ((size_t)channel >= st.n_io)) {
		return ret(UC_ERR_NOT_FOUND);
	}
	if (!st.io[channel].output) {
		return ret(UC_ERR_INVALID);
	}
	st.io[channel].value = value;
	return UC_OK;
}

static struct stub_kv *kv_find(const char *key)
{
	for (size_t i = 0; i < UC_STUB_MAX_KV; i++) {
		if (st.kv[i].used && (strcmp(st.kv[i].key, key) == 0)) {
			return &st.kv[i];
		}
	}
	return NULL;
}

int32_t uc_kv_get(const char *key, uint32_t key_len, void *buf, uint32_t buf_len)
{
	char k[KV_KEY_MAX + 1];
	struct stub_kv *e;
	int32_t err;

	if (!has_perm(UC_STUB_PERM_KV)) {
		return ret(UC_ERR_PERM);
	}
	if (!arg_str(key, key_len, k, sizeof(k)) || !key_valid(k, KV_KEY_MAX)) {
		return ret(UC_ERR_INVALID);
	}
	if ((buf_len > 0u) && (buf == NULL)) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_KV_GET, &err)) {
		return ret(err);
	}
	e = kv_find(k);
	if (e == NULL) {
		return ret_lookup(UC_ERR_NOT_FOUND);
	}
	memcpy(buf, e->val, (e->len < buf_len) ? e->len : buf_len);
	return (int32_t)e->len;
}

int32_t uc_kv_set(const char *key, uint32_t key_len, const void *val, uint32_t val_len)
{
	char k[KV_KEY_MAX + 1];
	int32_t err;

	if (!has_perm(UC_STUB_PERM_KV)) {
		return ret(UC_ERR_PERM);
	}
	if (!arg_str(key, key_len, k, sizeof(k)) || !key_valid(k, KV_KEY_MAX) ||
	    (val_len > UC_STUB_KV_VALUE_MAX) || ((val_len > 0u) && (val == NULL))) {
		return ret(UC_ERR_INVALID);
	}
	if (injected(UC_STUB_FN_KV_SET, &err)) {
		return ret(err);
	}
	return (uc_stub_kv_set(k, val, val_len) == 0) ? UC_OK : ret(UC_ERR_IO);
}

/* ---------------------------------------------------------------------- */
/* Control API                                                             */
/* ---------------------------------------------------------------------- */

void uc_stub_reset(void)
{
	const struct uc_stub_app *app = st.app;
	const char *v = getenv("UC_STUB_VERBOSE");

	memset(&st, 0, sizeof(st));
	st.app = app;
	st.perms = UC_STUB_PERM_ALL;
	st.local_device = 1000;
	st.cfg_period = 1000;
	st.verbose = (v != NULL) && (v[0] != '\0') && (v[0] != '0');
}

void uc_stub_set_app(const struct uc_stub_app *app)
{
	st.app = app;
}

void uc_stub_set_perms(uint32_t perms)
{
	st.perms = perms;
}

void uc_stub_set_period(uint32_t period_ms)
{
	st.cfg_period = period_ms;
}

void uc_stub_set_local_device(uint32_t instance)
{
	st.local_device = instance;
}

uint32_t uc_stub_local_device(void)
{
	return st.local_device;
}

void uc_stub_set_verbose(bool verbose)
{
	st.verbose = verbose;
}

int uc_stub_param_set(const char *key, const char *value)
{
	for (size_t i = 0; i < st.n_params; i++) {
		if (strcmp(st.params[i].key, key) == 0) {
			snprintf(st.params[i].value, sizeof(st.params[i].value), "%s", value);
			return 0;
		}
	}
	if (st.n_params >= UC_STUB_MAX_PARAMS) {
		return -1;
	}
	snprintf(st.params[st.n_params].key, sizeof(st.params[0].key), "%s", key);
	snprintf(st.params[st.n_params].value, sizeof(st.params[0].value), "%s", value);
	st.n_params++;
	return 0;
}

void uc_stub_param_clear(void)
{
	st.n_params = 0;
}

static bool app_failed(void)
{
	return (st.app != NULL) && (st.app->failed != NULL) && st.app->failed();
}

static void teardown(void)
{
	st.accept = false;
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		st.subs[i].used = false;
	}
	for (size_t i = 0; i < UC_STUB_MAX_OBJECTS; i++) {
		if (st.objs[i].used && (st.objs[i].owner == UC_STUB_OWNER_APP)) {
			st.objs[i].used = false;
		}
	}
	st.q_len = 0;
	st.q_head = 0;
	st.running = false;
}

int32_t uc_stub_start(void)
{
	uint32_t version;
	int32_t rc = 0;

	if (st.running) {
		return START_RUNNING;
	}
	if ((st.app == NULL) || (st.app->api_version == NULL)) {
		return START_BAD_VERSION;
	}
	st.period = (st.cfg_period == 0u)             ? 0u
		    : (st.cfg_period < PERIOD_MIN_MS) ? PERIOD_MIN_MS
						      : st.cfg_period;
	st.period_changed = true;
	st.log_window = 0;
	st.log_count = 0;
	st.q_len = 0;
	st.q_head = 0;
	st.accept = true;

	version = st.app->api_version();
	if (app_failed()) {
		teardown();
		return START_TRAP;
	}
	if ((version >> 16) != UC_API_VERSION_MAJOR) {
		teardown();
		return START_BAD_VERSION;
	}
	if (st.app->init != NULL) {
		rc = st.app->init();
		if (app_failed()) {
			teardown();
			return START_TRAP;
		}
		if (rc != 0) {
			teardown();
			return rc;
		}
	}
	st.running = true;
	return 0;
}

void uc_stub_stop(void)
{
	if (!st.running) {
		return;
	}
	if (st.app->deinit != NULL) {
		st.app->deinit();
	}
	teardown();
}

bool uc_stub_running(void)
{
	return st.running;
}

static bool sub_live(const struct stub_event *ev)
{
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		const struct stub_sub *s = &st.subs[i];

		if (s->used && (s->id == ev->sub_id) && (s->type == ev->type) &&
		    (s->instance == ev->instance)) {
			return true;
		}
	}
	return false;
}

static void deliver(const struct stub_event *ev)
{
	if (ev->kind == EV_COV) {
		if ((st.app->on_cov == NULL) || !sub_live(ev)) {
			return;
		}
		st.app->on_cov(ev->sub_id, ev->device, ev->type, ev->instance, ev->prop, ev->value);
	} else {
		if (st.app->on_write == NULL) {
			return;
		}
		st.app->on_write(ev->type, ev->instance, ev->prop, ev->priority, ev->value);
	}
	st.events++;
}

bool uc_stub_run(uint64_t ms)
{
	uint64_t end = st.now + ms;

	while (st.running) {
		if (st.period_changed) {
			st.period_changed = false;
			st.next_tick = st.now + st.period;
		}
		if ((st.app->tick != NULL) && (st.period != 0u) && (st.now >= st.next_tick)) {
			st.app->tick(st.now);
			if (app_failed()) {
				teardown();
				return false;
			}
			st.ticks++;
			if (!st.period_changed) {
				st.next_tick += st.period;
				if (st.next_tick <= st.now) {
					/* overrun (blocking calls): skip missed ticks */
					st.next_tick = st.now + st.period;
				}
			}
			continue;
		}
		if (st.q_len > 0u) {
			struct stub_event ev = st.queue[st.q_head];

			st.q_head = (st.q_head + 1u) % UC_STUB_QUEUE_LEN;
			st.q_len--;
			deliver(&ev);
			if (app_failed()) {
				teardown();
				return false;
			}
			continue;
		}
		if (st.now >= end) {
			break;
		}
		if ((st.app->tick != NULL) && (st.period != 0u) && (st.next_tick < end)) {
			st.now = st.next_tick;
		} else {
			st.now = end;
		}
	}
	if (st.now < end) {
		st.now = end;
	}
	return true;
}

uint64_t uc_stub_now(void)
{
	return st.now;
}

void uc_stub_advance(uint64_t ms)
{
	st.now += ms;
}

uint32_t uc_stub_ticks(void)
{
	return st.ticks;
}

uint32_t uc_stub_events(void)
{
	return st.events;
}

uint32_t uc_stub_errors(void)
{
	return st.errors;
}

uint32_t uc_stub_tick_period(void)
{
	return st.period;
}

uint32_t uc_stub_events_dropped(void)
{
	return st.dropped;
}

int uc_stub_obj_add(uint32_t type, uint32_t instance, const char *name, double value)
{
	if (obj_find(type, instance) != NULL) {
		return -2;
	}
	return (obj_new(type, instance, name, UC_STUB_OWNER_IO, value) != NULL) ? 0 : -1;
}

bool uc_stub_obj_exists(uint32_t type, uint32_t instance)
{
	return obj_find(type, instance) != NULL;
}

bool uc_stub_obj_app_owned(uint32_t type, uint32_t instance)
{
	struct stub_obj *o = obj_find(type, instance);

	return (o != NULL) && (o->owner == UC_STUB_OWNER_APP);
}

const char *uc_stub_obj_name(uint32_t type, uint32_t instance)
{
	struct stub_obj *o = obj_find(type, instance);

	return (o != NULL) ? o->name : NULL;
}

double uc_stub_obj_pv(uint32_t type, uint32_t instance)
{
	struct stub_obj *o = obj_find(type, instance);

	return (o != NULL) ? obj_pv(o) : NAN;
}

bool uc_stub_obj_prio(uint32_t type, uint32_t instance, uint32_t priority, double *value)
{
	struct stub_obj *o = obj_find(type, instance);

	if ((o == NULL) || !o->commandable || (priority < 1u) || (priority > PRIORITY_MAX) ||
	    !o->prio_set[priority - 1u]) {
		return false;
	}
	if (value != NULL) {
		*value = o->prio[priority - 1u];
	}
	return true;
}

double uc_stub_obj_prop(uint32_t type, uint32_t instance, uint32_t prop)
{
	struct stub_obj *o = obj_find(type, instance);

	if (o == NULL) {
		return NAN;
	}
	for (uint32_t i = 0; i < o->n_props; i++) {
		if (o->prop_id[i] == prop) {
			return o->prop_val[i];
		}
	}
	return NAN;
}

uint32_t uc_stub_obj_writes(uint32_t type, uint32_t instance)
{
	struct stub_obj *o = obj_find(type, instance);

	return (o != NULL) ? o->writes : 0u;
}

int uc_stub_obj_set_pv(uint32_t type, uint32_t instance, double value)
{
	struct stub_obj *o = obj_find(type, instance);
	double old;

	if (o == NULL) {
		return -1;
	}
	old = obj_pv(o);
	if (pv_normalise(type, value, &value) < 0) {
		return -1;
	}
	if (o->commandable) {
		o->relinquish = value;
	} else {
		o->value = value;
	}
	obj_changed(o, old);
	return 0;
}

static int32_t client_write(uint32_t type, uint32_t instance, uint32_t prop, const double *value,
			    uint32_t priority)
{
	struct stub_obj *o = obj_find(type, instance);
	struct stub_event ev = {0};
	int32_t rc;

	if (o == NULL) {
		return UC_ERR_NOT_FOUND;
	}
	if (priority > PRIORITY_MAX) {
		return UC_ERR_INVALID;
	}
	if (prop == UC_PROP_PRESENT_VALUE) {
		rc = obj_write_pv(o, value, priority);
	} else if (value != NULL) {
		rc = obj_prop_write(o, prop, *value);
	} else {
		rc = UC_ERR_TYPE;
	}
	if ((rc == UC_OK) && (value != NULL) && (o->owner == UC_STUB_OWNER_APP)) {
		ev.kind = EV_WRITE;
		ev.type = type;
		ev.instance = instance;
		ev.prop = prop;
		ev.priority = (priority == 0u) ? PRIORITY_MAX : priority;
		ev.value = *value;
		queue_event(&ev);
	}
	return rc;
}

int32_t uc_stub_client_write(uint32_t type, uint32_t instance, uint32_t prop, double value,
			     uint32_t priority)
{
	return client_write(type, instance, prop, &value, priority);
}

int32_t uc_stub_client_relinquish(uint32_t type, uint32_t instance, uint32_t priority)
{
	return client_write(type, instance, UC_PROP_PRESENT_VALUE, NULL, priority);
}

int uc_stub_remote_add(uint32_t device, uint32_t type, uint32_t instance, double value)
{
	struct stub_remote *r = remote_find(device, type, instance);

	if (r == NULL) {
		for (size_t i = 0; i < UC_STUB_MAX_REMOTE; i++) {
			if (!st.remote[i].used) {
				r = &st.remote[i];
				break;
			}
		}
	}
	if (r == NULL) {
		return -1;
	}
	memset(r, 0, sizeof(*r));
	r->used = true;
	r->device = device;
	r->type = type;
	r->instance = instance;
	r->value = value;
	return 0;
}

int uc_stub_remote_set(uint32_t device, uint32_t type, uint32_t instance, double value, bool notify)
{
	struct stub_remote *r = remote_find(device, type, instance);

	if (r == NULL) {
		return -1;
	}
	r->value = value;
	if (notify && (r->fail == 0)) {
		notify_subs(false, device, type, instance, value);
	}
	return 0;
}

int uc_stub_remote_fail(uint32_t device, uint32_t type, uint32_t instance, int32_t err)
{
	struct stub_remote *r = remote_find(device, type, instance);

	if (r == NULL) {
		return -1;
	}
	r->fail = err;
	return 0;
}

double uc_stub_remote_value(uint32_t device, uint32_t type, uint32_t instance)
{
	struct stub_remote *r = remote_find(device, type, instance);

	return (r != NULL) ? r->value : NAN;
}

uint32_t uc_stub_remote_writes(uint32_t device, uint32_t type, uint32_t instance)
{
	struct stub_remote *r = remote_find(device, type, instance);

	return (r != NULL) ? r->writes : 0u;
}

uint32_t uc_stub_remote_last_priority(uint32_t device, uint32_t type, uint32_t instance)
{
	struct stub_remote *r = remote_find(device, type, instance);

	return (r != NULL) ? r->last_priority : 0u;
}

bool uc_stub_remote_relinquished(uint32_t device, uint32_t type, uint32_t instance)
{
	struct stub_remote *r = remote_find(device, type, instance);

	return (r != NULL) && r->relinquished;
}

uint32_t uc_stub_remote_reads(uint32_t device, uint32_t type, uint32_t instance)
{
	struct stub_remote *r = remote_find(device, type, instance);

	return (r != NULL) ? r->reads : 0u;
}

uint32_t uc_stub_subs_active(void)
{
	uint32_t n = 0;

	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		n += st.subs[i].used ? 1u : 0u;
	}
	return n;
}

int32_t uc_stub_sub_find(uint32_t device, uint32_t type, uint32_t instance)
{
	bool local = is_local(device);

	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		const struct stub_sub *s = &st.subs[i];

		if (s->used && (s->local == local) && (s->type == type) &&
		    (s->instance == instance) && (local || (s->device == device))) {
			return s->id;
		}
	}
	return -1;
}

int uc_stub_cov_notify(int32_t sub_id, double value)
{
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		const struct stub_sub *s = &st.subs[i];
		struct stub_event ev = {0};

		if (!s->used || (s->id != sub_id)) {
			continue;
		}
		ev.kind = EV_COV;
		ev.sub_id = s->id;
		ev.device = s->device;
		ev.type = s->type;
		ev.instance = s->instance;
		ev.prop = UC_PROP_PRESENT_VALUE;
		ev.value = value;
		queue_event(&ev);
		return 0;
	}
	return -1;
}

void uc_stub_fail(enum uc_stub_fn fn, int32_t err, int32_t count)
{
	if ((unsigned int)fn < UC_STUB_FN_COUNT) {
		st.fail_err[fn] = err;
		st.fail_count[fn] = count;
	}
}

int uc_stub_kv_get(const char *key, void *buf, size_t size)
{
	struct stub_kv *e = kv_find(key);

	if (e == NULL) {
		return -1;
	}
	if (buf != NULL) {
		memcpy(buf, e->val, (e->len < size) ? e->len : size);
	}
	return (int)e->len;
}

int uc_stub_kv_set(const char *key, const void *val, size_t len)
{
	struct stub_kv *e = kv_find(key);

	if (len > UC_STUB_KV_VALUE_MAX) {
		return -1;
	}
	if (e == NULL) {
		for (size_t i = 0; i < UC_STUB_MAX_KV; i++) {
			if (!st.kv[i].used) {
				e = &st.kv[i];
				break;
			}
		}
	}
	if (e == NULL) {
		return -1;
	}
	e->used = true;
	snprintf(e->key, sizeof(e->key), "%s", key);
	if (len > 0u) {
		memcpy(e->val, val, len);
	}
	e->len = (uint32_t)len;
	return 0;
}

int uc_stub_io_add(const char *name, double value, bool output)
{
	if ((st.n_io >= UC_STUB_MAX_IO) || (strlen(name) > IO_NAME_MAX)) {
		return -1;
	}
	snprintf(st.io[st.n_io].name, sizeof(st.io[0].name), "%s", name);
	st.io[st.n_io].value = value;
	st.io[st.n_io].output = output;
	st.n_io++;
	return 0;
}

double uc_stub_io_get(const char *name)
{
	for (size_t i = 0; i < st.n_io; i++) {
		if (strcmp(st.io[i].name, name) == 0) {
			return st.io[i].value;
		}
	}
	return NAN;
}

int uc_stub_io_set(const char *name, double value)
{
	for (size_t i = 0; i < st.n_io; i++) {
		if (strcmp(st.io[i].name, name) == 0) {
			st.io[i].value = value;
			return 0;
		}
	}
	return -1;
}

size_t uc_stub_log_count(void)
{
	return st.n_log;
}

const char *uc_stub_log_line(size_t i, int32_t *level)
{
	if (i >= st.n_log) {
		return NULL;
	}
	if (level != NULL) {
		*level = st.log[i].level;
	}
	return st.log[i].text;
}

size_t uc_stub_log_matches(int32_t level, const char *substr)
{
	size_t n = 0;

	for (size_t i = 0; i < st.n_log; i++) {
		if (((level == 0) || (st.log[i].level == level)) &&
		    (strstr(st.log[i].text, substr) != NULL)) {
			n++;
		}
	}
	return n;
}

size_t uc_stub_log_excess(void)
{
	return st.log_excess;
}

void uc_stub_log_clear(void)
{
	st.n_log = 0;
	st.log_excess = 0;
}

void uc_stub_log_dump(void)
{
	static const char *const lv[] = {"???", "ERR", "WRN", "INF", "DBG"};

	for (size_t i = 0; i < st.n_log; i++) {
		int32_t l = st.log[i].level;

		fprintf(stderr, "  %s %s\n", lv[(l >= 1 && l <= 4) ? l : 0], st.log[i].text);
	}
}

void uc_stub_dump(FILE *f)
{
	static const char *const lv[] = {"???", "ERR", "WRN", "INF", "DBG"};

	for (size_t i = 0; i < UC_STUB_MAX_OBJECTS; i++) {
		const struct stub_obj *o = &st.objs[i];

		if (!o->used) {
			continue;
		}
		fprintf(f, "obj %s:%u %s \"%s\" pv=%.17g writes=%u", type_abbr(o->type),
			(unsigned int)o->instance, (o->owner == UC_STUB_OWNER_APP) ? "app" : "io",
			o->name, obj_pv(o), (unsigned int)o->writes);
		for (size_t p = 0; p < PRIORITY_MAX; p++) {
			if (o->commandable && o->prio_set[p]) {
				fprintf(f, " p%u=%.17g", (unsigned int)(p + 1u), o->prio[p]);
			}
		}
		for (uint32_t p = 0; p < o->n_props; p++) {
			fprintf(f, " prop%u=%.17g", (unsigned int)o->prop_id[p], o->prop_val[p]);
		}
		fputc('\n', f);
	}
	for (size_t i = 0; i < UC_STUB_MAX_REMOTE; i++) {
		const struct stub_remote *r = &st.remote[i];

		if (r->used) {
			fprintf(f, "remote %u %s:%u value=%.17g reads=%u writes=%u prio=%u%s\n",
				(unsigned int)r->device, type_abbr(r->type),
				(unsigned int)r->instance, r->value, (unsigned int)r->reads,
				(unsigned int)r->writes, (unsigned int)r->last_priority,
				r->relinquished ? " relinquished" : "");
		}
	}
	for (size_t i = 0; i < UC_STUB_MAX_SUBS; i++) {
		const struct stub_sub *s = &st.subs[i];

		if (s->used) {
			fprintf(f, "sub %d %s %u %s:%u\n", (int)s->id,
				s->local ? "local" : "remote", (unsigned int)s->device,
				type_abbr(s->type), (unsigned int)s->instance);
		}
	}
	for (size_t i = 0; i < UC_STUB_MAX_KV; i++) {
		const struct stub_kv *e = &st.kv[i];

		if (e->used) {
			fprintf(f, "kv %s len=%u", e->key, (unsigned int)e->len);
			for (uint32_t b = 0; b < e->len; b++) {
				fprintf(f, "%s%02x", (b == 0u) ? " " : "", e->val[b]);
			}
			fputc('\n', f);
		}
	}
	for (size_t i = 0; i < st.n_io; i++) {
		fprintf(f, "io %s=%.17g\n", st.io[i].name, st.io[i].value);
	}
	fprintf(f,
		"stats now=%llu running=%d period=%u ticks=%u events=%u errors=%u dropped=%u "
		"log_excess=%u\n",
		(unsigned long long)st.now, st.running ? 1 : 0, (unsigned int)st.period,
		(unsigned int)st.ticks, (unsigned int)st.events, (unsigned int)st.errors,
		(unsigned int)st.dropped, (unsigned int)st.log_excess);
	for (size_t i = 0; i < st.n_log; i++) {
		int32_t l = st.log[i].level;

		fprintf(f, "log %s %s\n", lv[(l >= 1 && l <= 4) ? l : 0], st.log[i].text);
	}
}
