/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * In-memory replacement of uc_storage for the unit tests.
 */
#ifndef STORAGE_STUB_H_
#define STORAGE_STUB_H_

#include <stdbool.h>
#include <stddef.h>

/** Forget every file. */
void stub_fs_reset(void);
/** Create or replace a file with a NUL-terminated string. */
void stub_fs_put(const char *path, const char *text);
/** Create or replace a file with len bytes of data. */
void stub_fs_put_len(const char *path, const char *data, size_t len);
/** Content of a file (NUL-terminated) or NULL if absent. */
const char *stub_fs_get(const char *path);
/** Number of uc_storage_write_file() calls since the last reset. */
int stub_fs_writes(void);
/** Make the next uc_storage_write_file() fail with err. */
void stub_fs_fail_next_write(int err);
/** Make the next uc_storage_rename() fail with err. */
void stub_fs_fail_next_rename(int err);
/** uc_storage_ready() result (true after stub_fs_reset()). */
void stub_fs_set_ready(bool ready);

#endif /* STORAGE_STUB_H_ */
