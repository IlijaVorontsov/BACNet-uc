# SPDX-License-Identifier: Apache-2.0
"""SMP operation codes, group ids, command ids and rc names.

Mirrors ``firmware/include/uc/uc_mgmt.h`` (custom groups, normative in
``docs/management-protocol.md``) and the Zephyr MCUmgr headers for the
standard groups.
"""

from __future__ import annotations

# --- operations (header byte 0, bits 2..0) --------------------------------
OP_READ = 0
OP_READ_RSP = 1
OP_WRITE = 2
OP_WRITE_RSP = 3

OP_NAMES: dict[int, str] = {
    OP_READ: "read",
    OP_READ_RSP: "read-rsp",
    OP_WRITE: "write",
    OP_WRITE_RSP: "write-rsp",
}

# SMP protocol version in header byte 0 bits 4..3: 0 = SMP v1 (legacy), 1 = SMP v2
SMP_VERSION_1 = 0
SMP_VERSION_2 = 1

# --- groups ----------------------------------------------------------------
GROUP_OS = 0
GROUP_IMG = 1
GROUP_STAT = 2
GROUP_SETTINGS = 3
GROUP_LOG = 4
GROUP_CRASH = 5
GROUP_SPLIT = 6
GROUP_RUN = 7
GROUP_FS = 8
GROUP_SHELL = 9
GROUP_ENUM = 10
GROUP_ZBASIC = 63
GROUP_PERUSER = 64
GROUP_UC_APP = 64
GROUP_UC_IO = 65
GROUP_UC_NODE = 66

GROUP_NAMES: dict[int, str] = {
    GROUP_OS: "os",
    GROUP_IMG: "img",
    GROUP_STAT: "stat",
    GROUP_SETTINGS: "settings",
    GROUP_LOG: "log",
    GROUP_CRASH: "crash",
    GROUP_SPLIT: "split",
    GROUP_RUN: "run",
    GROUP_FS: "fs",
    GROUP_SHELL: "shell",
    GROUP_ENUM: "enum",
    GROUP_ZBASIC: "zephyr_basic",
    GROUP_UC_APP: "uc_app",
    GROUP_UC_IO: "uc_io",
    GROUP_UC_NODE: "uc_node",
}

# --- OS group (0) -----------------------------------------------------------
OS_ECHO = 0
OS_CONS_ECHO_CTRL = 1
OS_TASKSTAT = 2
OS_MPSTAT = 3
OS_DATETIME_STR = 4
OS_RESET = 5
OS_MCUMGR_PARAMS = 6
OS_INFO = 7
OS_BOOTLOADER_INFO = 8

# --- image group (1) --------------------------------------------------------
IMG_STATE = 0
IMG_UPLOAD = 1
IMG_FILE = 2
IMG_CORELIST = 3
IMG_CORELOAD = 4
IMG_ERASE = 5
IMG_SLOT_INFO = 6

# --- statistics group (2) ---------------------------------------------------
STAT_SHOW = 0
STAT_LIST = 1

# --- settings group (3) -----------------------------------------------------
SETTINGS_READ_WRITE = 0
SETTINGS_DELETE = 1
SETTINGS_COMMIT = 2
SETTINGS_LOAD_SAVE = 3

# --- file system group (8) --------------------------------------------------
FS_FILE = 0
FS_STATUS = 1
FS_HASH_CHECKSUM = 2
FS_SUPPORTED_HASH_CHECKSUM = 3
FS_OPENED_FILE = 4  # write: close the file opened by upload/download

# --- shell group (9) --------------------------------------------------------
SHELL_EXEC = 0

# --- uc_app (64) --------------------------------------------------------------
UC_APP_LIST = 0
UC_APP_INSTALL = 1
UC_APP_START = 2
UC_APP_STOP = 3
UC_APP_REMOVE = 4
UC_APP_STATUS = 5

# --- uc_io (65) ---------------------------------------------------------------
UC_IO_CATALOG = 0
UC_IO_READ = 1
UC_IO_WRITE = 2
UC_IO_FORCE = 3

# --- uc_node (66) -------------------------------------------------------------
UC_NODE_INFO = 0
UC_NODE_RELOAD = 1
UC_NODE_OBJECTS = 2
UC_NODE_PROP_READ = 3
UC_NODE_PROP_WRITE = 4

# --- rc values of the BACnet-uc custom groups (64, 65, 66) ----------------------
UC_RC_OK = 0
UC_RC_UNKNOWN = 1
UC_RC_INVALID = 2
UC_RC_NOT_FOUND = 3
UC_RC_EXISTS = 4
UC_RC_BUSY = 5
UC_RC_NO_MEM = 6
UC_RC_IO = 7
UC_RC_STATE = 8
UC_RC_VERIFY = 9
UC_RC_PERM = 10
UC_RC_UNSUPPORTED = 11
UC_RC_LIMIT = 12

UC_RC_NAMES: dict[int, str] = {
    UC_RC_OK: "OK",
    UC_RC_UNKNOWN: "UNKNOWN",
    UC_RC_INVALID: "INVALID",
    UC_RC_NOT_FOUND: "NOT_FOUND",
    UC_RC_EXISTS: "EXISTS",
    UC_RC_BUSY: "BUSY",
    UC_RC_NO_MEM: "NO_MEM",
    UC_RC_IO: "IO",
    UC_RC_STATE: "STATE",
    UC_RC_VERIFY: "VERIFY",
    UC_RC_PERM: "PERM",
    UC_RC_UNSUPPORTED: "UNSUPPORTED",
    UC_RC_LIMIT: "LIMIT",
}

# --- legacy rc (enum mcumgr_err_t), used in {"rc": n} responses --------------------
MGMT_ERR_EOK = 0
MGMT_ERR_EUNKNOWN = 1
MGMT_ERR_ENOMEM = 2
MGMT_ERR_EINVAL = 3
MGMT_ERR_ETIMEOUT = 4
MGMT_ERR_ENOENT = 5
MGMT_ERR_EBADSTATE = 6
MGMT_ERR_EMSGSIZE = 7
MGMT_ERR_ENOTSUP = 8
MGMT_ERR_ECORRUPT = 9
MGMT_ERR_EBUSY = 10
MGMT_ERR_EACCESSDENIED = 11
MGMT_ERR_UNSUPPORTED_TOO_OLD = 12
MGMT_ERR_UNSUPPORTED_TOO_NEW = 13
MGMT_ERR_EPERUSER = 256

MCUMGR_ERR_NAMES: dict[int, str] = {
    MGMT_ERR_EOK: "EOK",
    MGMT_ERR_EUNKNOWN: "EUNKNOWN",
    MGMT_ERR_ENOMEM: "ENOMEM",
    MGMT_ERR_EINVAL: "EINVAL",
    MGMT_ERR_ETIMEOUT: "ETIMEOUT",
    MGMT_ERR_ENOENT: "ENOENT",
    MGMT_ERR_EBADSTATE: "EBADSTATE",
    MGMT_ERR_EMSGSIZE: "EMSGSIZE",
    MGMT_ERR_ENOTSUP: "ENOTSUP",
    MGMT_ERR_ECORRUPT: "ECORRUPT",
    MGMT_ERR_EBUSY: "EBUSY",
    MGMT_ERR_EACCESSDENIED: "EACCESSDENIED",
    MGMT_ERR_UNSUPPORTED_TOO_OLD: "UNSUPPORTED_TOO_OLD",
    MGMT_ERR_UNSUPPORTED_TOO_NEW: "UNSUPPORTED_TOO_NEW",
    MGMT_ERR_EPERUSER: "EPERUSER",
}

# --- group rc names of the standard groups (SMP v2 "err") -------------------------
OS_RC_NAMES: dict[int, str] = {
    0: "OK",
    1: "UNKNOWN",
    2: "INVALID_FORMAT",
    3: "QUERY_YIELDS_NO_ANSWER",
    4: "RTC_NOT_SET",
    5: "RTC_COMMAND_FAILED",
    6: "QUERY_RESPONSE_VALUE_NOT_VALID",
    7: "HEAP_STATS_FETCH_FAILED",
}

IMG_RC_NAMES: dict[int, str] = {
    0: "OK",
    1: "UNKNOWN",
    2: "FLASH_CONFIG_QUERY_FAIL",
    3: "NO_IMAGE",
    4: "NO_TLVS",
    5: "INVALID_TLV",
    6: "TLV_MULTIPLE_HASHES_FOUND",
    7: "TLV_INVALID_SIZE",
    8: "HASH_NOT_FOUND",
    9: "NO_FREE_SLOT",
    10: "FLASH_OPEN_FAILED",
    11: "FLASH_READ_FAILED",
    12: "FLASH_WRITE_FAILED",
    13: "FLASH_ERASE_FAILED",
    14: "INVALID_SLOT",
    15: "NO_FREE_MEMORY",
    16: "FLASH_CONTEXT_ALREADY_SET",
    17: "FLASH_CONTEXT_NOT_SET",
    18: "FLASH_AREA_DEVICE_NULL",
    19: "INVALID_PAGE_OFFSET",
    20: "INVALID_OFFSET",
    21: "INVALID_LENGTH",
    22: "INVALID_IMAGE_HEADER",
    23: "INVALID_IMAGE_HEADER_MAGIC",
    24: "INVALID_HASH",
    25: "INVALID_FLASH_ADDRESS",
    26: "VERSION_GET_FAILED",
    27: "CURRENT_VERSION_IS_NEWER",
    28: "IMAGE_ALREADY_PENDING",
    29: "INVALID_IMAGE_VECTOR_TABLE",
    30: "INVALID_IMAGE_TOO_LARGE",
    31: "INVALID_IMAGE_DATA_OVERRUN",
    32: "IMAGE_CONFIRMATION_DENIED",
    33: "IMAGE_SETTING_TEST_TO_ACTIVE_DENIED",
    34: "ACTIVE_SLOT_NOT_KNOWN",
}

FS_RC_OK = 0
FS_RC_UNKNOWN = 1
FS_RC_FILE_INVALID_NAME = 2
FS_RC_FILE_NOT_FOUND = 3
FS_RC_FILE_IS_DIRECTORY = 4
FS_RC_FILE_OPEN_FAILED = 5
FS_RC_FILE_SEEK_FAILED = 6
FS_RC_FILE_READ_FAILED = 7
FS_RC_FILE_TRUNCATE_FAILED = 8
FS_RC_FILE_DELETE_FAILED = 9
FS_RC_FILE_WRITE_FAILED = 10
FS_RC_FILE_OFFSET_NOT_VALID = 11
FS_RC_FILE_OFFSET_LARGER_THAN_FILE = 12
FS_RC_CHECKSUM_HASH_NOT_FOUND = 13
FS_RC_MOUNT_POINT_NOT_FOUND = 14
FS_RC_READ_ONLY_FILESYSTEM = 15
FS_RC_FILE_EMPTY = 16
FS_RC_FILE_CLOSE_FAILED = 17

FS_RC_NAMES: dict[int, str] = {
    FS_RC_OK: "OK",
    FS_RC_UNKNOWN: "UNKNOWN",
    FS_RC_FILE_INVALID_NAME: "FILE_INVALID_NAME",
    FS_RC_FILE_NOT_FOUND: "FILE_NOT_FOUND",
    FS_RC_FILE_IS_DIRECTORY: "FILE_IS_DIRECTORY",
    FS_RC_FILE_OPEN_FAILED: "FILE_OPEN_FAILED",
    FS_RC_FILE_SEEK_FAILED: "FILE_SEEK_FAILED",
    FS_RC_FILE_READ_FAILED: "FILE_READ_FAILED",
    FS_RC_FILE_TRUNCATE_FAILED: "FILE_TRUNCATE_FAILED",
    FS_RC_FILE_DELETE_FAILED: "FILE_DELETE_FAILED",
    FS_RC_FILE_WRITE_FAILED: "FILE_WRITE_FAILED",
    FS_RC_FILE_OFFSET_NOT_VALID: "FILE_OFFSET_NOT_VALID",
    FS_RC_FILE_OFFSET_LARGER_THAN_FILE: "FILE_OFFSET_LARGER_THAN_FILE",
    FS_RC_CHECKSUM_HASH_NOT_FOUND: "CHECKSUM_HASH_NOT_FOUND",
    FS_RC_MOUNT_POINT_NOT_FOUND: "MOUNT_POINT_NOT_FOUND",
    FS_RC_READ_ONLY_FILESYSTEM: "READ_ONLY_FILESYSTEM",
    FS_RC_FILE_EMPTY: "FILE_EMPTY",
    FS_RC_FILE_CLOSE_FAILED: "FILE_CLOSE_FAILED",
}

SHELL_RC_NAMES: dict[int, str] = {
    0: "OK",
    1: "UNKNOWN",
    2: "COMMAND_TOO_LONG",
    3: "EMPTY_COMMAND",
}

GROUP_RC_NAMES: dict[int, dict[int, str]] = {
    GROUP_OS: OS_RC_NAMES,
    GROUP_IMG: IMG_RC_NAMES,
    GROUP_FS: FS_RC_NAMES,
    GROUP_SHELL: SHELL_RC_NAMES,
    GROUP_UC_APP: UC_RC_NAMES,
    GROUP_UC_IO: UC_RC_NAMES,
    GROUP_UC_NODE: UC_RC_NAMES,
}


def group_name(group: int) -> str:
    """Name of an SMP group id (``"uc_app"``, ``"fs"``, ...) or ``"group<n>"``."""
    return GROUP_NAMES.get(group, f"group{group}")


def rc_name(group: int | None, rc: int) -> str:
    """Symbolic name of an rc.

    ``group`` is ``None`` for a legacy ``{"rc": n}`` (``mcumgr_err_t``) value.
    """
    table = MCUMGR_ERR_NAMES if group is None else GROUP_RC_NAMES.get(group, {})
    return table.get(rc, f"rc{rc}")
