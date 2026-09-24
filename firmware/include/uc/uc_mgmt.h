/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Custom MCUmgr (SMP) groups. Numbers and field names are normative, see
 * docs/management-protocol.md. The Python harness mirrors them in
 * harness/src/bacnet_uc_harness/smp/groups.py.
 */
#ifndef UC_MGMT_H_
#define UC_MGMT_H_

#include <zephyr/mgmt/mcumgr/mgmt/mgmt_defines.h>

#define UC_MGMT_GROUP_APP  (MGMT_GROUP_ID_PERUSER + 0) /* 64 */
#define UC_MGMT_GROUP_IO   (MGMT_GROUP_ID_PERUSER + 1) /* 65 */
#define UC_MGMT_GROUP_NODE (MGMT_GROUP_ID_PERUSER + 2) /* 66 */

/* uc_app */
#define UC_MGMT_APP_LIST    0
#define UC_MGMT_APP_INSTALL 1
#define UC_MGMT_APP_START   2
#define UC_MGMT_APP_STOP    3
#define UC_MGMT_APP_REMOVE  4
#define UC_MGMT_APP_STATUS  5

/* uc_io */
#define UC_MGMT_IO_CATALOG 0
#define UC_MGMT_IO_READ    1
#define UC_MGMT_IO_WRITE   2
#define UC_MGMT_IO_FORCE   3

/* uc_node */
#define UC_MGMT_NODE_INFO       0
#define UC_MGMT_NODE_RELOAD     1
#define UC_MGMT_NODE_OBJECTS    2
#define UC_MGMT_NODE_PROP_READ  3
#define UC_MGMT_NODE_PROP_WRITE 4

/* Group rc values, shared by the three groups. */
enum uc_mgmt_rc {
	UC_MGMT_RC_OK = 0,
	UC_MGMT_RC_UNKNOWN = 1,
	UC_MGMT_RC_INVALID = 2,
	UC_MGMT_RC_NOT_FOUND = 3,
	UC_MGMT_RC_EXISTS = 4,
	UC_MGMT_RC_BUSY = 5,
	UC_MGMT_RC_NO_MEM = 6,
	UC_MGMT_RC_IO = 7,
	UC_MGMT_RC_STATE = 8,
	UC_MGMT_RC_VERIFY = 9,
	UC_MGMT_RC_PERM = 10,
	UC_MGMT_RC_UNSUPPORTED = 11,
	UC_MGMT_RC_LIMIT = 12,
};

/** Register the custom groups (called from main after the other modules
 *  are initialised). */
int uc_mgmt_init(void);

#endif /* UC_MGMT_H_ */
