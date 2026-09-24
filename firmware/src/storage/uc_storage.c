/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * File system: LittleFS at /lfs from a devicetree fstab entry.
 *
 * The fstab entry is the node labelled "uc_lfs_ram" (snippet uc-ramfs), else
 * "uc_lfs" (board overlays), else the first enabled "zephyr,fstab,littlefs"
 * node. The overlays set "automount" and "no-format": the partition is mounted
 * during boot (POST_KERNEL) when it holds a valid file system, and this
 * module decides about formatting (CONFIG_UC_STORAGE_FORMAT_ON_FAIL).
 *
 * Atomic replace: data is written to <path>.tmp, synced, then renamed over
 * <path>. fs_rename() clobbers an existing destination; with LittleFS the
 * rename is a single metadata commit, so after a power loss either the old
 * or the new file exists. A stale <path>.tmp is removed on the next write.
 */

#include <errno.h>
#include <string.h>

#include <zephyr/devicetree.h>
#include <zephyr/fs/fs.h>
#include <zephyr/fs/littlefs.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/printk.h>

#if defined(CONFIG_PSA_CRYPTO)
#include <psa/crypto.h>
#endif

#include "uc/uc_common.h"
#include "uc/uc_storage.h"

LOG_MODULE_REGISTER(uc_storage, CONFIG_UC_LOG_LEVEL);

#if DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(uc_lfs_ram))
#define UC_LFS_NODE DT_NODELABEL(uc_lfs_ram)
#elif DT_NODE_HAS_STATUS_OKAY(DT_NODELABEL(uc_lfs))
#define UC_LFS_NODE DT_NODELABEL(uc_lfs)
#elif DT_HAS_COMPAT_STATUS_OKAY(zephyr_fstab_littlefs)
#define UC_LFS_NODE DT_COMPAT_GET_ANY_STATUS_OKAY(zephyr_fstab_littlefs)
#endif

#ifdef UC_LFS_NODE
FS_FSTAB_DECLARE_ENTRY(UC_LFS_NODE);
#define UC_LFS_MP (&FS_FSTAB_ENTRY(UC_LFS_NODE))
#endif

#define TMP_SUFFIX ".tmp"
#define IO_CHUNK   256

static K_MUTEX_DEFINE(storage_lock); /* init and atomic writes */
static bool storage_ready;

static const char *const layout_dirs[] = {
	UC_DIR_CFG,
	UC_DIR_APPS,
	UC_DIR_DATA,
	UC_DIR_LOG,
};

static bool is_mounted(void)
{
	struct fs_statvfs st;

	return fs_statvfs(UC_FS_ROOT, &st) == 0;
}

#ifdef UC_LFS_NODE
static int mount_lfs(void)
{
	struct fs_mount_t *mp = UC_LFS_MP;
	int rc;

	if (strcmp(mp->mnt_point, UC_FS_ROOT) != 0) {
		LOG_ERR("fstab mount point is %s, expected %s", mp->mnt_point, UC_FS_ROOT);
		return -EINVAL;
	}

	/* Mount without the driver's implicit format so that the format
	 * decision stays here.
	 */
	mp->flags |= FS_MOUNT_FLAG_NO_FORMAT;

	rc = fs_mount(mp);
	if (rc == 0 || rc == -EBUSY) {
		return 0;
	}

	LOG_WRN("mounting %s failed (%d)", UC_FS_ROOT, rc);

	if (!IS_ENABLED(CONFIG_UC_STORAGE_FORMAT_ON_FAIL) ||
	    !IS_ENABLED(CONFIG_FILE_SYSTEM_MKFS)) {
		return rc;
	}

#if defined(CONFIG_FILE_SYSTEM_MKFS)
	LOG_WRN("formatting %s", UC_FS_ROOT);
	/* Same littlefs configuration (buffers, sizes) as the fstab entry. */
	rc = fs_mkfs(FS_LITTLEFS, (uintptr_t)mp->storage_dev, mp->fs_data, 0);
	if (rc < 0) {
		LOG_ERR("format failed (%d)", rc);
		return rc;
	}

	rc = fs_mount(mp);
	if (rc < 0) {
		LOG_ERR("mount after format failed (%d)", rc);
	}
#endif
	return rc;
}
#endif /* UC_LFS_NODE */

int uc_storage_init(void)
{
	int rc = 0;

	k_mutex_lock(&storage_lock, K_FOREVER);

	if (storage_ready) {
		goto out;
	}

	if (!is_mounted()) {
#ifdef UC_LFS_NODE
		rc = mount_lfs();
		if (rc < 0) {
			goto out;
		}
#else
		LOG_ERR("no zephyr,fstab,littlefs node in the devicetree");
		rc = -ENODEV;
		goto out;
#endif
	}

	for (size_t i = 0; i < ARRAY_SIZE(layout_dirs); i++) {
		rc = fs_mkdir(layout_dirs[i]);
		if (rc < 0 && rc != -EEXIST) {
			LOG_ERR("mkdir %s failed (%d)", layout_dirs[i], rc);
			goto out;
		}
		rc = 0;
	}

	storage_ready = true;

	{
		uint64_t total = 0;
		uint64_t free_bytes = 0;

		(void)uc_storage_stats(&total, &free_bytes);
		LOG_INF("%s ready: %u KiB total, %u KiB free", UC_FS_ROOT,
			(unsigned int)(total / 1024U), (unsigned int)(free_bytes / 1024U));
	}

out:
	k_mutex_unlock(&storage_lock);
	return rc;
}

bool uc_storage_ready(void)
{
	return storage_ready;
}

int uc_storage_file_size(const char *path, size_t *size)
{
	struct fs_dirent ent;
	int rc;

	if (path == NULL || size == NULL) {
		return -EINVAL;
	}

	rc = fs_stat(path, &ent);
	if (rc < 0) {
		return rc;
	}
	if (ent.type != FS_DIR_ENTRY_FILE) {
		return -EISDIR;
	}

	*size = ent.size;
	return 0;
}

int uc_storage_read_file(const char *path, char **buf, size_t *len, size_t max_len)
{
	struct fs_file_t file;
	size_t size;
	size_t done = 0;
	char *data;
	int rc;

	if (path == NULL || buf == NULL || len == NULL) {
		return -EINVAL;
	}

	*buf = NULL;
	*len = 0;

	rc = uc_storage_file_size(path, &size);
	if (rc < 0) {
		return rc;
	}
	if (size > max_len) {
		return -EFBIG;
	}

	data = k_malloc(size + 1);
	if (data == NULL) {
		return -ENOMEM;
	}

	fs_file_t_init(&file);
	rc = fs_open(&file, path, FS_O_READ);
	if (rc < 0) {
		k_free(data);
		return rc;
	}

	while (done < size) {
		ssize_t n = fs_read(&file, data + done, size - done);

		if (n < 0) {
			rc = (int)n;
			break;
		}
		if (n == 0) {
			break;
		}
		done += (size_t)n;
	}

	(void)fs_close(&file);

	if (rc < 0) {
		k_free(data);
		return rc;
	}

	data[done] = '\0';
	*buf = data;
	*len = done;
	return 0;
}

static int write_all(const char *path, const void *data, size_t len)
{
	struct fs_file_t file;
	const uint8_t *p = data;
	size_t done = 0;
	int rc;

	fs_file_t_init(&file);
	rc = fs_open(&file, path, FS_O_CREATE | FS_O_WRITE | FS_O_TRUNC);
	if (rc < 0) {
		return rc;
	}

	while (done < len) {
		ssize_t n = fs_write(&file, p + done, len - done);

		if (n < 0) {
			rc = (int)n;
			break;
		}
		if (n == 0) {
			rc = -ENOSPC;
			break;
		}
		done += (size_t)n;
	}

	if (rc == 0) {
		rc = fs_sync(&file);
	}

	{
		int rc2 = fs_close(&file);

		if (rc == 0) {
			rc = rc2;
		}
	}

	return rc;
}

int uc_storage_write_file(const char *path, const void *data, size_t len)
{
	char tmp[UC_PATH_MAX + sizeof(TMP_SUFFIX)];
	int rc;

	if (path == NULL || (data == NULL && len > 0)) {
		return -EINVAL;
	}
	if (strlen(path) >= UC_PATH_MAX) {
		return -ENAMETOOLONG;
	}
	if (!storage_ready) {
		return -ENODEV;
	}

	snprintk(tmp, sizeof(tmp), "%s" TMP_SUFFIX, path);

	k_mutex_lock(&storage_lock, K_FOREVER);

	rc = fs_unlink(tmp);
	if (rc < 0 && rc != -ENOENT) {
		LOG_WRN("cannot remove stale %s (%d)", tmp, rc);
	}

	rc = write_all(tmp, data, len);
	if (rc < 0) {
		LOG_ERR("write %s failed (%d)", tmp, rc);
		goto fail;
	}

	rc = fs_rename(tmp, path);
	if (rc == -EEXIST) {
		/* file systems that do not clobber on rename */
		(void)fs_unlink(path);
		rc = fs_rename(tmp, path);
	}
	if (rc < 0) {
		LOG_ERR("rename %s -> %s failed (%d)", tmp, path, rc);
		goto fail;
	}

	k_mutex_unlock(&storage_lock);
	return 0;

fail:
	(void)fs_unlink(tmp);
	k_mutex_unlock(&storage_lock);
	return rc;
}

int uc_storage_remove(const char *path)
{
	int rc;

	if (path == NULL) {
		return -EINVAL;
	}

	rc = fs_unlink(path);
	if (rc == -ENOENT) {
		return 0;
	}
	return rc;
}

int uc_storage_mkdir(const char *path)
{
	char buf[UC_PATH_MAX];
	size_t root_len = strlen(UC_FS_ROOT);
	size_t len;
	int rc;

	if (path == NULL) {
		return -EINVAL;
	}

	len = strlen(path);
	if (len >= sizeof(buf)) {
		return -ENAMETOOLONG;
	}
	if (len <= root_len + 1 || strncmp(path, UC_FS_ROOT "/", root_len + 1) != 0) {
		return -EINVAL;
	}

	memcpy(buf, path, len + 1);
	if (buf[len - 1] == '/') {
		buf[--len] = '\0';
	}

	/* create every component after the mount point */
	for (size_t i = root_len + 1; i <= len; i++) {
		if (buf[i] != '/' && buf[i] != '\0') {
			continue;
		}
		if (buf[i - 1] == '/') {
			return -EINVAL; /* empty component */
		}

		char saved = buf[i];

		buf[i] = '\0';
		rc = fs_mkdir(buf);
		buf[i] = saved;
		if (rc < 0 && rc != -EEXIST) {
			return rc;
		}
	}

	return 0;
}

int uc_storage_stats(uint64_t *total, uint64_t *free_bytes)
{
	struct fs_statvfs st;
	int rc;

	rc = fs_statvfs(UC_FS_ROOT, &st);
	if (rc < 0) {
		return rc;
	}

	if (total != NULL) {
		*total = (uint64_t)st.f_frsize * st.f_blocks;
	}
	if (free_bytes != NULL) {
		*free_bytes = (uint64_t)st.f_frsize * st.f_bfree;
	}
	return 0;
}

int uc_storage_sha256(const char *path, uint8_t digest[32])
{
#if defined(CONFIG_PSA_CRYPTO) && defined(CONFIG_PSA_WANT_ALG_SHA_256)
	psa_hash_operation_t op = PSA_HASH_OPERATION_INIT;
	struct fs_file_t file;
	uint8_t chunk[IO_CHUNK];
	size_t out_len = 0;
	psa_status_t st;
	int rc;

	if (path == NULL || digest == NULL) {
		return -EINVAL;
	}

	st = psa_crypto_init();
	if (st != PSA_SUCCESS) {
		LOG_ERR("psa_crypto_init failed (%d)", (int)st);
		return -EIO;
	}

	fs_file_t_init(&file);
	rc = fs_open(&file, path, FS_O_READ);
	if (rc < 0) {
		return rc;
	}

	st = psa_hash_setup(&op, PSA_ALG_SHA_256);
	if (st != PSA_SUCCESS) {
		rc = -EIO;
		goto out;
	}

	while (true) {
		ssize_t n = fs_read(&file, chunk, sizeof(chunk));

		if (n < 0) {
			rc = (int)n;
			goto out;
		}
		if (n == 0) {
			break;
		}
		st = psa_hash_update(&op, chunk, (size_t)n);
		if (st != PSA_SUCCESS) {
			rc = -EIO;
			goto out;
		}
	}

	st = psa_hash_finish(&op, digest, 32, &out_len);
	if (st != PSA_SUCCESS || out_len != 32) {
		rc = -EIO;
	}

out:
	if (rc < 0) {
		(void)psa_hash_abort(&op);
	}
	(void)fs_close(&file);
	return rc;
#else
	ARG_UNUSED(path);
	ARG_UNUSED(digest);
	return -ENOTSUP;
#endif
}
