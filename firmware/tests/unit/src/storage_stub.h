/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * In-memory replacement of uc_storage for the unit tests.
 */
#ifndef STORAGE_STUB_H_
#define STORAGE_STUB_H_

#include <stddef.h>

/** Forget every file. */
void stub_fs_reset(void);
/** Create or replace a file with a NUL-terminated string. */
void stub_fs_put(const char *path, const char *text);
/** Content of a file (NUL-terminated) or NULL if absent. */
const char *stub_fs_get(const char *path);
/** Number of uc_storage_write_file() calls since the last reset. */
int stub_fs_writes(void);
/** Make the next uc_storage_write_file() fail with err. */
void stub_fs_fail_next_write(int err);

#endif /* STORAGE_STUB_H_ */
