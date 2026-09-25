/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Unit tests of uc_config.c: parsing of the documented examples, defaults
 * for absent fields, schema violations, apps.json encode/parse round trip,
 * the cache (init, reload, set_apps), staged <doc>.new documents and the
 * device.json log level, on top of the storage stub.
 */

#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/logging/log.h>
#include <zephyr/logging/log_backend.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/ztest.h>

#include "bacnet/bacenum.h"

#include "uc/uc_config.h"
#include "uc/uc_storage.h"

#include "storage_stub.h"

/* A log source compiled with all levels: the runtime filter set from
 * device.json "log.level" is observable on it.
 */
LOG_MODULE_REGISTER(test_config, LOG_LEVEL_DBG);

#define DEVICE_NEW UC_FILE_DEVICE_CFG UC_CFG_STAGED_SUFFIX
#define IO_NEW     UC_FILE_IO_CFG UC_CFG_STAGED_SUFFIX
#define APPS_NEW   UC_FILE_APPS_CFG UC_CFG_STAGED_SUFFIX

/* schemas/examples/<doc>.json, embedded by CMake */
static const char example_device[] = {
#include "example_device.json.inc"
	0};
static const char example_io[] = {
#include "example_io.json.inc"
	0};
static const char example_apps[] = {
#include "example_apps.json.inc"
	0};

/* Parsers may modify their input: always parse a private copy. */
static char json_buf[CONFIG_UC_CONFIG_DOC_MAX + 1];

static char *copy(const char *text)
{
	size_t len = strlen(text);

	zassert_true(len < sizeof(json_buf), "test document too large");
	memcpy(json_buf, text, len + 1);
	return json_buf;
}

/* Large results live in .bss (ztest stack). */
static struct uc_device_cfg dev;
static struct uc_io_cfg io;
static struct uc_apps_cfg apps;
static struct uc_apps_cfg apps2;
static char enc_buf[CONFIG_UC_CONFIG_DOC_MAX + 1];

static int parse_device(const char *text)
{
	return uc_config_parse_device(copy(text), strlen(text), &dev);
}

static int parse_io(const char *text)
{
	return uc_config_parse_io(copy(text), strlen(text), &io);
}

static int parse_apps(const char *text)
{
	return uc_config_parse_apps(copy(text), strlen(text), &apps);
}

static void before(void *fixture)
{
	ARG_UNUSED(fixture);
	stub_fs_reset();
}

ZTEST_SUITE(uc_config, NULL, NULL, before, NULL, NULL);

/* ---------------------------------------------------------------------- */
/* Defaults                                                                */
/* ---------------------------------------------------------------------- */

ZTEST(uc_config, test_defaults)
{
	struct uc_io_point_cfg pt;
	struct uc_app_cfg *app = &apps.apps[0];

	uc_config_device_defaults(&dev);
	zassert_equal(dev.instance, CONFIG_UC_DEVICE_INSTANCE_DEFAULT);
	zassert_str_equal(dev.name, CONFIG_UC_DEVICE_NAME_DEFAULT);
	zassert_true(dev.dhcp);
	zassert_equal(dev.udp_port, 47808);
	zassert_equal(dev.apdu_timeout_ms, 3000);
	zassert_equal(dev.apdu_retries, 3);
	zassert_false(dev.fd_enabled);
	zassert_equal(dev.binding_count, 0);
	zassert_str_equal(dev.bacnet_password, "");
	zassert_equal(dev.log_level, LOG_LEVEL_INF);

	uc_config_io_point_defaults(&pt);
	zassert_equal(pt.units, UNITS_NO_UNITS);
	zassert_within(pt.scale, 1.0, 1e-12);
	zassert_within(pt.offset, 0.0, 1e-12);
	zassert_within(pt.cov_increment, 0.1, 1e-12);
	zassert_equal(pt.sample_ms, 100);
	zassert_equal(pt.debounce_ms, 20);
	zassert_false(pt.invert);
	zassert_false(pt.has_min);
	zassert_false(pt.has_max);

	uc_config_app_defaults(app);
	zassert_true(app->autostart);
	zassert_equal(app->period_ms, 1000);
	zassert_equal(app->heap_kb, 8);
	zassert_equal(app->stack_kb, 4);
	zassert_equal(app->perms, 0);
	zassert_equal(app->param_count, 0);
	zassert_false(app->has_sha256);
}

/* ---------------------------------------------------------------------- */
/* device.json                                                             */
/* ---------------------------------------------------------------------- */

ZTEST(uc_config, test_device_example)
{
	zassert_ok(parse_device(example_device));
	zassert_equal(dev.instance, 1001);
	zassert_str_equal(dev.name, "uc-sensor");
	zassert_str_equal(dev.description, "Room sensor node");
	zassert_str_equal(dev.location, "Lab 1");
	zassert_false(dev.dhcp);
	zassert_str_equal(dev.ipv4, "192.168.10.51");
	zassert_str_equal(dev.netmask, "255.255.255.0");
	zassert_str_equal(dev.gateway, "192.168.10.1");
	zassert_equal(dev.udp_port, 47808);
	zassert_equal(dev.apdu_timeout_ms, 3000);
	zassert_equal(dev.apdu_retries, 3);
	zassert_false(dev.fd_enabled);
	zassert_equal(dev.binding_count, 1);
	zassert_equal(dev.bindings[0].device, 1002);
	zassert_str_equal(dev.bindings[0].address, "192.168.10.52");
	zassert_equal(dev.bindings[0].port, 47808);
	zassert_str_equal(dev.bacnet_password, "");
	zassert_equal(dev.log_level, LOG_LEVEL_INF);
}

ZTEST(uc_config, test_device_minimal_defaults)
{
	zassert_ok(parse_device("{\"schema\":1,\"device\":{\"instance\":5,\"name\":\"n\"}}"));
	zassert_equal(dev.instance, 5);
	zassert_str_equal(dev.name, "n");
	zassert_str_equal(dev.description, "");
	zassert_true(dev.dhcp);
	zassert_equal(dev.udp_port, 47808);
	zassert_equal(dev.apdu_timeout_ms, 3000);
	zassert_equal(dev.apdu_retries, 3);
	zassert_false(dev.fd_enabled);
	zassert_equal(dev.binding_count, 0);
	zassert_equal(dev.log_level, LOG_LEVEL_INF);
}

ZTEST(uc_config, test_device_optional_fields)
{
	zassert_ok(parse_device(
		"{\"schema\":1,\"device\":{\"instance\":0,\"name\":\"a\\\"b\"},"
		"\"network\":{\"dhcp\":false,\"ipv4\":\"10.0.0.2\"},"
		"\"bacnet\":{\"udp_port\":47809,\"apdu_timeout_ms\":100,\"apdu_retries\":0,"
		"\"foreign_device\":{\"bbmd\":\"10.0.0.1\",\"ttl_s\":120},"
		"\"static_bindings\":[{\"device\":7,\"address\":\"127.0.0.1\"},"
		"{\"device\":8,\"address\":\"127.0.0.1\",\"port\":47810}],"
		"\"password\":\"Open Sesame~20chars!\"},"
		"\"log\":{\"level\":\"dbg\"},\"x-unknown\":{\"ignored\":[1,2]}}"));
	zassert_equal(dev.instance, 0);
	zassert_str_equal(dev.name, "a\"b");
	zassert_false(dev.dhcp);
	zassert_str_equal(dev.ipv4, "10.0.0.2");
	zassert_str_equal(dev.netmask, "255.255.255.0", "static netmask default");
	zassert_str_equal(dev.gateway, "");
	zassert_equal(dev.udp_port, 47809);
	zassert_equal(dev.apdu_timeout_ms, 100);
	zassert_equal(dev.apdu_retries, 0);
	zassert_true(dev.fd_enabled);
	zassert_str_equal(dev.fd_bbmd, "10.0.0.1");
	zassert_equal(dev.fd_port, 47808);
	zassert_equal(dev.fd_ttl_s, 120);
	zassert_equal(dev.binding_count, 2);
	zassert_equal(dev.bindings[0].port, 47808);
	zassert_equal(dev.bindings[1].device, 8);
	zassert_equal(dev.bindings[1].port, 47810);
	zassert_str_equal(dev.bacnet_password, "Open Sesame~20chars!");
	zassert_equal(dev.log_level, LOG_LEVEL_DBG);

	zassert_ok(parse_device("{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
				"\"bacnet\":{\"password\":\"x\"}}"));
	zassert_str_equal(dev.bacnet_password, "x");
	/* "" is "not configured" (the schema requires 1..20 characters) */
	zassert_ok(parse_device("{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
				"\"bacnet\":{\"password\":\"\"}}"));
	zassert_str_equal(dev.bacnet_password, "");

	/* static without address falls back to DHCP */
	zassert_ok(parse_device("{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
				"\"network\":{\"dhcp\":false}}"));
	zassert_true(dev.dhcp);
}

ZTEST(uc_config, test_device_invalid)
{
	static const char *const bad[] = {
		/* syntax */
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"}",
		"",
		"[]",
		/* schema / required */
		"{\"device\":{\"instance\":1,\"name\":\"n\"}}",
		"{\"schema\":2,\"device\":{\"instance\":1,\"name\":\"n\"}}",
		"{\"schema\":1}",
		"{\"schema\":1,\"device\":{\"name\":\"n\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"\"}}",
		/* types and ranges */
		"{\"schema\":1,\"device\":{\"instance\":\"1\",\"name\":\"n\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1.5,\"name\":\"n\"}}",
		"{\"schema\":1,\"device\":{\"instance\":-1,\"name\":\"n\"}}",
		"{\"schema\":1,\"device\":{\"instance\":4194303,\"name\":\"n\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":"
		"\"0123456789012345678901234567890123456789012345678901234567890123\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"network\":{\"dhcp\":\"no\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"network\":{\"dhcp\":false,\"ipv4\":\"300.1.1.1\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"network\":{\"dhcp\":false,\"ipv4\":\"10.0.0.1\",\"netmask\":\"255.0.255.0\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"network\":{\"gateway\":\"10.0.0\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},\"bacnet\":{\"udp_port\":0}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"udp_port\":65536}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"apdu_timeout_ms\":99}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"apdu_retries\":11}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"foreign_device\":{\"ttl_s\":60}}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"foreign_device\":{\"bbmd\":\"10.0.0.1\",\"ttl_s\":9}}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"static_bindings\":[{\"device\":1}]}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"static_bindings\":[{\"address\":\"10.0.0.1\"}]}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},\"log\":{\"level\":\"trace\"}}",
		/* bacnet.password: 1..20 printable ASCII characters */
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"password\":\"012345678901234567890\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"password\":\"tab\\there\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"password\":\"p\u00e4ss\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"password\":\"0123456789012345678901234567890123456789"
		"012345678901234567890123456789\"}}",
		"{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		"\"bacnet\":{\"password\":1234}}",
	};

	for (size_t i = 0; i < ARRAY_SIZE(bad); i++) {
		zassert_equal(parse_device(bad[i]), -EINVAL, "document %u accepted", (unsigned)i);
		/* *out holds defaults after a rejection */
		zassert_equal(dev.instance, CONFIG_UC_DEVICE_INSTANCE_DEFAULT);
		zassert_str_equal(dev.bacnet_password, "");
	}
}

ZTEST(uc_config, test_device_bindings_limit)
{
	static char doc[2048];
	int n;

	n = snprintf(doc, sizeof(doc),
		     "{\"schema\":1,\"device\":{\"instance\":1,\"name\":\"n\"},"
		     "\"bacnet\":{\"static_bindings\":[");
	for (int i = 0; i <= CONFIG_UC_BACNET_STATIC_BINDINGS_MAX; i++) {
		n += snprintf(doc + n, sizeof(doc) - n, "%s{\"device\":%d,\"address\":\"10.0.0.1\"}",
			      (i == 0) ? "" : ",", i);
	}
	snprintf(doc + n, sizeof(doc) - n, "]}}");
	zassert_equal(parse_device(doc), -ENOSPC);
}

/* ---------------------------------------------------------------------- */
/* io.json                                                                 */
/* ---------------------------------------------------------------------- */

ZTEST(uc_config, test_io_example)
{
	const struct uc_io_point_cfg *p;

	zassert_ok(parse_io(example_io));
	zassert_equal(io.count, 4);

	p = &io.points[0];
	zassert_str_equal(p->channel, "di0");
	zassert_equal(p->object_type, OBJECT_BINARY_INPUT);
	zassert_equal(p->object_instance, 1);
	zassert_str_equal(p->name, "User Button");
	zassert_equal(p->debounce_ms, 30);
	zassert_equal(p->sample_ms, 100);
	zassert_equal(p->units, UNITS_NO_UNITS);
	zassert_false(p->invert);

	p = &io.points[1];
	zassert_str_equal(p->channel, "do0");
	zassert_equal(p->object_type, OBJECT_BINARY_OUTPUT);
	zassert_str_equal(p->name, "Heater Relay");
	zassert_equal(p->debounce_ms, 20);

	p = &io.points[2];
	zassert_str_equal(p->channel, "ai0");
	zassert_equal(p->object_type, OBJECT_ANALOG_INPUT);
	zassert_equal(p->units, UNITS_DEGREES_CELSIUS);
	zassert_within(p->scale, 0.1, 1e-12);
	zassert_within(p->offset, -50.0, 1e-12);
	zassert_within(p->cov_increment, 0.2, 1e-12);
	zassert_equal(p->sample_ms, 500);
	zassert_false(p->has_min);

	p = &io.points[3];
	zassert_str_equal(p->channel, "ao0");
	zassert_equal(p->object_type, OBJECT_ANALOG_OUTPUT);
	zassert_equal(p->units, UNITS_PERCENT);
	zassert_true(p->has_min);
	zassert_true(p->has_max);
	zassert_within(p->min, 0.0, 1e-12);
	zassert_within(p->max, 100.0, 1e-12);
	zassert_within(p->scale, 1.0, 1e-12);
}

ZTEST(uc_config, test_io_minimal_defaults)
{
	const struct uc_io_point_cfg *p = &io.points[0];

	zassert_ok(parse_io("{\"schema\":1,\"points\":[]}"));
	zassert_equal(io.count, 0);

	zassert_ok(parse_io("{\"schema\":1,\"points\":[{\"channel\":\"do1\","
			    "\"type\":\"binary-value\",\"instance\":7,\"invert\":true}]}"));
	zassert_equal(io.count, 1);
	zassert_str_equal(p->name, "do1", "name defaults to the channel");
	zassert_str_equal(p->description, "");
	zassert_equal(p->object_type, OBJECT_BINARY_VALUE);
	zassert_equal(p->units, UNITS_NO_UNITS);
	zassert_within(p->scale, 1.0, 1e-12);
	zassert_within(p->cov_increment, 0.1, 1e-12);
	zassert_equal(p->sample_ms, 100);
	zassert_equal(p->debounce_ms, 20);
	zassert_true(p->invert);
	zassert_false(p->has_min);
	zassert_false(p->has_max);

	zassert_ok(parse_io("{\"schema\":1,\"points\":[{\"channel\":\"di1\","
			    "\"type\":\"multi-state-input\",\"instance\":4194302,"
			    "\"min\":-1.5e2,\"sample_ms\":10,\"debounce_ms\":0}]}"));
	zassert_equal(p->object_type, OBJECT_MULTI_STATE_INPUT);
	zassert_equal(p->object_instance, 4194302);
	zassert_true(p->has_min);
	zassert_within(p->min, -150.0, 1e-12);
	zassert_equal(p->sample_ms, 10);
	zassert_equal(p->debounce_ms, 0);
}

ZTEST(uc_config, test_io_invalid)
{
#define PT(body) "{\"schema\":1,\"points\":[{" body "}]}"
	static const char *const bad[] = {
		"{\"schema\":1}",
		"{\"points\":[]}",
		"{\"schema\":1,\"points\":{}}",
		PT("\"type\":\"binary-input\",\"instance\":1"),
		PT("\"channel\":\"di0\",\"instance\":1"),
		PT("\"channel\":\"di0\",\"type\":\"binary-input\""),
		PT("\"channel\":\"di0\",\"type\":\"analog-foo\",\"instance\":1"),
		PT("\"channel\":\"di0\",\"type\":\"multi-state-value\",\"instance\":1"),
		PT("\"channel\":\"di0\",\"type\":\"device\",\"instance\":1"),
		PT("\"channel\":\"di0\",\"type\":\"3\",\"instance\":1"),
		PT("\"channel\":\"di0\",\"type\":\"binary-input\",\"instance\":4194303"),
		PT("\"channel\":\"channel-name-16ch\",\"type\":\"binary-input\",\"instance\":1"),
		PT("\"channel\":\"ai0\",\"type\":\"analog-input\",\"instance\":1,"
		   "\"units\":\"furlongs\""),
		PT("\"channel\":\"ai0\",\"type\":\"analog-input\",\"instance\":1,\"scale\":\"2\""),
		PT("\"channel\":\"ai0\",\"type\":\"analog-input\",\"instance\":1,"
		   "\"cov_increment\":-0.1"),
		PT("\"channel\":\"ai0\",\"type\":\"analog-input\",\"instance\":1,\"sample_ms\":9"),
		PT("\"channel\":\"ai0\",\"type\":\"analog-input\",\"instance\":1,"
		   "\"sample_ms\":3600001"),
		PT("\"channel\":\"di0\",\"type\":\"binary-input\",\"instance\":1,"
		   "\"debounce_ms\":10001"),
		PT("\"channel\":\"ao0\",\"type\":\"analog-output\",\"instance\":1,"
		   "\"min\":10,\"max\":5"),
		PT("\"channel\":\"di0\",\"type\":\"binary-input\",\"instance\":1,\"invert\":1"),
		"{\"schema\":1,\"points\":[{\"channel\":\"di0\",\"type\":\"binary-input\","
		"\"instance\":1},{\"channel\":\"di1\",\"type\":\"binary-input\",\"instance\":1}]}",
	};
#undef PT

	for (size_t i = 0; i < ARRAY_SIZE(bad); i++) {
		zassert_equal(parse_io(bad[i]), -EINVAL, "document %u accepted", (unsigned)i);
		zassert_equal(io.count, 0);
	}
}

ZTEST(uc_config, test_io_points_limit)
{
	static char doc[CONFIG_UC_CONFIG_DOC_MAX];
	int n;

	n = snprintf(doc, sizeof(doc), "{\"schema\":1,\"points\":[");
	for (int i = 0; i <= CONFIG_UC_IO_POINTS_MAX; i++) {
		n += snprintf(doc + n, sizeof(doc) - n,
			      "%s{\"channel\":\"ai%d\",\"type\":\"analog-value\",\"instance\":%d}",
			      (i == 0) ? "" : ",", i, i);
	}
	snprintf(doc + n, sizeof(doc) - n, "]}");
	zassert_equal(parse_io(doc), -ENOSPC);
}

/* ---------------------------------------------------------------------- */
/* apps.json                                                               */
/* ---------------------------------------------------------------------- */

ZTEST(uc_config, test_apps_example)
{
	const struct uc_app_cfg *a = &apps.apps[0];

	zassert_ok(parse_apps(example_apps));
	zassert_equal(apps.count, 1);
	zassert_str_equal(a->name, "thermostat");
	zassert_str_equal(a->file, "/lfs/apps/thermostat.wasm");
	zassert_true(a->autostart);
	zassert_equal(a->period_ms, 1000);
	zassert_equal(a->heap_kb, 8);
	zassert_equal(a->stack_kb, 4);
	zassert_equal(a->perms, UC_PERM_BACNET_LOCAL | UC_PERM_BACNET_REMOTE);
	zassert_equal(a->param_count, 3);
	zassert_str_equal(uc_app_cfg_param(a, "sensor_device"), "1001");
	zassert_str_equal(uc_app_cfg_param(a, "sensor_instance"), "1");
	zassert_str_equal(uc_app_cfg_param(a, "setpoint"), "21.5");
	zassert_is_null(uc_app_cfg_param(a, "missing"));
	zassert_false(a->has_sha256);
}

ZTEST(uc_config, test_apps_minimal_defaults)
{
	const struct uc_app_cfg *a = &apps.apps[0];

	zassert_ok(parse_apps("{\"schema\":1,\"apps\":[{\"name\":\"a\","
			      "\"file\":\"/lfs/apps/a.aot\"},{\"name\":\"b\",\"autostart\":false,"
			      "\"file\":\"/lfs/apps/b.x.wasm\",\"period_ms\":0,\"heap_kb\":0,"
			      "\"stack_kb\":64,\"perms\":[\"kv\",\"io\"],\"sha256\":"
			      "\"00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff\"}]}"));
	zassert_equal(apps.count, 2);
	zassert_true(a->autostart);
	zassert_equal(a->period_ms, 1000);
	zassert_equal(a->heap_kb, 8);
	zassert_equal(a->stack_kb, 4);
	zassert_equal(a->perms, 0);
	zassert_equal(a->param_count, 0);
	zassert_false(a->has_sha256);

	a = &apps.apps[1];
	zassert_false(a->autostart);
	zassert_equal(a->period_ms, 0);
	zassert_equal(a->heap_kb, 0);
	zassert_equal(a->stack_kb, 64);
	zassert_equal(a->perms, UC_PERM_KV | UC_PERM_IO);
	zassert_true(a->has_sha256);
	zassert_equal(a->sha256[0], 0x00);
	zassert_equal(a->sha256[1], 0x11);
	zassert_equal(a->sha256[15], 0xff);
	zassert_equal(a->sha256[31], 0xff);
}

ZTEST(uc_config, test_apps_invalid)
{
#define APP(body) "{\"schema\":1,\"apps\":[{" body "}]}"
#define F "\"file\":\"/lfs/apps/a.wasm\""
	static const char *const bad[] = {
		"{\"schema\":1}",
		"{\"apps\":[]}",
		APP(F),
		APP("\"name\":\"a\""),
		APP("\"name\":\"Thermo\"," F),
		APP("\"name\":\"a.b\"," F),
		APP("\"name\":\"abcdefghijklmnopqrstuvwx\"," F),
		APP("\"name\":\"a\",\"file\":\"/lfs/cfg/a.wasm\""),
		APP("\"name\":\"a\",\"file\":\"/lfs/apps/a.bin\""),
		APP("\"name\":\"a\",\"file\":\"/lfs/apps/.wasm\""),
		APP("\"name\":\"a\",\"file\":\"/lfs/apps/sub/a.wasm\""),
		APP("\"name\":\"a\",\"file\":"
		    "\"/lfs/apps/abcdefghijabcdefghijabcdefghijabcdefghijX.wasm\""),
		APP("\"name\":\"a\"," F ",\"period_ms\":3600001"),
		APP("\"name\":\"a\"," F ",\"period_ms\":-1"),
		APP("\"name\":\"a\"," F ",\"heap_kb\":257"),
		APP("\"name\":\"a\"," F ",\"stack_kb\":0"),
		APP("\"name\":\"a\"," F ",\"stack_kb\":65"),
		APP("\"name\":\"a\"," F ",\"autostart\":\"yes\""),
		APP("\"name\":\"a\"," F ",\"perms\":[\"net\"]"),
		APP("\"name\":\"a\"," F ",\"perms\":[\"io\",\"io\"]"),
		APP("\"name\":\"a\"," F ",\"perms\":[\"io\",\"kv\",\"bacnet.local\","
		    "\"bacnet.remote\",\"io\"]"),
		APP("\"name\":\"a\"," F ",\"params\":[{\"key\":\"a b\",\"value\":\"1\"}]"),
		APP("\"name\":\"a\"," F ",\"params\":[{\"key\":\"..\",\"value\":\"1\"}]"),
		APP("\"name\":\"a\"," F ",\"params\":[{\"key\":\"abcdefghijklmnopqrstuvwx\","
		    "\"value\":\"1\"}]"),
		APP("\"name\":\"a\"," F ",\"params\":[{\"key\":\"k\",\"value\":1}]"),
		APP("\"name\":\"a\"," F ",\"sha256\":\"0011\""),
		APP("\"name\":\"a\"," F ",\"sha256\":"
		    "\"00112233445566778899AABBCCDDEEFF00112233445566778899aabbccddeeff\""),
		"{\"schema\":1,\"apps\":[{\"name\":\"a\"," F "},{\"name\":\"a\"," F "}]}",
	};
#undef F
#undef APP

	for (size_t i = 0; i < ARRAY_SIZE(bad); i++) {
		zassert_equal(parse_apps(bad[i]), -EINVAL, "document %u accepted", (unsigned)i);
		zassert_equal(apps.count, 0);
	}
}

ZTEST(uc_config, test_apps_limit)
{
	static char doc[2048];
	int n;

	n = snprintf(doc, sizeof(doc), "{\"schema\":1,\"apps\":[");
	for (int i = 0; i <= CONFIG_UC_APPS_MAX; i++) {
		n += snprintf(doc + n, sizeof(doc) - n,
			      "%s{\"name\":\"app%d\",\"file\":\"/lfs/apps/app%d.wasm\"}",
			      (i == 0) ? "" : ",", i, i);
	}
	snprintf(doc + n, sizeof(doc) - n, "]}");
	zassert_equal(parse_apps(doc), -ENOSPC);
}

static void fill_apps(struct uc_apps_cfg *cfg)
{
	struct uc_app_cfg *a;

	memset(cfg, 0, sizeof(*cfg));
	cfg->count = 2;

	a = &cfg->apps[0];
	uc_config_app_defaults(a);
	strcpy(a->name, "pid_1");
	strcpy(a->file, "/lfs/apps/pid.wasm");
	a->autostart = false;
	a->period_ms = 250;
	a->heap_kb = 16;
	a->stack_kb = 8;
	a->perms = UC_PERM_BACNET_LOCAL | UC_PERM_IO | UC_PERM_KV;
	a->param_count = 3;
	strcpy(a->params[0].key, "setpoint");
	strcpy(a->params[0].value, "21.5");
	strcpy(a->params[1].key, "label");
	strcpy(a->params[1].value, "say \"hi\" \\ tab\there");
	strcpy(a->params[2].key, "empty");
	a->has_sha256 = true;
	for (int i = 0; i < 32; i++) {
		a->sha256[i] = (uint8_t)(i * 7 + 3);
	}

	a = &cfg->apps[1];
	uc_config_app_defaults(a);
	strcpy(a->name, "link");
	strcpy(a->file, "/lfs/apps/uc-link.aot");
}

ZTEST(uc_config, test_apps_encode_round_trip)
{
	int len;

	fill_apps(&apps2);
	len = uc_config_encode_apps(&apps2, enc_buf, sizeof(enc_buf));
	zassert_true(len > 0, "encode failed: %d", len);
	zassert_equal((size_t)len, strlen(enc_buf));

	zassert_ok(uc_config_parse_apps(enc_buf, (size_t)len, &apps));
	zassert_equal(apps.count, apps2.count);
	for (size_t i = 0; i < apps.count; i++) {
		const struct uc_app_cfg *x = &apps2.apps[i];
		const struct uc_app_cfg *y = &apps.apps[i];

		zassert_str_equal(y->name, x->name);
		zassert_str_equal(y->file, x->file);
		zassert_equal(y->autostart, x->autostart);
		zassert_equal(y->period_ms, x->period_ms);
		zassert_equal(y->heap_kb, x->heap_kb);
		zassert_equal(y->stack_kb, x->stack_kb);
		zassert_equal(y->perms, x->perms);
		zassert_equal(y->param_count, x->param_count);
		for (size_t k = 0; k < x->param_count; k++) {
			zassert_str_equal(y->params[k].key, x->params[k].key);
			zassert_str_equal(y->params[k].value, x->params[k].value);
		}
		zassert_equal(y->has_sha256, x->has_sha256);
		zassert_mem_equal(y->sha256, x->sha256, sizeof(x->sha256));
	}

	/* an empty table encodes to a valid document */
	apps2.count = 0;
	len = uc_config_encode_apps(&apps2, enc_buf, sizeof(enc_buf));
	zassert_true(len > 0);
	zassert_ok(uc_config_parse_apps(enc_buf, (size_t)len, &apps));
	zassert_equal(apps.count, 0);

	/* too small a buffer */
	fill_apps(&apps2);
	zassert_equal(uc_config_encode_apps(&apps2, enc_buf, 64), -ENOSPC);
}

/* ---------------------------------------------------------------------- */
/* Cache                                                                   */
/* ---------------------------------------------------------------------- */

ZTEST(uc_config, test_cache_init_and_reload)
{
	/* missing files: defaults */
	zassert_ok(uc_config_init());
	uc_config_get_device(&dev);
	zassert_equal(dev.instance, CONFIG_UC_DEVICE_INSTANCE_DEFAULT);
	uc_config_get_io(&io);
	zassert_equal(io.count, 0);
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 0);

	/* documented examples */
	stub_fs_put(UC_FILE_DEVICE_CFG, example_device);
	stub_fs_put(UC_FILE_IO_CFG, example_io);
	stub_fs_put(UC_FILE_APPS_CFG, example_apps);
	zassert_ok(uc_config_init());
	uc_config_get_device(&dev);
	zassert_equal(dev.instance, 1001);
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 1);

	/* a rejected document on reload keeps the active configuration */
	stub_fs_put(UC_FILE_IO_CFG, "{\"schema\":1,\"points\":[{\"channel\":\"x\"}]}");
	zassert_equal(uc_config_reload(UC_CFG_IO), -EINVAL);
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);

	/* ... but boots with defaults */
	zassert_equal(uc_config_init(), -EINVAL);
	uc_config_get_io(&io);
	zassert_equal(io.count, 0);
	uc_config_get_device(&dev);
	zassert_equal(dev.instance, 1001);

	/* reload of one document leaves the others alone */
	stub_fs_put(UC_FILE_DEVICE_CFG,
		    "{\"schema\":1,\"device\":{\"instance\":77,\"name\":\"re\"}}");
	stub_fs_put(UC_FILE_APPS_CFG, "{\"schema\":1,\"apps\":[]}");
	zassert_ok(uc_config_reload(UC_CFG_DEVICE));
	uc_config_get_device(&dev);
	zassert_equal(dev.instance, 77);
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 1);
	zassert_ok(uc_config_reload(UC_CFG_ALL & ~UC_CFG_IO));
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 0);

	zassert_equal(uc_config_reload(0), -EINVAL);
	zassert_equal(uc_config_reload(BIT(7)), -EINVAL);
}

ZTEST(uc_config, test_set_apps)
{
	const char *file;

	zassert_ok(uc_config_init());

	fill_apps(&apps2);
	zassert_ok(uc_config_set_apps(&apps2));
	zassert_equal(stub_fs_writes(), 1);

	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 2);
	zassert_str_equal(apps.apps[0].params[1].value, "say \"hi\" \\ tab\there");

	/* the persisted document parses to the same table */
	file = stub_fs_get(UC_FILE_APPS_CFG);
	zassert_not_null(file);
	zassert_ok(parse_apps(file));
	zassert_equal(apps.count, 2);
	zassert_str_equal(apps.apps[1].name, "link");
	zassert_ok(uc_config_reload(UC_CFG_APPS));
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 2);

	/* invalid tables are refused without touching file or cache */
	apps2.apps[1].stack_kb = 0;
	zassert_equal(uc_config_set_apps(&apps2), -EINVAL);
	fill_apps(&apps2);
	strcpy(apps2.apps[1].name, "pid_1");
	zassert_equal(uc_config_set_apps(&apps2), -EINVAL);
	fill_apps(&apps2);
	strcpy(apps2.apps[1].file, "/tmp/x.wasm");
	zassert_equal(uc_config_set_apps(&apps2), -EINVAL);
	fill_apps(&apps2);
	apps2.count = CONFIG_UC_APPS_MAX + 1;
	zassert_equal(uc_config_set_apps(&apps2), -ENOSPC);
	zassert_equal(stub_fs_writes(), 1);

	/* a failing write leaves the cache unchanged */
	fill_apps(&apps2);
	apps2.count = 1;
	stub_fs_fail_next_write(-EIO);
	zassert_equal(uc_config_set_apps(&apps2), -EIO);
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 2);
}

/* ---------------------------------------------------------------------- */
/* Staged documents (<doc>.new)                                            */
/* ---------------------------------------------------------------------- */

static const char io_empty[] = "{\"schema\":1,\"points\":[]}";
static const char io_bad[] = "{\"schema\":1,\"points\":[{\"channel\":\"x\"}]}";

ZTEST(uc_config, test_staged_valid)
{
	stub_fs_put(UC_FILE_DEVICE_CFG, example_device);
	stub_fs_put(UC_FILE_IO_CFG, example_io);
	zassert_ok(uc_config_init());
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);

	/* valid: renamed over the active document and used */
	stub_fs_put(IO_NEW, io_empty);
	zassert_ok(uc_config_reload(UC_CFG_IO));
	uc_config_get_io(&io);
	zassert_equal(io.count, 0);
	zassert_is_null(stub_fs_get(IO_NEW));
	zassert_str_equal(stub_fs_get(UC_FILE_IO_CFG), io_empty);

	/* without a staged document the active one is re-read */
	stub_fs_put(UC_FILE_IO_CFG, example_io);
	zassert_ok(uc_config_reload(UC_CFG_IO));
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);

	/* staged without an active document */
	zassert_ok(uc_storage_remove(UC_FILE_APPS_CFG));
	stub_fs_put(APPS_NEW, example_apps);
	zassert_ok(uc_config_reload(UC_CFG_APPS));
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 1);
	zassert_is_null(stub_fs_get(APPS_NEW));
	zassert_str_equal(stub_fs_get(UC_FILE_APPS_CFG), example_apps);

	/* only the requested documents look at their staged file */
	stub_fs_put(IO_NEW, io_empty);
	zassert_ok(uc_config_reload(UC_CFG_DEVICE));
	zassert_not_null(stub_fs_get(IO_NEW));
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);
}

ZTEST(uc_config, test_staged_invalid)
{
	static char big[CONFIG_UC_CONFIG_DOC_MAX + 2];
	static char limit_doc[CONFIG_UC_CONFIG_DOC_MAX];
	int n;

	stub_fs_put(UC_FILE_DEVICE_CFG, example_device);
	stub_fs_put(UC_FILE_IO_CFG, example_io);
	stub_fs_put(UC_FILE_APPS_CFG, example_apps);
	zassert_ok(uc_config_init());

	/* schema error: deleted, active configuration and file kept */
	stub_fs_put(IO_NEW, io_bad);
	zassert_equal(uc_config_reload(UC_CFG_IO), -EINVAL);
	zassert_is_null(stub_fs_get(IO_NEW));
	zassert_str_equal(stub_fs_get(UC_FILE_IO_CFG), example_io);
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);

	/* half-written upload (syntax error) */
	stub_fs_put(IO_NEW, "{\"schema\":1,\"points\":[{\"chan");
	zassert_equal(uc_config_reload(UC_CFG_IO), -EINVAL);
	zassert_is_null(stub_fs_get(IO_NEW));
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);

	/* table limit exceeded: also -EINVAL for a staged document */
	n = snprintf(limit_doc, sizeof(limit_doc), "{\"schema\":1,\"points\":[");
	for (int i = 0; i <= CONFIG_UC_IO_POINTS_MAX; i++) {
		n += snprintf(limit_doc + n, sizeof(limit_doc) - n,
			      "%s{\"channel\":\"ai%d\",\"type\":\"analog-value\",\"instance\":%d}",
			      (i == 0) ? "" : ",", i, i);
	}
	snprintf(limit_doc + n, sizeof(limit_doc) - n, "]}");
	stub_fs_put(IO_NEW, limit_doc);
	zassert_equal(uc_config_reload(UC_CFG_IO), -EINVAL);
	zassert_is_null(stub_fs_get(IO_NEW));

	/* larger than CONFIG_UC_CONFIG_DOC_MAX */
	memset(big, ' ', sizeof(big) - 1);
	memcpy(big, io_empty, strlen(io_empty));
	stub_fs_put_len(IO_NEW, big, CONFIG_UC_CONFIG_DOC_MAX + 1);
	zassert_equal(uc_config_reload(UC_CFG_IO), -EINVAL);
	zassert_is_null(stub_fs_get(IO_NEW));
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);

	/* activation (rename) fails: nothing changes, the staged file stays for
	 * another attempt
	 */
	stub_fs_put(IO_NEW, io_empty);
	stub_fs_fail_next_rename(-EIO);
	zassert_equal(uc_config_reload(UC_CFG_IO), -EIO);
	zassert_not_null(stub_fs_get(IO_NEW));
	zassert_str_equal(stub_fs_get(UC_FILE_IO_CFG), example_io);
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);
	zassert_ok(uc_config_reload(UC_CFG_IO));
	uc_config_get_io(&io);
	zassert_equal(io.count, 0);

	/* "all": every document is processed, the first error is returned */
	stub_fs_put(DEVICE_NEW, "{\"schema\":1,\"device\":{\"instance\":-5,\"name\":\"x\"}}");
	stub_fs_put(APPS_NEW, "{\"schema\":1,\"apps\":[]}");
	zassert_equal(uc_config_reload(UC_CFG_ALL), -EINVAL);
	zassert_is_null(stub_fs_get(DEVICE_NEW));
	zassert_is_null(stub_fs_get(APPS_NEW));
	uc_config_get_device(&dev);
	zassert_equal(dev.instance, 1001);
	uc_config_get_apps(&apps);
	zassert_equal(apps.count, 0);
}

ZTEST(uc_config, test_staged_at_boot)
{
	stub_fs_put(UC_FILE_DEVICE_CFG, example_device);
	stub_fs_put(UC_FILE_IO_CFG, example_io);

	/* a valid staged document is activated at boot as well */
	stub_fs_put(DEVICE_NEW, "{\"schema\":1,\"device\":{\"instance\":42,\"name\":\"b\"}}");
	/* a rejected one is deleted and the active document is used */
	stub_fs_put(IO_NEW, io_bad);
	zassert_ok(uc_config_init());

	uc_config_get_device(&dev);
	zassert_equal(dev.instance, 42);
	zassert_is_null(stub_fs_get(DEVICE_NEW));
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);
	zassert_is_null(stub_fs_get(IO_NEW));
	zassert_str_equal(stub_fs_get(UC_FILE_IO_CFG), example_io);
}

ZTEST(uc_config, test_storage_not_ready)
{
	stub_fs_put(UC_FILE_IO_CFG, example_io);
	zassert_ok(uc_config_init());

	/* without /lfs a reload keeps the cache and leaves staged files alone */
	stub_fs_put(IO_NEW, io_empty);
	stub_fs_set_ready(false);
	zassert_equal(uc_config_reload(UC_CFG_IO), -ENODEV);
	uc_config_get_io(&io);
	zassert_equal(io.count, 4);
	zassert_not_null(stub_fs_get(IO_NEW));

	/* boot without /lfs: defaults */
	zassert_equal(uc_config_init(), -ENODEV);
	uc_config_get_io(&io);
	zassert_equal(io.count, 0);
	uc_config_get_device(&dev);
	zassert_equal(dev.instance, CONFIG_UC_DEVICE_INSTANCE_DEFAULT);

	stub_fs_set_ready(true);
	zassert_ok(uc_config_reload(UC_CFG_IO));
	uc_config_get_io(&io);
	zassert_equal(io.count, 0);
	zassert_is_null(stub_fs_get(IO_NEW));
}

/* ---------------------------------------------------------------------- */
/* device.json log.level                                                   */
/* ---------------------------------------------------------------------- */

#define DEV_WITH_LEVEL(lvl)                                                                       \
	"{\"schema\":1,\"device\":{\"instance\":3,\"name\":\"l\"},\"log\":{\"level\":\"" lvl "\"}}"

static uint32_t runtime_level(void)
{
	int src = log_source_id_get("test_config");

	zassert_true(src >= 0);
	zassert_true(log_backend_count_get() > 0);
	return log_filter_get(log_backend_get(0), Z_LOG_LOCAL_DOMAIN_ID, (int16_t)src, true);
}

ZTEST(uc_config, test_log_level_applied)
{
	Z_TEST_SKIP_IFNDEF(CONFIG_LOG_RUNTIME_FILTERING);

	/* boot */
	stub_fs_put(UC_FILE_DEVICE_CFG, DEV_WITH_LEVEL("wrn"));
	zassert_ok(uc_config_init());
	zassert_equal(runtime_level(), LOG_LEVEL_WRN);

	/* reload of device.json */
	stub_fs_put(UC_FILE_DEVICE_CFG, DEV_WITH_LEVEL("err"));
	zassert_ok(uc_config_reload(UC_CFG_DEVICE));
	zassert_equal(runtime_level(), LOG_LEVEL_ERR);

	/* staged device.json */
	stub_fs_put(DEVICE_NEW, DEV_WITH_LEVEL("dbg"));
	zassert_ok(uc_config_reload(UC_CFG_ALL));
	zassert_equal(runtime_level(), LOG_LEVEL_DBG);

	/* a rejected device.json keeps the level */
	stub_fs_put(UC_FILE_DEVICE_CFG, DEV_WITH_LEVEL("trace"));
	zassert_equal(uc_config_reload(UC_CFG_DEVICE), -EINVAL);
	zassert_equal(runtime_level(), LOG_LEVEL_DBG);

	/* without log.level: default "inf" */
	stub_fs_put(UC_FILE_DEVICE_CFG,
		    "{\"schema\":1,\"device\":{\"instance\":3,\"name\":\"l\"}}");
	zassert_ok(uc_config_reload(UC_CFG_DEVICE));
	zassert_equal(runtime_level(), LOG_LEVEL_INF);

	/* leave the default for the other tests */
	stub_fs_reset();
	zassert_ok(uc_config_init());
}
