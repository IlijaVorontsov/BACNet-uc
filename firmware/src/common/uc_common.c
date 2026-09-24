/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shared helpers of the BACnet-uc firmware: error mapping, permission
 * names, BACnet text <-> enum conversion, value conversion and validators.
 * Pure functions without state; safe to call from any thread.
 */

#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/logging/log.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacenum.h"
#include "bacnet/bacapp.h"
#include "bacnet/bactext.h"

#include "uc/uc_common.h"
#include "uc/uc_mgmt.h"

/* Guest ABI error codes (UC_ERR_*). Only the constants are used here. */
#include "../../../wasm/sdk/include/bacnet_uc.h"

LOG_MODULE_REGISTER(uc_common, CONFIG_UC_LOG_LEVEL);

/* ---------------------------------------------------------------------- */
/* Permissions                                                             */
/* ---------------------------------------------------------------------- */

static const struct {
	uint32_t bit;
	const char *name;
} perm_names[] = {
	{UC_PERM_BACNET_LOCAL, "bacnet.local"},
	{UC_PERM_BACNET_REMOTE, "bacnet.remote"},
	{UC_PERM_IO, "io"},
	{UC_PERM_KV, "kv"},
};

uint32_t uc_perm_from_str(const char *name)
{
	if (name == NULL) {
		return 0;
	}

	for (size_t i = 0; i < ARRAY_SIZE(perm_names); i++) {
		if (strcmp(name, perm_names[i].name) == 0) {
			return perm_names[i].bit;
		}
	}

	return 0;
}

const char *uc_perm_to_str(uint32_t bit)
{
	for (size_t i = 0; i < ARRAY_SIZE(perm_names); i++) {
		if (perm_names[i].bit == bit) {
			return perm_names[i].name;
		}
	}

	return NULL;
}

/* ---------------------------------------------------------------------- */
/* Error mapping                                                           */
/* ---------------------------------------------------------------------- */

int uc_err_to_api(int err)
{
	if (err >= 0) {
		return err;
	}

	switch (err) {
	case -EINVAL:
	case -EALREADY:
	case -ESRCH:
	case -EILSEQ:
	case -ERANGE:
	case -EDOM:
	case -E2BIG:
	case -EFBIG:
	case -ENAMETOOLONG:
		return UC_ERR_INVALID;
	case -ENOENT:
		return UC_ERR_NOT_FOUND;
	case -EACCES:
	case -EPERM:
		return UC_ERR_PERM;
	case -ETIMEDOUT:
		return UC_ERR_TIMEOUT;
	case -EBUSY:
	case -EAGAIN:
		return UC_ERR_BUSY;
	case -ENOMEM:
	case -ENOSPC:
		return UC_ERR_NO_MEM;
	case -EREMOTEIO:
		return UC_ERR_BACNET;
	case -ENOTSUP:
	case -ENOSYS:
		return UC_ERR_UNSUPPORTED;
	case -EBADMSG:
		return UC_ERR_TYPE;
	case -EEXIST:
		return UC_ERR_EXISTS;
	case -EHOSTUNREACH:
		return UC_ERR_NO_ROUTE;
	case -EIO:
	default:
		return UC_ERR_IO;
	}
}

int uc_err_to_mgmt(int err)
{
	if (err >= 0) {
		return UC_MGMT_RC_OK;
	}

	switch (err) {
	case -EINVAL:
	case -EBADMSG:
	case -ERANGE:
	case -EDOM:
	case -E2BIG:
	case -EFBIG:
	case -ENAMETOOLONG:
		return UC_MGMT_RC_INVALID;
	case -ENOENT:
	case -EHOSTUNREACH:
		return UC_MGMT_RC_NOT_FOUND;
	case -EACCES:
	case -EPERM:
		return UC_MGMT_RC_PERM;
	case -ETIMEDOUT:
	case -EBUSY:
	case -EAGAIN:
		return UC_MGMT_RC_BUSY;
	case -ENOMEM:
		return UC_MGMT_RC_NO_MEM;
	case -ENOTSUP:
	case -ENOSYS:
		return UC_MGMT_RC_UNSUPPORTED;
	case -EIO:
	case -ENODEV:
	case -EROFS:
	case -ENOTDIR:
	case -EISDIR:
	case -ENOTEMPTY:
		return UC_MGMT_RC_IO;
	case -EEXIST:
		return UC_MGMT_RC_EXISTS;
	case -EALREADY:
	case -ESRCH:
		return UC_MGMT_RC_STATE;
	case -EILSEQ:
		return UC_MGMT_RC_VERIFY;
	case -ENOSPC:
		return UC_MGMT_RC_LIMIT;
	case -EREMOTEIO:
	default:
		return UC_MGMT_RC_UNKNOWN;
	}
}

const char *uc_err_str(int err)
{
	if (err >= 0) {
		return "ok";
	}

	switch (err) {
	case -EINVAL:
		return "invalid argument";
	case -ENOENT:
		return "not found";
	case -EACCES:
	case -EPERM:
		return "permission denied";
	case -ETIMEDOUT:
		return "timeout";
	case -EBUSY:
	case -EAGAIN:
		return "busy";
	case -ENOMEM:
		return "out of memory";
	case -EREMOTEIO:
		return "BACnet error";
	case -ENOTSUP:
	case -ENOSYS:
		return "not supported";
	case -EIO:
		return "I/O error";
	case -EBADMSG:
		return "datatype not convertible";
	case -EEXIST:
		return "already exists";
	case -EHOSTUNREACH:
		return "device not bound";
	case -EALREADY:
	case -ESRCH:
		return "wrong state";
	case -EILSEQ:
		return "verification failed";
	case -ENOSPC:
		return "table full";
	case -EFBIG:
		return "too large";
	case -ENODEV:
		return "no device";
	default:
		return "error";
	}
}

/* ---------------------------------------------------------------------- */
/* BACnet text <-> enum                                                    */
/* ---------------------------------------------------------------------- */

/* Strict unsigned decimal: digits only, no sign, no blanks, <= max. */
static int parse_decimal(const char *s, uint32_t max, uint32_t *out)
{
	uint64_t v = 0;

	if (s == NULL || *s == '\0') {
		return -EINVAL;
	}

	for (const char *p = s; *p != '\0'; p++) {
		if (*p < '0' || *p > '9') {
			return -EINVAL;
		}
		v = v * 10U + (uint64_t)(*p - '0');
		if (v > max) {
			return -EINVAL;
		}
	}

	*out = (uint32_t)v;
	return 0;
}

static bool starts_with_digit(const char *s)
{
	return s != NULL && s[0] >= '0' && s[0] <= '9';
}

int uc_obj_type_from_str(const char *s, uint16_t *type)
{
	uint32_t v;

	if (s == NULL || *s == '\0' || type == NULL) {
		return -EINVAL;
	}

	if (starts_with_digit(s)) {
		if (parse_decimal(s, BACNET_MAX_OBJECT, &v) < 0) {
			return -EINVAL;
		}
	} else if (!bactext_object_type_index(s, &v) || v > BACNET_MAX_OBJECT) {
		return -EINVAL;
	}

	*type = (uint16_t)v;
	return 0;
}

const char *uc_obj_type_to_str(uint16_t type)
{
	return bactext_object_type_name(type);
}

int uc_prop_from_str(const char *s, uint32_t *prop)
{
	uint32_t v;

	if (s == NULL || *s == '\0' || prop == NULL) {
		return -EINVAL;
	}

	if (starts_with_digit(s)) {
		if (parse_decimal(s, MAX_BACNET_PROPERTY_ID, &v) < 0) {
			return -EINVAL;
		}
	} else if (!bactext_property_index(s, &v) || v > MAX_BACNET_PROPERTY_ID) {
		return -EINVAL;
	}

	*prop = v;
	return 0;
}

int uc_units_from_str(const char *s, uint16_t *units)
{
	uint32_t v;

	if (s == NULL || *s == '\0' || units == NULL) {
		return -EINVAL;
	}

	if (starts_with_digit(s)) {
		if (parse_decimal(s, UINT16_MAX, &v) < 0) {
			return -EINVAL;
		}
	} else if (!bactext_engineering_unit_index(s, &v) || v > UINT16_MAX) {
		return -EINVAL;
	}

	*units = (uint16_t)v;
	return 0;
}

bool uc_obj_type_is_analog(uint16_t type)
{
	return type == OBJECT_ANALOG_INPUT || type == OBJECT_ANALOG_OUTPUT ||
	       type == OBJECT_ANALOG_VALUE;
}

bool uc_obj_type_is_binary(uint16_t type)
{
	return type == OBJECT_BINARY_INPUT || type == OBJECT_BINARY_OUTPUT ||
	       type == OBJECT_BINARY_VALUE;
}

bool uc_obj_type_is_multistate(uint16_t type)
{
	return type == OBJECT_MULTI_STATE_INPUT || type == OBJECT_MULTI_STATE_OUTPUT ||
	       type == OBJECT_MULTI_STATE_VALUE;
}

bool uc_obj_type_supported(uint16_t type)
{
	return uc_obj_type_is_analog(type) || uc_obj_type_is_binary(type) ||
	       uc_obj_type_is_multistate(type);
}

/* ---------------------------------------------------------------------- */
/* Values                                                                  */
/* ---------------------------------------------------------------------- */

int uc_value_to_double(const BACNET_APPLICATION_DATA_VALUE *v, double *out)
{
	if (v == NULL || out == NULL) {
		return -EINVAL;
	}

	switch (v->tag) {
#if defined(BACAPP_REAL)
	case BACNET_APPLICATION_TAG_REAL:
		*out = (double)v->type.Real;
		return 0;
#endif
#if defined(BACAPP_DOUBLE)
	case BACNET_APPLICATION_TAG_DOUBLE:
		*out = v->type.Double;
		return 0;
#endif
#if defined(BACAPP_UNSIGNED)
	case BACNET_APPLICATION_TAG_UNSIGNED_INT:
		*out = (double)v->type.Unsigned_Int;
		return 0;
#endif
#if defined(BACAPP_SIGNED)
	case BACNET_APPLICATION_TAG_SIGNED_INT:
		*out = (double)v->type.Signed_Int;
		return 0;
#endif
#if defined(BACAPP_ENUMERATED)
	case BACNET_APPLICATION_TAG_ENUMERATED:
		*out = (double)v->type.Enumerated;
		return 0;
#endif
#if defined(BACAPP_BOOLEAN)
	case BACNET_APPLICATION_TAG_BOOLEAN:
		*out = v->type.Boolean ? 1.0 : 0.0;
		return 0;
#endif
	default:
		return -EBADMSG;
	}
}

/*
 * Datatype of a numeric property. bacapp_known_property_tag() only knows
 * the constructed datatypes and returns -1 for primitive ones, so the
 * primitive numeric properties of the supported object types (and of the
 * device object) are listed here.
 */
static int numeric_property_tag(uint16_t object_type, uint32_t property)
{
	switch (property) {
	case PROP_PRESENT_VALUE:
	case PROP_RELINQUISH_DEFAULT:
	case PROP_PRIORITY_ARRAY:
		if (uc_obj_type_is_analog(object_type)) {
			return BACNET_APPLICATION_TAG_REAL;
		}
		if (uc_obj_type_is_binary(object_type)) {
			return BACNET_APPLICATION_TAG_ENUMERATED;
		}
		if (uc_obj_type_is_multistate(object_type)) {
			return BACNET_APPLICATION_TAG_UNSIGNED_INT;
		}
		return -1;
	case PROP_COV_INCREMENT:
	case PROP_HIGH_LIMIT:
	case PROP_LOW_LIMIT:
	case PROP_DEADBAND:
	case PROP_MIN_PRES_VALUE:
	case PROP_MAX_PRES_VALUE:
	case PROP_RESOLUTION:
		return BACNET_APPLICATION_TAG_REAL;
	case PROP_OUT_OF_SERVICE:
	case PROP_DAYLIGHT_SAVINGS_STATUS:
	case PROP_EVENT_DETECTION_ENABLE:
		return BACNET_APPLICATION_TAG_BOOLEAN;
	case PROP_UNITS:
	case PROP_POLARITY:
	case PROP_EVENT_STATE:
	case PROP_RELIABILITY:
	case PROP_NOTIFY_TYPE:
	case PROP_SYSTEM_STATUS:
	case PROP_SEGMENTATION_SUPPORTED:
		return BACNET_APPLICATION_TAG_ENUMERATED;
	case PROP_NUMBER_OF_STATES:
	case PROP_TIME_DELAY:
	case PROP_NOTIFICATION_CLASS:
	case PROP_APDU_TIMEOUT:
	case PROP_APDU_SEGMENT_TIMEOUT:
	case PROP_NUMBER_OF_APDU_RETRIES:
	case PROP_MAX_APDU_LENGTH_ACCEPTED:
	case PROP_MAX_SEGMENTS_ACCEPTED:
	case PROP_VENDOR_IDENTIFIER:
	case PROP_DATABASE_REVISION:
	case PROP_PROTOCOL_VERSION:
	case PROP_PROTOCOL_REVISION:
	case PROP_MINIMUM_ON_TIME:
	case PROP_MINIMUM_OFF_TIME:
	case PROP_CHANGE_OF_STATE_COUNT:
	case PROP_ELAPSED_ACTIVE_TIME:
	case PROP_MAX_INFO_FRAMES:
	case PROP_MAX_MASTER:
		return BACNET_APPLICATION_TAG_UNSIGNED_INT;
	case PROP_UTC_OFFSET:
		return BACNET_APPLICATION_TAG_SIGNED_INT;
	default:
		return -1;
	}
}

int uc_value_from_double(uint16_t object_type, uint32_t property, double in,
			 BACNET_APPLICATION_DATA_VALUE *out)
{
	int tag;
	double t;

	if (out == NULL) {
		return -EINVAL;
	}

	tag = bacapp_known_property_tag((BACNET_OBJECT_TYPE)object_type,
					(BACNET_PROPERTY_ID)property);
	if (tag < 0) {
		tag = numeric_property_tag(object_type, property);
	}

	memset(out, 0, sizeof(*out));
	t = trunc(in);

	switch (tag) {
#if defined(BACAPP_REAL)
	case BACNET_APPLICATION_TAG_REAL:
		out->tag = BACNET_APPLICATION_TAG_REAL;
		out->type.Real = (float)in;
		return 0;
#endif
#if defined(BACAPP_DOUBLE)
	case BACNET_APPLICATION_TAG_DOUBLE:
		out->tag = BACNET_APPLICATION_TAG_DOUBLE;
		out->type.Double = in;
		return 0;
#endif
#if defined(BACAPP_UNSIGNED)
	case BACNET_APPLICATION_TAG_UNSIGNED_INT:
		if (isnan(in) || t < 0.0 || t > (double)UINT32_MAX) {
			return -EINVAL;
		}
		out->tag = BACNET_APPLICATION_TAG_UNSIGNED_INT;
		out->type.Unsigned_Int = (BACNET_UNSIGNED_INTEGER)t;
		return 0;
#endif
#if defined(BACAPP_SIGNED)
	case BACNET_APPLICATION_TAG_SIGNED_INT:
		if (isnan(in) || t < (double)INT32_MIN || t > (double)INT32_MAX) {
			return -EINVAL;
		}
		out->tag = BACNET_APPLICATION_TAG_SIGNED_INT;
		out->type.Signed_Int = (int32_t)t;
		return 0;
#endif
#if defined(BACAPP_ENUMERATED)
	case BACNET_APPLICATION_TAG_ENUMERATED:
		if (isnan(in)) {
			return -EINVAL;
		}
		out->tag = BACNET_APPLICATION_TAG_ENUMERATED;
		if (property == PROP_PRESENT_VALUE || property == PROP_RELINQUISH_DEFAULT ||
		    property == PROP_PRIORITY_ARRAY) {
			/* binary PV: 0.0 inactive, anything else active */
			out->type.Enumerated = (in != 0.0) ? BINARY_ACTIVE : BINARY_INACTIVE;
			return 0;
		}
		if (t < 0.0 || t > (double)UINT32_MAX) {
			return -EINVAL;
		}
		out->type.Enumerated = (uint32_t)t;
		return 0;
#endif
#if defined(BACAPP_BOOLEAN)
	case BACNET_APPLICATION_TAG_BOOLEAN:
		if (isnan(in)) {
			return -EINVAL;
		}
		out->tag = BACNET_APPLICATION_TAG_BOOLEAN;
		out->type.Boolean = (in != 0.0);
		return 0;
#endif
	default:
		return -EBADMSG;
	}
}

/* ---------------------------------------------------------------------- */
/* Strings and validators                                                  */
/* ---------------------------------------------------------------------- */

int uc_strlcpy(char *dst, const char *src, size_t dst_size)
{
	size_t n;

	if (dst == NULL || dst_size == 0) {
		return -ENOSPC;
	}

	if (src == NULL) {
		dst[0] = '\0';
		return 0;
	}

	n = strlen(src);
	if (n >= dst_size) {
		memcpy(dst, src, dst_size - 1);
		dst[dst_size - 1] = '\0';
		return -ENOSPC;
	}

	memcpy(dst, src, n + 1);
	return 0;
}

bool uc_app_name_valid(const char *name)
{
	size_t n = 0;

	if (name == NULL) {
		return false;
	}

	for (const char *p = name; *p != '\0'; p++, n++) {
		char c = *p;

		if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' ||
		      c == '-')) {
			return false;
		}
	}

	return n >= 1 && n <= UC_APP_NAME_MAX - 1;
}

bool uc_key_valid(const char *key, size_t max_len)
{
	size_t n = 0;

	if (key == NULL) {
		return false;
	}

	for (const char *p = key; *p != '\0'; p++, n++) {
		char c = *p;

		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
		      (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-')) {
			return false;
		}
	}

	if (n < 1 || n > max_len) {
		return false;
	}

	return strcmp(key, ".") != 0 && strcmp(key, "..") != 0;
}
