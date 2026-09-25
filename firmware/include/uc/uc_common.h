/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shared definitions for the BACnet-uc firmware modules.
 *
 * Error convention inside the firmware: functions return 0 (or a positive
 * count/id) on success and a negative errno on failure. The mapping to the
 * WASM ABI (UC_ERR_*) and to the management rc values happens only at the
 * boundaries (uc_err_to_api(), uc_err_to_mgmt()):
 *
 *   -EINVAL       invalid argument          UC_ERR_INVALID      INVALID
 *   -ENOENT       not found                 UC_ERR_NOT_FOUND    NOT_FOUND
 *   -EACCES       permission                UC_ERR_PERM         PERM
 *   -ETIMEDOUT    no confirmation           UC_ERR_TIMEOUT      BUSY
 *   -ECANCELED    request abandoned (stop)  UC_ERR_TIMEOUT      BUSY
 *   -EBUSY/-EAGAIN busy / slots exhausted   UC_ERR_BUSY         BUSY
 *   -ENOMEM       allocation failed         UC_ERR_NO_MEM       NO_MEM
 *   -EREMOTEIO    BACnet Error/Reject/Abort UC_ERR_BACNET       UNKNOWN
 *   -ENOTSUP      not built in              UC_ERR_UNSUPPORTED  UNSUPPORTED
 *   -EIO          hardware / fs error       UC_ERR_IO           IO
 *   -EBADMSG      datatype not convertible  UC_ERR_TYPE         INVALID
 *   -EEXIST       already exists            UC_ERR_EXISTS       EXISTS
 *   -EHOSTUNREACH device not bound          UC_ERR_NO_ROUTE     NOT_FOUND
 *   -EALREADY/-ESRCH wrong state            UC_ERR_INVALID      STATE
 *   -EILSEQ       verification failed       UC_ERR_INVALID      VERIFY
 *   -ENOSPC       table full                UC_ERR_NO_MEM       LIMIT
 */
#ifndef UC_COMMON_H_
#define UC_COMMON_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include <errno.h>

#include <zephyr/sys/util.h>

#include "bacnet/bacdef.h"
#include "bacnet/bacapp.h"

#ifdef __cplusplus
extern "C" {
#endif

/* EREMOTEIO is Linux-specific: glibc (native_sim) defines it, picolibc does
 * not. picolibc reserves values from __ELASTERROR for users; 121 (the Linux
 * value) would collide with picolibc's EDESTADDRREQ. */
#ifndef EREMOTEIO
#ifdef __ELASTERROR
#define EREMOTEIO (__ELASTERROR + 1)
#else
#define EREMOTEIO 2001
#endif
#endif

#define UC_NAME_MAX      64 /* object names, descriptions (incl. NUL) */
#define UC_APP_NAME_MAX  24 /* incl. NUL */
/* incl. NUL; fits /lfs/data/<23-char app>/<31-char key> plus a ".tmp" or
 * ".new" suffix (69 chars) */
#define UC_PATH_MAX      96
#define UC_CHANNEL_NAME_MAX 16 /* incl. NUL */

/* Owner of a local BACnet object (who created it, who gets write events). */
#define UC_OWNER_NONE     0u  /* unknown / pre-existing */
#define UC_OWNER_SYSTEM   1u  /* device object, network port, ... */
#define UC_OWNER_IO       2u  /* created from io.json */
#define UC_OWNER_APP_BASE 16u /* UC_OWNER_APP_BASE + app slot index */
#define UC_OWNER_IS_APP(o) ((o) >= UC_OWNER_APP_BASE)
#define UC_OWNER_APP_SLOT(o) ((o) - UC_OWNER_APP_BASE)

/* Application permissions (apps.json "perms"). */
#define UC_PERM_BACNET_LOCAL  BIT(0) /* "bacnet.local"  */
#define UC_PERM_BACNET_REMOTE BIT(1) /* "bacnet.remote" */
#define UC_PERM_IO            BIT(2) /* "io"            */
#define UC_PERM_KV            BIT(3) /* "kv"            */

/** Parse one permission name, returns the bit or 0 if unknown. */
uint32_t uc_perm_from_str(const char *name);
/** Name of a single permission bit, NULL if not a single known bit. */
const char *uc_perm_to_str(uint32_t bit);

/* Error mapping, see the table above. */
int uc_err_to_api(int err);
int uc_err_to_mgmt(int err);
const char *uc_err_str(int err);

/* BACnet text <-> enum helpers (accept the BACnet stack's text names, e.g.
 * "analog-input", "present-value", "degrees-celsius", or a decimal
 * number). Return 0 or -EINVAL. */
int uc_obj_type_from_str(const char *s, uint16_t *type);
const char *uc_obj_type_to_str(uint16_t type);
int uc_prop_from_str(const char *s, uint32_t *prop);
int uc_units_from_str(const char *s, uint16_t *units);

/** True for the object types the firmware can create and bind:
 *  AI, AO, AV, BI, BO, BV, MSI, MSO, MSV. */
bool uc_obj_type_supported(uint16_t type);
bool uc_obj_type_is_binary(uint16_t type);
bool uc_obj_type_is_analog(uint16_t type);
bool uc_obj_type_is_multistate(uint16_t type);

/** Convert a decoded application value to double (REAL, DOUBLE, UNSIGNED,
 *  SIGNED, ENUMERATED, BOOLEAN). -EBADMSG for other tags. */
int uc_value_to_double(const BACNET_APPLICATION_DATA_VALUE *v, double *out);

/** Build the application value a property of an object type expects from a
 *  double (uses bacapp_known_property_tag(); binary present values become
 *  ENUMERATED 0/1, multi-state present values UNSIGNED). -EBADMSG if the
 *  property is not numeric; -EINVAL for NaN/Inf, for a binary present value
 *  (present-value, relinquish-default, priority-array) other than 0 or 1,
 *  for a non-integral value of an UNSIGNED/SIGNED/ENUMERATED property, for a
 *  negative UNSIGNED/ENUMERATED value and for values out of the datatype's
 *  range (REAL: |in| > FLT_MAX). */
int uc_value_from_double(uint16_t object_type, uint32_t property, double in,
			 BACNET_APPLICATION_DATA_VALUE *out);

/** Safe string copy, always NUL-terminates, returns -ENOSPC on truncation. */
int uc_strlcpy(char *dst, const char *src, size_t dst_size);

/** Validate an application name: 1..23 chars of [a-z0-9_-]. */
bool uc_app_name_valid(const char *name);
/** Validate a kv key / param key: 1..31 (kv) / 1..23 (param) chars of
 *  [A-Za-z0-9_.-], not "." or "..". */
bool uc_key_valid(const char *key, size_t max_len);

#ifdef __cplusplus
}
#endif

#endif /* UC_COMMON_H_ */
