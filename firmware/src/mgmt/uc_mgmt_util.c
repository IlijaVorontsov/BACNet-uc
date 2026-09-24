/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shared helpers of the custom MCUmgr groups and group registration
 * (uc_mgmt_init). See uc_mgmt_util.h and docs/management-protocol.md.
 */

#include <math.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>

#include <zcbor_common.h>
#include <zcbor_decode.h>
#include <zcbor_encode.h>

#include "bacnet/bacstr.h"

#include "uc_mgmt_util.h"

LOG_MODULE_REGISTER(uc_mgmt, CONFIG_UC_LOG_LEVEL);

union uc_mgmt_scratch uc_mgmt_scratch;

/* errno recorded by the failing decoder of the current request */
static int dec_err;

/* Longest object type / property text name accepted in requests. */
#define NAME_TEXT_MAX 48

/* CBOR initial bytes of simple values and floats (major type 7) */
#define CBOR_FALSE 0xF4
#define CBOR_TRUE  0xF5
#define CBOR_NULL  0xF6
#define CBOR_F16   0xF9
#define CBOR_F32   0xFA
#define CBOR_F64   0xFB

/* ---------------------------------------------------------------------- */
/* Response budget                                                         */
/* ---------------------------------------------------------------------- */

size_t uc_mgmt_room(const struct smp_streamer *ctxt)
{
	const zcbor_state_t *zse = ctxt->writer->zs;
	const uint8_t *start = ctxt->writer->nb->data + MGMT_HDR_SIZE;
	size_t used = (size_t)(zse->payload - start);
	size_t cap = (size_t)(zse->payload_end - start);
	size_t limit = MIN(cap, (size_t)UC_MGMT_RSP_MAX);

	return (used < limit) ? (limit - used) : 0;
}

void uc_mgmt_mark_set(const zcbor_state_t *zse, struct uc_mgmt_mark *m)
{
	m->payload = zse->payload;
	m->elem_count = zse->elem_count;
	m->backup = (zse->constant_state != NULL) ? zse->constant_state->current_backup : 0;
}

void uc_mgmt_rewind(zcbor_state_t *zse, const struct uc_mgmt_mark *m)
{
	zse->payload = m->payload;
	zse->elem_count = m->elem_count;
	if (zse->constant_state != NULL) {
		/* drops backups of containers opened after the mark
		 * (ZCBOR_CANONICAL only)
		 */
		zse->constant_state->current_backup = m->backup;
	}
	(void)zcbor_pop_error(zse);
}

bool uc_mgmt_elem_commit(struct smp_streamer *ctxt, const struct uc_mgmt_mark *m, bool ok)
{
	if (ok && (uc_mgmt_room(ctxt) >= UC_MGMT_RSP_RESERVE)) {
		return true;
	}
	uc_mgmt_rewind(ctxt->writer->zs, m);

	return false;
}

/* ---------------------------------------------------------------------- */
/* Errors                                                                  */
/* ---------------------------------------------------------------------- */

int uc_mgmt_rc(struct smp_streamer *ctxt, uint16_t group, int rc)
{
	bool ok;

	if (rc == UC_MGMT_RC_OK) {
		return MGMT_ERR_EOK;
	}
	ok = smp_add_cmd_err(ctxt->writer->zs, group, (uint16_t)rc);

	return MGMT_RETURN_CHECK(ok);
}

int uc_mgmt_errno(struct smp_streamer *ctxt, uint16_t group, int err)
{
	return uc_mgmt_rc(ctxt, group, uc_err_to_mgmt(err));
}

int uc_mgmt_translate_error(uint16_t err)
{
	switch (err) {
	case UC_MGMT_RC_OK:
		return MGMT_ERR_EOK;
	case UC_MGMT_RC_INVALID:
		return MGMT_ERR_EINVAL;
	case UC_MGMT_RC_NOT_FOUND:
		return MGMT_ERR_ENOENT;
	case UC_MGMT_RC_EXISTS:
	case UC_MGMT_RC_STATE:
		return MGMT_ERR_EBADSTATE;
	case UC_MGMT_RC_BUSY:
		return MGMT_ERR_EBUSY;
	case UC_MGMT_RC_NO_MEM:
	case UC_MGMT_RC_LIMIT:
		return MGMT_ERR_ENOMEM;
	case UC_MGMT_RC_VERIFY:
		return MGMT_ERR_ECORRUPT;
	case UC_MGMT_RC_PERM:
		return MGMT_ERR_EACCESSDENIED;
	case UC_MGMT_RC_UNSUPPORTED:
		return MGMT_ERR_ENOTSUP;
	case UC_MGMT_RC_IO:
	case UC_MGMT_RC_UNKNOWN:
	default:
		return MGMT_ERR_EUNKNOWN;
	}
}

/* ---------------------------------------------------------------------- */
/* Request decoding                                                        */
/* ---------------------------------------------------------------------- */

bool uc_mgmt_dec_fail(int err)
{
	if (dec_err == 0) {
		dec_err = err;
	}

	return false;
}

int uc_mgmt_decode(struct smp_streamer *ctxt, struct zcbor_map_decode_key_val *map,
		   size_t map_size)
{
	zcbor_state_t *zsd = ctxt->reader->zs;
	size_t decoded = 0;

	dec_err = 0;
	if (zsd->payload >= zsd->payload_end) {
		/* no payload at all: same as {} */
		return 0;
	}
	if (zcbor_map_decode_bulk(zsd, map, map_size, &decoded) != 0) {
		return (dec_err != 0) ? dec_err : -EINVAL;
	}

	return 0;
}

bool uc_mgmt_found(struct zcbor_map_decode_key_val *map, size_t map_size, const char *key)
{
	return zcbor_map_decode_bulk_key_found(map, map_size, key);
}

/* Initial byte of the next element, -1 if there is none. */
static int peek_initial(const zcbor_state_t *zsd)
{
	if ((zsd->payload >= zsd->payload_end) || (zsd->elem_count == 0)) {
		return -1;
	}

	return *zsd->payload;
}

/* Copy a decoded text into buf (size incl. NUL); rejects NUL bytes. */
static bool text_copy(const struct zcbor_string *s, char *buf, size_t size)
{
	if ((s->len >= size) || (memchr(s->value, '\0', s->len) != NULL)) {
		return false;
	}
	memcpy(buf, s->value, s->len);
	buf[s->len] = '\0';

	return true;
}

bool uc_mgmt_dec_tstr(zcbor_state_t *zsd, struct uc_mgmt_tstr *out)
{
	struct zcbor_string s;

	if (!zcbor_tstr_decode(zsd, &s) || !text_copy(&s, out->buf, out->size)) {
		return uc_mgmt_dec_fail(-EINVAL);
	}

	return true;
}

bool uc_mgmt_dec_double(zcbor_state_t *zsd, double *out)
{
	int ib = peek_initial(zsd);
	int64_t i;

	if (ib < 0) {
		return uc_mgmt_dec_fail(-EINVAL);
	}

	switch (ZCBOR_MAJOR_TYPE((uint8_t)ib)) {
	case ZCBOR_MAJOR_TYPE_PINT:
	case ZCBOR_MAJOR_TYPE_NINT:
		if (!zcbor_int64_decode(zsd, &i)) {
			return uc_mgmt_dec_fail(-EINVAL);
		}
		*out = (double)i;
		return true;
	case ZCBOR_MAJOR_TYPE_SIMPLE:
		if (((ib == CBOR_F16) || (ib == CBOR_F32) || (ib == CBOR_F64)) &&
		    zcbor_float_decode(zsd, out)) {
			return true;
		}
		return uc_mgmt_dec_fail(-EINVAL);
	default:
		return uc_mgmt_dec_fail(-EINVAL);
	}
}

/* Text-or-uint identifier: numbers up to max, text through from_str. */
static bool dec_ident(zcbor_state_t *zsd, uint32_t max, uint32_t *out,
		      int (*from_str)(const char *s, uint32_t *v))
{
	int ib = peek_initial(zsd);
	struct zcbor_string s;
	char buf[NAME_TEXT_MAX];
	uint32_t v;

	if (ib < 0) {
		return uc_mgmt_dec_fail(-EINVAL);
	}

	switch (ZCBOR_MAJOR_TYPE((uint8_t)ib)) {
	case ZCBOR_MAJOR_TYPE_PINT:
		if (!zcbor_uint32_decode(zsd, &v) || (v > max)) {
			return uc_mgmt_dec_fail(-EINVAL);
		}
		break;
	case ZCBOR_MAJOR_TYPE_TSTR:
		if (!zcbor_tstr_decode(zsd, &s) || !text_copy(&s, buf, sizeof(buf)) ||
		    (from_str(buf, &v) < 0) || (v > max)) {
			return uc_mgmt_dec_fail(-EINVAL);
		}
		break;
	default:
		return uc_mgmt_dec_fail(-EINVAL);
	}

	*out = v;
	return true;
}

static int obj_type_from_str32(const char *s, uint32_t *v)
{
	uint16_t type;
	int rc = uc_obj_type_from_str(s, &type);

	if (rc == 0) {
		*v = type;
	}

	return rc;
}

bool uc_mgmt_dec_obj_type(zcbor_state_t *zsd, uint16_t *out)
{
	uint32_t v;

	if (!dec_ident(zsd, BACNET_MAX_OBJECT, &v, obj_type_from_str32)) {
		return false;
	}
	*out = (uint16_t)v;

	return true;
}

bool uc_mgmt_dec_prop(zcbor_state_t *zsd, uint32_t *out)
{
	return dec_ident(zsd, MAX_BACNET_PROPERTY_ID, out, uc_prop_from_str);
}

bool uc_mgmt_dec_value(zcbor_state_t *zsd, struct uc_mgmt_value *out)
{
	int ib = peek_initial(zsd);
	struct zcbor_string s;

	if (ib < 0) {
		return uc_mgmt_dec_fail(-EINVAL);
	}

	memset(out, 0, sizeof(*out));
	switch (ZCBOR_MAJOR_TYPE((uint8_t)ib)) {
	case ZCBOR_MAJOR_TYPE_PINT:
	case ZCBOR_MAJOR_TYPE_NINT:
		out->kind = UC_MGMT_VAL_NUMBER;
		return uc_mgmt_dec_double(zsd, &out->number);
	case ZCBOR_MAJOR_TYPE_TSTR:
		/* CharacterString capacity: MAX_CHARACTER_STRING_BYTES - 1 */
		if (!zcbor_tstr_decode(zsd, &s) || !text_copy(&s, out->text, sizeof(out->text))) {
			return uc_mgmt_dec_fail(-EINVAL);
		}
		out->kind = UC_MGMT_VAL_TEXT;
		return true;
	case ZCBOR_MAJOR_TYPE_SIMPLE:
		if ((ib == CBOR_FALSE) || (ib == CBOR_TRUE)) {
			out->kind = UC_MGMT_VAL_BOOL;
			return zcbor_bool_decode(zsd, &out->boolean) || uc_mgmt_dec_fail(-EINVAL);
		}
		if (ib == CBOR_NULL) {
			out->kind = UC_MGMT_VAL_NULL;
			return zcbor_nil_expect(zsd, NULL) || uc_mgmt_dec_fail(-EINVAL);
		}
		out->kind = UC_MGMT_VAL_NUMBER;
		return uc_mgmt_dec_double(zsd, &out->number);
	default:
		return uc_mgmt_dec_fail(-EINVAL);
	}
}

int uc_mgmt_value_to_bacnet(const struct uc_mgmt_value *in, uint16_t type, uint32_t prop,
			    BACNET_APPLICATION_DATA_VALUE *out)
{
	memset(out, 0, sizeof(*out));

	switch (in->kind) {
	case UC_MGMT_VAL_NUMBER:
		return uc_value_from_double(type, prop, in->number, out);
	case UC_MGMT_VAL_BOOL:
		return uc_value_from_double(type, prop, in->boolean ? 1.0 : 0.0, out);
	case UC_MGMT_VAL_TEXT:
		out->tag = BACNET_APPLICATION_TAG_CHARACTER_STRING;
		if (!characterstring_init_ansi(&out->type.Character_String, in->text)) {
			return -EINVAL;
		}
		return 0;
	case UC_MGMT_VAL_NULL:
		out->tag = BACNET_APPLICATION_TAG_NULL;
		return 0;
	default:
		return -EINVAL;
	}
}

/* ---------------------------------------------------------------------- */
/* Response encoding                                                       */
/* ---------------------------------------------------------------------- */

/* Length of the well-formed UTF-8 sequence at s (avail bytes), 0 if the
 * bytes do not start one (RFC 3629: no overlongs, no surrogates).
 */
static size_t utf8_seq_len(const uint8_t *s, size_t avail)
{
	uint8_t c = s[0];
	size_t n;

	if (c < 0x80U) {
		return 1;
	} else if ((c >= 0xC2U) && (c <= 0xDFU)) {
		n = 2;
	} else if ((c >= 0xE0U) && (c <= 0xEFU)) {
		n = 3;
	} else if ((c >= 0xF0U) && (c <= 0xF4U)) {
		n = 4;
	} else {
		return 0;
	}
	if (n > avail) {
		return 0;
	}
	for (size_t i = 1; i < n; i++) {
		if ((s[i] & 0xC0U) != 0x80U) {
			return 0;
		}
	}
	if (((c == 0xE0U) && (s[1] < 0xA0U)) || ((c == 0xEDU) && (s[1] >= 0xA0U)) ||
	    ((c == 0xF0U) && (s[1] < 0x90U)) || ((c == 0xF4U) && (s[1] >= 0x90U))) {
		return 0;
	}

	return n;
}

/* Encode len bytes of s as text of at most max_len bytes. ascii_only
 * replaces every non-ASCII byte (for non-UTF-8 CharacterStrings).
 */
static bool put_text(zcbor_state_t *zse, const char *s, size_t len, size_t max_len,
		     bool ascii_only)
{
	char buf[UC_MGMT_TSTR_MAX];
	const uint8_t *in = (const uint8_t *)s;
	size_t o = 0;
	size_t i = 0;

	max_len = MIN(max_len, sizeof(buf));
	while ((i < len) && (in[i] != '\0')) {
		size_t n = ascii_only ? ((in[i] < 0x80U) ? 1U : 0U) : utf8_seq_len(&in[i], len - i);

		if (o + MAX(n, (size_t)1) > max_len) {
			break;
		}
		if (n == 0) {
			buf[o++] = '?';
			i++;
			continue;
		}
		memcpy(&buf[o], &in[i], n);
		o += n;
		i += n;
	}

	return zcbor_tstr_encode_ptr(zse, buf, o);
}

bool uc_mgmt_put_tstr(zcbor_state_t *zse, const char *s, size_t max_len)
{
	if (s == NULL) {
		return zcbor_tstr_put_lit(zse, "");
	}

	/* look 3 bytes past max_len so that a character crossing the limit is
	 * dropped instead of being replaced
	 */
	return put_text(zse, s, strnlen(s, max_len + 3), max_len, false);
}

bool uc_mgmt_put_float(zcbor_state_t *zse, double v)
{
	float f = (float)v;

	if (!isnan(v) && ((double)f != v)) {
		return zcbor_float64_put(zse, v);
	}
	if (isnan(f) || (zcbor_float16_to_32(zcbor_float32_to_16(f)) == f)) {
		return zcbor_float16_put(zse, f);
	}

	return zcbor_float32_put(zse, f);
}

bool uc_mgmt_put_obj_type(zcbor_state_t *zse, uint16_t type)
{
	const char *name = uc_obj_type_to_str(type);
	uint16_t back;
	char num[8];

	if ((name != NULL) && (uc_obj_type_from_str(name, &back) == 0) && (back == type)) {
		return zcbor_tstr_put_term(zse, name, NAME_TEXT_MAX);
	}
	(void)snprintk(num, sizeof(num), "%u", (unsigned int)type);

	return zcbor_tstr_put_term(zse, num, sizeof(num));
}

/* Datatypes without a natural CBOR type: the stack's text rendering. */
static bool put_value_text(zcbor_state_t *zse, uint16_t type, uint32_t instance, uint32_t prop,
			   const BACNET_APPLICATION_DATA_VALUE *v)
{
	BACNET_APPLICATION_DATA_VALUE copy = *v;
	BACNET_OBJECT_PROPERTY_VALUE opv = {
		.object_type = (BACNET_OBJECT_TYPE)type,
		.object_instance = instance,
		.object_property = (BACNET_PROPERTY_ID)prop,
		.array_index = BACNET_ARRAY_ALL,
		.value = &copy,
	};
	char buf[UC_MGMT_TSTR_MAX + 1];

	buf[0] = '\0';
	(void)bacapp_snprintf_value(buf, sizeof(buf), &opv);
	buf[sizeof(buf) - 1] = '\0';

	return uc_mgmt_put_tstr(zse, buf, UC_MGMT_TSTR_MAX);
}

bool uc_mgmt_put_value(zcbor_state_t *zse, uint16_t type, uint32_t instance, uint32_t prop,
		       const BACNET_APPLICATION_DATA_VALUE *v)
{
	switch (v->tag) {
	case BACNET_APPLICATION_TAG_NULL:
		return zcbor_nil_put(zse, NULL);
#if defined(BACAPP_BOOLEAN)
	case BACNET_APPLICATION_TAG_BOOLEAN:
		return zcbor_bool_put(zse, v->type.Boolean);
#endif
#if defined(BACAPP_UNSIGNED)
	case BACNET_APPLICATION_TAG_UNSIGNED_INT:
		return zcbor_uint64_put(zse, (uint64_t)v->type.Unsigned_Int);
#endif
#if defined(BACAPP_SIGNED)
	case BACNET_APPLICATION_TAG_SIGNED_INT:
		return zcbor_int64_put(zse, (int64_t)v->type.Signed_Int);
#endif
#if defined(BACAPP_REAL)
	case BACNET_APPLICATION_TAG_REAL:
		return uc_mgmt_put_float(zse, (double)v->type.Real);
#endif
#if defined(BACAPP_DOUBLE)
	case BACNET_APPLICATION_TAG_DOUBLE:
		return uc_mgmt_put_float(zse, v->type.Double);
#endif
#if defined(BACAPP_ENUMERATED)
	case BACNET_APPLICATION_TAG_ENUMERATED:
		return zcbor_uint32_put(zse, v->type.Enumerated);
#endif
#if defined(BACAPP_CHARACTER_STRING)
	case BACNET_APPLICATION_TAG_CHARACTER_STRING: {
		const BACNET_CHARACTER_STRING *cs = &v->type.Character_String;

		return put_text(zse, characterstring_value_const(cs), characterstring_length(cs),
				UC_MGMT_TSTR_MAX, characterstring_encoding(cs) != CHARACTER_UTF8);
	}
#endif
#if defined(BACAPP_OCTET_STRING)
	case BACNET_APPLICATION_TAG_OCTET_STRING: {
		const BACNET_OCTET_STRING *os = &v->type.Octet_String;

		return zcbor_bstr_encode_ptr(zse, (const char *)octetstring_value_const(os),
					     MIN(octetstring_length(os), (size_t)UC_MGMT_TSTR_MAX));
	}
#endif
	default:
		return put_value_text(zse, type, instance, prop, v);
	}
}

/* ---------------------------------------------------------------------- */
/* Registration                                                            */
/* ---------------------------------------------------------------------- */

int uc_mgmt_init(void)
{
	static bool registered;

	if (registered) {
		return 0;
	}

	uc_mgmt_app_register();
	uc_mgmt_io_register();
	uc_mgmt_node_register();
	registered = true;

	LOG_INF("SMP groups registered: %d uc_app%s, %d uc_io, %d uc_node", UC_MGMT_GROUP_APP,
		IS_ENABLED(CONFIG_UC_APPS) ? "" : " (unsupported)", UC_MGMT_GROUP_IO,
		UC_MGMT_GROUP_NODE);

	return 0;
}
