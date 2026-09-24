/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * File system: LittleFS mounted at /lfs (devicetree fstab, automount) and
 * the directory layout used by the other modules.
 */
#ifndef UC_STORAGE_H_
#define UC_STORAGE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define UC_FS_ROOT   "/lfs"
#define UC_DIR_CFG   UC_FS_ROOT "/cfg"
#define UC_DIR_APPS  UC_FS_ROOT "/apps"
#define UC_DIR_DATA  UC_FS_ROOT "/data"
#define UC_DIR_LOG   UC_FS_ROOT "/log"

#define UC_FILE_DEVICE_CFG UC_DIR_CFG "/device.json"
#define UC_FILE_IO_CFG     UC_DIR_CFG "/io.json"
#define UC_FILE_APPS_CFG   UC_DIR_CFG "/apps.json"

/** Mount (if the fstab entry is not automounted) and create the directory
 *  layout. Formats the partition if mounting fails and
 *  CONFIG_UC_STORAGE_FORMAT_ON_FAIL=y. Safe to call more than once. */
int uc_storage_init(void);

/** True once /lfs is mounted and the layout exists. */
bool uc_storage_ready(void);

/** Read a whole file into a new k_malloc() buffer (NUL-terminated, the
 *  terminator is not counted in *len). Fails with -EFBIG if the file is
 *  larger than max_len. The caller k_free()s *buf. */
int uc_storage_read_file(const char *path, char **buf, size_t *len,
			 size_t max_len);

/** Write a file atomically: write <path>.tmp, then rename over <path>. */
int uc_storage_write_file(const char *path, const void *data, size_t len);

/** Size of a file, -ENOENT if missing. */
int uc_storage_file_size(const char *path, size_t *size);

/** Delete a file or an empty directory; missing is not an error. */
int uc_storage_remove(const char *path);

/** Create a directory (and missing parents under /lfs). */
int uc_storage_mkdir(const char *path);

/** Total and free bytes of the /lfs volume. */
int uc_storage_stats(uint64_t *total, uint64_t *free_bytes);

/** SHA-256 of a file (mbedTLS/PSA). */
int uc_storage_sha256(const char *path, uint8_t digest[32]);

#endif /* UC_STORAGE_H_ */
