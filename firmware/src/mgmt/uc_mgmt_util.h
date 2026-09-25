/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Helpers shared by the custom MCUmgr groups (uc_app, uc_io, uc_node):
 * request decoding, response encoding within the transport budget, SMP v2
 * group errors and the shared scratch area.
 *
 * Threading: every SMP handler runs in the single MCUmgr transport work
 * queue, so handlers never run concurrently. The static scratch area and
 * the decode error slot below rely on that.
 */
#ifndef UC_MGMT_UTIL_H_
#define UC_MGMT_UTIL_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <zcbor_common.h>
#include <zephyr/mgmt/mcumgr/mgmt/mgmt.h>
#include <zephyr/mgmt/mcumgr/smp/smp.h>
#include <mgmt/mcumgr/util/zcbor_bulk.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacapp.h"

#include "uc/uc_common.h"
#include "uc/uc_config.h"
#include "uc/uc_bacnet.h"
#include "uc/uc_mgmt.h"
/* declarations only; the functions exist with CONFIG_UC_APPS */
#include "uc/uc_apps.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ---------------------------------------------------------------------- */
/* Response budget                                                         */
/* ---------------------------------------------------------------------- */

/* Largest CBOR payload a response may use: one UDP datagram of
 * CONFIG_MCUMGR_TRANSPORT_UDP_MTU bytes including the 8-byte SMP header.
 * The encoder buffer itself (CONFIG_MCUMGR_TRANSPORT_NETBUF_SIZE) is
 * slightly larger; uc_mgmt_room() uses the smaller of both.
 */
#if defined(CONFIG_MCUMGR_TRANSPORT_UDP)
#define UC_MGMT_RSP_MAX (CONFIG_MCUMGR_TRANSPORT_UDP_MTU - MGMT_HDR_SIZE)
#else
#define UC_MGMT_RSP_MAX (CONFIG_MCUMGR_TRANSPORT_NETBUF_SIZE - MGMT_HDR_SIZE)
#endif

/* Bytes kept free after the last list element: list and main map
 * terminators (1 byte each without ZCBOR_CANONICAL) plus margin.
 */
#define UC_MGMT_RSP_RESERVE 8

/* Longest text a response carries for free-form strings. */
#define UC_MGMT_TSTR_MAX 96

/** Bytes still available for the response payload. */
size_t uc_mgmt_room(const struct smp_streamer *ctxt);

/** Encoder position, used to drop a partially encoded list element. */
struct uc_mgmt_mark {
	const uint8_t *payload;
	size_t elem_count;
	size_t backup;
};

void uc_mgmt_mark_set(const zcbor_state_t *zse, struct uc_mgmt_mark *m);

/** Rewind the encoder to m and clear the zcbor error state. */
void uc_mgmt_rewind(zcbor_state_t *zse, const struct uc_mgmt_mark *m);

/** After encoding one list element: true when it was encoded completely
 *  (ok) and the response still has UC_MGMT_RSP_RESERVE bytes left.
 *  Otherwise the element is removed again (rewind to m) and false is
 *  returned; the caller then closes the list. */
bool uc_mgmt_elem_commit(struct smp_streamer *ctxt, const struct uc_mgmt_mark *m, bool ok);

/* ---------------------------------------------------------------------- */
/* Errors                                                                  */
/* ---------------------------------------------------------------------- */

/** Add {"err": {"group": group, "rc": rc}} when rc != UC_MGMT_RC_OK.
 *  Returns the handler result (MGMT_ERR_EOK or MGMT_ERR_EMSGSIZE). */
int uc_mgmt_rc(struct smp_streamer *ctxt, uint16_t group, int rc);

/** uc_mgmt_rc() with a negative errno mapped by uc_err_to_mgmt(). */
int uc_mgmt_errno(struct smp_streamer *ctxt, uint16_t group, int err);

/** SMP v1 (legacy) translation of the group rc values. */
int uc_mgmt_translate_error(uint16_t err);

/* ---------------------------------------------------------------------- */
/* Request decoding                                                        */
/* ---------------------------------------------------------------------- */

/** Decode the request map with zcbor_map_decode_bulk(). An empty payload
 *  counts as an empty map. Returns 0, or the errno a decoder recorded with
 *  uc_mgmt_dec_fail() (default -EINVAL). */
int uc_mgmt_decode(struct smp_streamer *ctxt, struct zcbor_map_decode_key_val *map,
		   size_t map_size);

/** Record why a decoder failed (first error wins until the next
 *  uc_mgmt_decode()). Always returns false for use in return statements. */
bool uc_mgmt_dec_fail(int err);

/** True if key was present in the decoded request. */
bool uc_mgmt_found(struct zcbor_map_decode_key_val *map, size_t map_size, const char *key);

/* Decoders with the zcbor_decoder_t signature, for
 * ZCBOR_MAP_DECODE_KEY_DECODER(key, decoder, value_ptr).
 */

/** Text into a NUL-terminated buffer. Fails (-EINVAL) for other types,
 *  embedded NUL bytes or text that does not fit. */
struct uc_mgmt_tstr {
	char *buf;
	size_t size;
};
bool uc_mgmt_dec_tstr(zcbor_state_t *zsd, struct uc_mgmt_tstr *out);

/** Any CBOR float width or an integer. */
bool uc_mgmt_dec_double(zcbor_state_t *zsd, double *out);

/** Object type as BACnet text name ("analog-value") or number. */
bool uc_mgmt_dec_obj_type(zcbor_state_t *zsd, uint16_t *out);

/** Property identifier as BACnet text name ("present-value") or number. */
bool uc_mgmt_dec_prop(zcbor_state_t *zsd, uint32_t *out);

/** A <value> of the protocol: number, bool, text or null. */
enum uc_mgmt_value_kind {
	UC_MGMT_VAL_NUMBER,
	UC_MGMT_VAL_BOOL,
	UC_MGMT_VAL_TEXT,
	UC_MGMT_VAL_NULL,
};

struct uc_mgmt_value {
	enum uc_mgmt_value_kind kind;
	double number;
	bool boolean;
	char text[MAX_CHARACTER_STRING_BYTES];
};

bool uc_mgmt_dec_value(zcbor_state_t *zsd, struct uc_mgmt_value *out);

/** Convert a decoded <value> into the application value a property
 *  expects: numbers and bools via uc_value_from_double(), text as UTF-8
 *  CharacterString, null as NULL. -EBADMSG if not convertible, -EINVAL for
 *  a number that the datatype cannot hold exactly (not finite, a fraction
 *  for an integer datatype, other than 0/1 for a binary present value). */
int uc_mgmt_value_to_bacnet(const struct uc_mgmt_value *in, uint16_t type, uint32_t prop,
			    BACNET_APPLICATION_DATA_VALUE *out);

/* ---------------------------------------------------------------------- */
/* Response encoding                                                       */
/* ---------------------------------------------------------------------- */

/** Text, truncated to max_len bytes on a UTF-8 character boundary and with
 *  invalid UTF-8 replaced by '?' (max_len <= UC_MGMT_TSTR_MAX). NULL
 *  encodes "". */
bool uc_mgmt_put_tstr(zcbor_state_t *zse, const char *s, size_t max_len);

/** Float in the shortest CBOR width that represents v exactly. */
bool uc_mgmt_put_float(zcbor_state_t *zse, double v);

/** Object type as the BACnet text name, or its number as text when the
 *  stack has no name for it. */
bool uc_mgmt_put_obj_type(zcbor_state_t *zse, uint16_t type);

/** BACnet application value as its natural CBOR type (see
 *  docs/management-protocol.md "<value>"): REAL/DOUBLE float,
 *  UNSIGNED/ENUMERATED uint, SIGNED int, BOOLEAN bool, CharacterString
 *  tstr, NULL null, OctetString bstr. Other datatypes are encoded as their
 *  text representation (bacapp_snprintf_value). */
bool uc_mgmt_put_value(zcbor_state_t *zse, uint16_t type, uint32_t instance, uint32_t prop,
		       const BACNET_APPLICATION_DATA_VALUE *v);

/* ---------------------------------------------------------------------- */
/* Shared scratch and group registration                                   */
/* ---------------------------------------------------------------------- */

/* Objects fetched per uc_bn_obj_list() call by uc_node objects. */
#define UC_MGMT_OBJ_CHUNK 4

/* Large structures used by the handlers. The MCUmgr work queue stack
 * (CONFIG_MCUMGR_TRANSPORT_WORKQUEUE_STACK_SIZE) cannot hold them; one
 * handler runs at a time, so they share this static area.
 */
union uc_mgmt_scratch {
	struct uc_app_cfg app_cfg;
#if defined(CONFIG_UC_APPS)
	struct uc_app_status app_status;
#endif
	struct uc_device_cfg device_cfg;
	struct uc_bn_obj_info objs[UC_MGMT_OBJ_CHUNK];
	BACNET_APPLICATION_DATA_VALUE value;
	/* uc_node prop_read of a whole array/list: encoded value (one APDU)
	 * and the element being converted */
	struct {
		uint8_t data[MAX_APDU];
		BACNET_APPLICATION_DATA_VALUE value;
	} prop;
};

extern union uc_mgmt_scratch uc_mgmt_scratch;

void uc_mgmt_app_register(void);
void uc_mgmt_io_register(void);
void uc_mgmt_node_register(void);

#ifdef __cplusplus
}
#endif

#endif /* UC_MGMT_UTIL_H_ */
