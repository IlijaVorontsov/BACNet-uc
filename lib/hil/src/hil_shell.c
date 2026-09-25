/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Shell group "hil" of instrumented DUT images (docs/HIL.md 8.3). Like the stimulus protocol,
 * every command prints exactly one line, "OK k=v ..." or "ERR -<errno> <text>", so the host
 * can drive it over the DUT console:
 *
 *   hil info                 OK board= zephyr= reset=0x<boot cause> uid= markers= ip= uptime_ms=
 *   hil mark <n> <0|1|t>     OK m=<n> v=<new level>          (t = toggle)
 *   hil panic                OK panic, then k_panic(): tests fatal-error recovery (RST-04, FW-05)
 */
#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/util.h>
#include <zephyr/version.h>

#include <hil/hil.h>

/*
 * Reply codes are the stimulus protocol's fixed numbers (Zephyr/newlib numbering, protocol
 * section 3), not the libc's errno values: native_sim builds against another libc, where
 * ENOTSUP is 95, and the host must read the same number from every image.
 */
enum { HIL_E_NOENT = 2, HIL_E_IO = 5, HIL_E_INVAL = 22, HIL_E_NOTSUP = 134 };

static int hil_err(const struct shell *sh, int code, const char *text)
{
	shell_print(sh, "ERR -%d %s", code, text);
	return -code;
}

static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	char ip[sizeof("xxx.xxx.xxx.xxx")];

	ARG_UNUSED(argv);
	if (argc != 1) {
		return hil_err(sh, HIL_E_INVAL, "usage: hil info");
	}
	shell_print(sh, "OK board=%s zephyr=%s reset=0x%08x uid=%s markers=%u ip=%s uptime_ms=%lld",
		    CONFIG_BOARD_TARGET, KERNEL_VERSION_STRING, hil_boot_reset_cause(), hil_uid_hex(),
		    (unsigned int)hil_marker_count(), hil_ipv4_str(ip, sizeof(ip)),
		    (long long)k_uptime_get());
	return 0;
}

static int cmd_mark(const struct shell *sh, size_t argc, char **argv)
{
	char *end;
	unsigned long n;
	int level;
	int rc;

	if (argc != 3) {
		return hil_err(sh, HIL_E_INVAL, "usage: hil mark <n> <0|1|t>");
	}
	n = strtoul(argv[1], &end, 10);
	if (argv[1][0] < '0' || argv[1][0] > '9' || *end != '\0') {
		return hil_err(sh, HIL_E_INVAL, "marker number");
	}
	if (strcmp(argv[2], "0") == 0) {
		level = 0;
	} else if (strcmp(argv[2], "1") == 0) {
		level = 1;
	} else if (strcmp(argv[2], "t") == 0) {
		level = HIL_MARK_TOGGLE;
	} else {
		return hil_err(sh, HIL_E_INVAL, "level must be 0, 1 or t");
	}
	rc = hil_mark(n, level);
	if (rc == -ENOENT) {
		return hil_err(sh, HIL_E_NOENT, "no such marker");
	} else if (rc < 0) {
		return hil_err(sh, HIL_E_IO, "gpio");
	}
	shell_print(sh, "OK m=%lu v=%d", n, rc);
	return 0;
}

static int cmd_panic(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argv);
	if (argc != 1) {
		return hil_err(sh, HIL_E_INVAL, "usage: hil panic");
	}
	shell_print(sh, "OK panic");
	k_msleep(50); /* let the shell UART drain the reply before the fatal error locks IRQs */
	k_panic();
	return 0;
}

/* "hil" alone or with an unknown subcommand: still exactly one ERR line */
static int cmd_hil_root(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argv);
	if (argc < 2) {
		return hil_err(sh, HIL_E_INVAL, "usage: hil <info|mark|panic> ...");
	}
	return hil_err(sh, HIL_E_NOTSUP, "unknown command");
}

SHELL_STATIC_SUBCMD_SET_CREATE(sub_hil,
	SHELL_CMD_ARG(info, NULL, "board, reset cause, UID, markers, IPv4", cmd_info, 1,
		      SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(mark, NULL, "<n> <0|1|t>: set or toggle marker m<n>", cmd_mark, 1,
		      SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(panic, NULL, "kernel panic (fatal-error recovery test)", cmd_panic, 1,
		      SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_ARG_REGISTER(hil, &sub_hil, "HIL rig instrumentation", cmd_hil_root, 1,
		       SHELL_OPT_ARG_CHECK_SKIP);
