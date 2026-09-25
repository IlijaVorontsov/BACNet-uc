/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * In-memory replacement of uc_storage (only the functions uc_config uses
 * have a real implementation).
 */

#include <errno.h>
#include <string.h>

#include <zephyr/kernel.h>

#include "uc/uc_common.h"
#include "uc/uc_storage.h"

#include "storage_stub.h"

#define STUB_FILES    8
#define STUB_FILE_MAX (CONFIG_UC_CONFIG_DOC_MAX + 64)

struct stub_file {
	bool used;
	char path[UC_PATH_MAX];
	char data[STUB_FILE_MAX + 1];
	size_t len;
};

static struct stub_file files[STUB_FILES];
static int writes;
static int fail_next;
static int fail_next_rename;
static bool not_ready;

static struct stub_file *find(const char *path)
{
	for (size_t i = 0; i < STUB_FILES; i++) {
		if (files[i].used && strcmp(files[i].path, path) == 0) {
			return &files[i];
		}
	}
	return NULL;
}

static struct stub_file *find_or_add(const char *path)
{
	struct stub_file *f = find(path);

	if (f != NULL) {
		return f;
	}
	for (size_t i = 0; i < STUB_FILES; i++) {
		if (!files[i].used) {
			files[i].used = true;
			strncpy(files[i].path, path, sizeof(files[i].path) - 1);
			return &files[i];
		}
	}
	return NULL;
}

void stub_fs_reset(void)
{
	memset(files, 0, sizeof(files));
	writes = 0;
	fail_next = 0;
	fail_next_rename = 0;
	not_ready = false;
}

void stub_fs_put(const char *path, const char *text)
{
	stub_fs_put_len(path, text, strlen(text));
}

void stub_fs_put_len(const char *path, const char *data, size_t len)
{
	struct stub_file *f = find_or_add(path);

	__ASSERT_NO_MSG(f != NULL && len <= STUB_FILE_MAX);
	memcpy(f->data, data, len);
	f->data[len] = '\0';
	f->len = len;
}

const char *stub_fs_get(const char *path)
{
	struct stub_file *f = find(path);

	return (f != NULL) ? f->data : NULL;
}

int stub_fs_writes(void)
{
	return writes;
}

void stub_fs_fail_next_write(int err)
{
	fail_next = err;
}

void stub_fs_fail_next_rename(int err)
{
	fail_next_rename = err;
}

void stub_fs_set_ready(bool ready)
{
	not_ready = !ready;
}

int uc_storage_init(void)
{
	return 0;
}

bool uc_storage_ready(void)
{
	return !not_ready;
}

int uc_storage_read_file(const char *path, char **buf, size_t *len, size_t max_len)
{
	struct stub_file *f = find(path);
	char *data;

	if (f == NULL) {
		return -ENOENT;
	}
	if (f->len > max_len) {
		return -EFBIG;
	}
	data = k_malloc(f->len + 1);
	if (data == NULL) {
		return -ENOMEM;
	}
	memcpy(data, f->data, f->len + 1);
	*buf = data;
	*len = f->len;
	return 0;
}

int uc_storage_write_file(const char *path, const void *data, size_t len)
{
	struct stub_file *f;

	if (fail_next != 0) {
		int err = fail_next;

		fail_next = 0;
		return err;
	}
	if (len > STUB_FILE_MAX) {
		return -ENOSPC;
	}
	f = find_or_add(path);
	if (f == NULL) {
		return -ENOSPC;
	}
	memcpy(f->data, data, len);
	f->data[len] = '\0';
	f->len = len;
	writes++;
	return 0;
}

int uc_storage_rename(const char *from, const char *to)
{
	struct stub_file *src = find(from);
	struct stub_file *dst;

	if (fail_next_rename != 0) {
		int err = fail_next_rename;

		fail_next_rename = 0;
		return err;
	}
	if (src == NULL) {
		return -ENOENT;
	}
	if (strcmp(from, to) == 0) {
		return 0;
	}
	/* clobber the destination, like LittleFS */
	dst = find(to);
	if (dst != NULL) {
		dst->used = false;
	}
	strncpy(src->path, to, sizeof(src->path) - 1);
	src->path[sizeof(src->path) - 1] = '\0';
	return 0;
}

int uc_storage_file_size(const char *path, size_t *size)
{
	struct stub_file *f = find(path);

	if (f == NULL) {
		return -ENOENT;
	}
	*size = f->len;
	return 0;
}

int uc_storage_remove(const char *path)
{
	struct stub_file *f = find(path);

	if (f != NULL) {
		f->used = false;
	}
	return 0;
}

int uc_storage_mkdir(const char *path)
{
	ARG_UNUSED(path);
	return 0;
}

int uc_storage_stats(uint64_t *total, uint64_t *free_bytes)
{
	*total = 0;
	*free_bytes = 0;
	return 0;
}

int uc_storage_sha256(const char *path, uint8_t digest[32])
{
	ARG_UNUSED(path);
	ARG_UNUSED(digest);
	return -ENOTSUP;
}
