/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * "stim" shell command group, protocol v0 (+ It2 caps pwmcap, rs485).
 *
 * Reply contract: every stim command prints exactly one line
 *   OK [key=value ...]            or    ERR <negative code> <text>
 * Codes are fixed protocol numbers (newlib / Zephyr minimal-libc numbering).
 * Timestamps: ns since boot on the stimulus 64-bit cycle counter
 * (SysTick: nucleo_f767zi 216 MHz -> 4.63 ns, frdm_mcxn236 150 MHz -> 6.67 ns).
 *
 * Board-specific code is confined to three blocks selected from devicetree:
 * the STM32 on-chip DAC (EN=0 for Hi-Z), the RS-485 "transmission complete"
 * flag (STM32 USART ISR.TC / NXP LPUART STAT.TC) and the NXP FlexPWM capture
 * preparation (free-running 16-bit counter for a capture-only submodule).
 */
#include <errno.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/dac.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/shell/shell.h>
#include <zephyr/sys/crc.h>
#include <zephyr/sys/util.h>

#include <soc.h>

#include "stim.h"

#define STIM_NODE DT_NODELABEL(stim)

/* STM32 on-chip DAC (F7): src_off disables the channel (EN=0, output Hi-Z). */
#if DT_NODE_HAS_STATUS(DT_NODELABEL(dac1), okay) && DT_NODE_HAS_COMPAT(DT_NODELABEL(dac1), st_stm32_dac)
#include <stm32_ll_dac.h>
#define STIM_STM32_DAC 1
#endif

/* MS/TP injector UART: the "transmission complete" flag is read from the register block. */
#if DT_NODE_HAS_PROP(STIM_NODE, rs485_uart)
#define RS485_NODE DT_PHANDLE(STIM_NODE, rs485_uart)
#if DT_NODE_HAS_COMPAT(RS485_NODE, st_stm32_usart) || DT_NODE_HAS_COMPAT(RS485_NODE, st_stm32_uart)
#include <stm32_ll_usart.h>
#define STIM_RS485_STM32 1
#elif DT_NODE_HAS_COMPAT(RS485_NODE, nxp_lpuart)
#include <fsl_lpuart.h>
#define STIM_RS485_LPUART 1
#else
#error "rs485-uart: unsupported UART (need its transmission-complete flag)"
#endif
#endif

/* NXP FlexPWM capture (pwm_mcux): see cap_prepare(). */
#if DT_HAS_COMPAT_STATUS_OKAY(nxp_imx_pwm) && defined(CONFIG_PWM_CAPTURE)
#include <fsl_pwm.h>
#define STIM_NXP_FLEXPWM 1
#endif
/* DUT profile this channel map is wired for (D22); "stim info" reports it. */
#define STIM_PROFILE DT_PROP(STIM_NODE, profile)

/* ---------------------------------------------------------- errors ---- */

enum {
	E_PERM = 1, E_NOENT = 2, E_IO = 5, E_AGAIN = 11, E_BUSY = 16, E_NODEV = 19,
	E_INVAL = 22, E_NOSPC = 28, E_RANGE = 34, E_PROTO = 71, E_TIMEDOUT = 116,
	E_ALREADY = 120, E_MSGSIZE = 122, E_NOTSUP = 134,
};
BUILD_ASSERT(ETIMEDOUT == E_TIMEDOUT && ENOTSUP == E_NOTSUP && EALREADY == E_ALREADY &&
	     EAGAIN == E_AGAIN && ERANGE == E_RANGE, "libc errno numbering differs");

static int err(const struct shell *sh, int code, const char *txt)
{
	shell_print(sh, "ERR -%d %s", code, txt);
	return -code;
}

/* --------------------------------------------------------- channels ---- */

enum kind { K_DI, K_DO, K_AI, K_AO, K_NRST, K_NRST_SENSE, K_PWR, K_SYNC, K_MARKER, K_SENSE };
static const char *const kind_name[] = {"di", "do", "ai", "ao", "nrst", "nrst-sense",
					"pwr", "sync", "marker", "sense"};

struct chan {
	const char *name;
	uint8_t kind;
	struct gpio_dt_spec gpio;
	struct adc_dt_spec sense;
	struct dac_dt_spec src;
	struct pwm_dt_spec cap;
#ifdef STIM_NXP_FLEXPWM
	uintptr_t cap_base; /* FlexPWM register block of the capture submodule (0: none) */
	uint8_t cap_sm;     /* submodule index */
#endif
	int16_t lo_mv, hi_mv;
	uint8_t num, den;
	bool drive_high; /* di: level 1 allowed (push-pull high) */
	const char *stim_pin;
	const char *dut_pin;
};

#ifdef STIM_NXP_FLEXPWM
#define CAP_IS_FLEXPWM(n)                                                                        \
	COND_CODE_1(DT_NODE_HAS_PROP(n, pwms),                                                   \
		    (DT_NODE_HAS_COMPAT(DT_PWMS_CTLR(n), nxp_imx_pwm)), (0))
#define CAP_NXP_INIT(n)                                                                          \
	.cap_base = COND_CODE_1(CAP_IS_FLEXPWM(n),                                               \
				(DT_REG_ADDR(DT_PARENT(DT_PWMS_CTLR(n)))), (0)),                 \
	.cap_sm = COND_CODE_1(CAP_IS_FLEXPWM(n), (DT_PROP(DT_PWMS_CTLR(n), index)), (0)),
#else
#define CAP_NXP_INIT(n)
#endif

#define CHAN_INIT(n)                                                                             \
	{                                                                                        \
		.name = DT_NODE_FULL_NAME(n),                                                    \
		.kind = DT_ENUM_IDX(n, kind),                                                    \
		.gpio = GPIO_DT_SPEC_GET_OR(n, gpios, {0}),                                      \
		.sense = ADC_DT_SPEC_GET_BY_NAME_OR(n, sense, {0}),                              \
		.src = DAC_DT_SPEC_GET_BY_NAME_OR(n, src, {0}),                                  \
		.cap = PWM_DT_SPEC_GET_OR(n, {0}),                                               \
		CAP_NXP_INIT(n)                                                                  \
		.lo_mv = COND_CODE_1(DT_NODE_HAS_PROP(n, range_mv),                              \
				     (DT_PROP_BY_IDX(n, range_mv, 0)), (0)),                     \
		.hi_mv = COND_CODE_1(DT_NODE_HAS_PROP(n, range_mv),                              \
				     (DT_PROP_BY_IDX(n, range_mv, 1)), (3300)),                  \
		.num = DT_PROP_BY_IDX(n, scale, 0),                                              \
		.den = DT_PROP_BY_IDX(n, scale, 1),                                              \
		.drive_high = DT_PROP(n, drive_high),                                            \
		.stim_pin = DT_PROP_OR(n, stim_pin, "-"),                                        \
		.dut_pin = DT_PROP_OR(n, dut_pin, "-"),                                          \
	}

static const struct chan chans[] = {DT_FOREACH_CHILD_STATUS_OKAY_SEP(STIM_NODE, CHAN_INIT, (,))};
#define NCHAN ARRAY_SIZE(chans)

enum { ST_Z = 0, ST_0, ST_1 };
static uint8_t dstate[NCHAN]; /* di/sync/nrst: last commanded state */
static bool src_on[NCHAN];    /* ai: source enabled */
static bool src_nak[NCHAN];   /* ai: external source DAC did not answer at the last safe/init */
static bool pwr_on = true;
static int32_t vdda_mv = 3300;

/* The analog sources also refuse while the sensed DUT MCU rail (channel "v3v3")
 * is below this, which covers power cuts the stimulus did not command
 * (usb_port, HIL.md D39). */
#define DUT_ON_MV 3000

static const struct chan *find(const char *name)
{
	for (size_t i = 0; i < NCHAN; i++) {
		if (strcmp(chans[i].name, name) == 0) {
			return &chans[i];
		}
	}
	return NULL;
}

static const struct chan *find_kind(uint8_t kind)
{
	for (size_t i = 0; i < NCHAN; i++) {
		if (chans[i].kind == kind) {
			return &chans[i];
		}
	}
	return NULL;
}

static inline size_t idx(const struct chan *c)
{
	return (size_t)(c - chans);
}

/* Edge-capable inputs. K_AO is excluded on purpose: its pin is in TIM AF mode
 * (pwmcap) and pinctrl is applied only once at pwm init, so it must never be
 * reconfigured as a GPIO input (lines_safe) or armed on EXTI (edges/lat; the ao
 * pins also share EXTI lines 0/12/15 with m0, do4 and nrst_sense). ao levels
 * are read with `din` (IDR) or `pwmcap` only. */
static inline bool is_input_kind(uint8_t k)
{
	return k == K_DO || k == K_MARKER || k == K_NRST_SENSE;
}

/* --------------------------------------------------------- parsing ----- */

static int u32_arg(const char *s, uint32_t lo, uint32_t hi, uint32_t *out)
{
	char *end;
	unsigned long v;

	if (s == NULL || *s == '\0' || *s == '-' || *s == '+') {
		return -EINVAL;
	}
	v = strtoul(s, &end, 0);
	if (*end != '\0' || v < lo || v > hi) {
		return -EINVAL;
	}
	*out = (uint32_t)v;
	return 0;
}

static int level_arg(const char *s, bool allow_z)
{
	if (strcmp(s, "0") == 0) {
		return ST_0;
	}
	if (strcmp(s, "1") == 0) {
		return ST_1;
	}
	if (allow_z && strcmp(s, "z") == 0) {
		return ST_Z;
	}
	return -EINVAL;
}

/* D40: a di line is 0/z unless its node sets drive-high. */
static bool level_allowed(const struct chan *c, int st)
{
	return !(c->kind == K_DI && st == ST_1 && !c->drive_high);
}

/* --------------------------------------------------------- timebase ---- */

static inline uint64_t cyc_ns(uint64_t cyc)
{
	return k_cyc_to_ns_floor64(cyc);
}

static inline uint64_t now_ns(void)
{
	return cyc_ns(k_cycle_get_64());
}

/* command deadline watchdog (checked by main) */
static atomic_t deadline_ms;
/* stim_cmd_overrun() compares 32-bit uptimes as a signed difference: keep budgets far below 2^31 */
#define BUDGET_MAX_MS 0x40000000ULL

/* budget_ms: the command's own worst-case duration; main starves the IWDG 5 s after it */
static void cmd_begin(uint64_t budget_ms)
{
	uint32_t budget = (uint32_t)MIN(budget_ms, BUDGET_MAX_MS);

	atomic_set(&deadline_ms, (atomic_val_t)(k_uptime_get_32() + budget + 5000U));
}

/* worst-case time on the wire for n octets at the RS-485 UART's current rate (12 bit times each:
 * start, 8 data, parity, 2 stop), so long frames at low rates stay within the command budget
 */
static uint64_t rs485_octets_ms(const struct device *uart, size_t n)
{
	struct uart_config cfg;
	uint32_t baud = 1200U; /* the lowest rate "rs485 baud" accepts */

	if (uart_config_get(uart, &cfg) == 0 && cfg.baudrate >= 1U) {
		baud = cfg.baudrate;
	}
	return ((uint64_t)n * 12U * 1000U + baud - 1U) / baud;
}

static void cmd_end(void)
{
	atomic_set(&deadline_ms, 0);
}

bool stim_cmd_overrun(void)
{
	atomic_val_t d = atomic_get(&deadline_ms);

	return d != 0 && (int32_t)(k_uptime_get_32() - (uint32_t)d) > 0;
}

/* ---------------------------------------------------------- digital ---- */

static int drive(const struct chan *c, uint8_t st)
{
	int rc;

	if (st == ST_Z) {
		rc = gpio_pin_configure_dt(&c->gpio, GPIO_INPUT);
	} else {
		rc = gpio_pin_configure_dt(&c->gpio, st == ST_1 ? GPIO_OUTPUT_HIGH : GPIO_OUTPUT_LOW);
	}
	if (rc == 0) {
		dstate[idx(c)] = st;
	}
	return rc;
}

/* ------------------------------------------------------------- edges --- */

#define EDGE_MAX 1024
static struct {
	uint64_t cyc;
	uint8_t lv;
} edge_buf[EDGE_MAX];
static volatile uint32_t edge_n;
static volatile int edge_want = -1;
static const struct chan *edge_chan;
static struct gpio_callback edge_cb;
static K_SEM_DEFINE(edge_sem, 0, 1);

/* background NRST monitor (counts DUT resets, incl. watchdog/software) */
static const struct chan *rst_chan;
static struct gpio_callback rst_cb;
static atomic_t rst_count;
static uint64_t rst_last_cyc;

static void edge_isr(const struct device *port, struct gpio_callback *cb, gpio_port_pins_t pins)
{
	uint64_t t = k_cycle_get_64();
	int lv = edge_chan ? gpio_pin_get_dt(&edge_chan->gpio) : -1;
	uint32_t n = edge_n;

	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);
	if (n < EDGE_MAX) {
		edge_buf[n].cyc = t;
		edge_buf[n].lv = (uint8_t)lv;
	}
	edge_n = n + 1;
	if (edge_want < 0 || lv == edge_want) {
		k_sem_give(&edge_sem);
	}
}

static void rst_isr(const struct device *port, struct gpio_callback *cb, gpio_port_pins_t pins)
{
	uint64_t t = k_cycle_get_64();

	ARG_UNUSED(port);
	ARG_UNUSED(cb);
	ARG_UNUSED(pins);
	if (gpio_pin_get_dt(&rst_chan->gpio) == 1) { /* entered reset */
		rst_last_cyc = t;
		atomic_inc(&rst_count);
	}
}

static void edge_disarm(void)
{
	if (edge_chan == NULL) {
		return;
	}
	if (edge_chan == rst_chan) {
		(void)gpio_pin_interrupt_configure_dt(&edge_chan->gpio, GPIO_INT_EDGE_BOTH);
	} else {
		(void)gpio_pin_interrupt_configure_dt(&edge_chan->gpio, GPIO_INT_DISABLE);
	}
	(void)gpio_remove_callback_dt(&edge_chan->gpio, &edge_cb);
	edge_chan = NULL;
}

static int edge_arm(const struct chan *c, gpio_flags_t mode, int want)
{
	int rc;

	if (edge_chan != NULL) {
		return -EBUSY;
	}
	edge_n = 0;
	edge_want = want;
	k_sem_reset(&edge_sem);
	edge_chan = c;
	gpio_init_callback(&edge_cb, edge_isr, BIT(c->gpio.pin));
	rc = gpio_add_callback_dt(&c->gpio, &edge_cb);
	if (rc == 0) {
		rc = gpio_pin_interrupt_configure_dt(&c->gpio, mode);
	}
	if (rc != 0) {
		edge_disarm();
	}
	return rc;
}

/* ----------------------------------------------------------- analog ---- */

static void vdda_update(void)
{
#if DT_NODE_HAS_STATUS(DT_NODELABEL(vref), okay) && defined(CONFIG_STM32_VREF)
	const struct device *vref = DEVICE_DT_GET(DT_NODELABEL(vref));
	struct sensor_value v;

	if (device_is_ready(vref) && sensor_sample_fetch(vref) == 0 &&
	    sensor_channel_get(vref, SENSOR_CHAN_VOLTAGE, &v) == 0) {
		int32_t mv = v.val1 * 1000 + v.val2 / 1000;

		if (mv > 2700 && mv < 3600) {
			vdda_mv = mv;
		}
	}
#endif
}

static void src_off(const struct chan *c)
{
#ifdef STIM_STM32_DAC
	if (c->src.dev == DEVICE_DT_GET(DT_NODELABEL(dac1))) {
		DAC_TypeDef *dac = (DAC_TypeDef *)DT_REG_ADDR(DT_NODELABEL(dac1));

		/* EN = 0: output Hi-Z (pin stays analog) */
		LL_DAC_Disable(dac, c->src.channel_id == 1 ? LL_DAC_CHANNEL_1 : LL_DAC_CHANNEL_2);
		src_on[idx(c)] = false;
		return;
	}
#endif
	/* e.g. MCP4728: 0 V. Its driver reports "ready" without probing the bus,
	 * so a missing AFE shows up here; "dac" then answers ERR -19 (hardware
	 * absent) instead of -5, as for a missing on-chip DAC. */
	src_nak[idx(c)] = dac_write_value_dt(&c->src, 0) != 0;
	src_on[idx(c)] = false;
}

/* Reference (full scale) of a channel's source DAC in mV. The STM32 on-chip DAC
 * is ratiometric to VDDA, measured from VREFINT; an external DAC (MCP4728)
 * declares its reference as zephyr,vref-mv on its channel node. */
static uint32_t src_ref_mv(const struct chan *c)
{
#ifdef STIM_STM32_DAC
	if (c->src.dev == DEVICE_DT_GET(DT_NODELABEL(dac1))) {
		return (uint32_t)vdda_mv;
	}
#endif
	return c->src.vref_mv != 0U ? c->src.vref_mv : (uint32_t)vdda_mv;
}

static int adc_mean(const struct chan *c, uint32_t n, int32_t *raw_mean)
{
	uint16_t sample; /* single-ended; up to 16 bit (MCX LPADC) */
	int64_t sum = 0;
	struct adc_sequence seq = {.buffer = &sample, .buffer_size = sizeof(sample)};
	int rc = adc_channel_setup_dt(&c->sense);

	if (rc == 0) {
		rc = adc_sequence_init_dt(&c->sense, &seq);
	}
	for (uint32_t i = 0; rc == 0 && i < n; i++) {
		rc = adc_read_dt(&c->sense, &seq);
		sum += sample;
	}
	*raw_mean = (int32_t)((sum + n / 2) / n);
	return rc;
}

/* Mean of n samples of a sense input in mV at the rig node (divider applied). */
static int sense_mv(const struct chan *c, uint32_t n, int32_t *raw, int32_t *mv)
{
	/* full scale from the channel's zephyr,resolution (12 on F767, 16 on MCX) */
	int32_t fs = (int32_t)BIT(c->sense.resolution ? c->sense.resolution : 12U) - 1;
	int rc = adc_mean(c, n, raw);

	*mv = (int32_t)(((int64_t)*raw * vdda_mv * c->num + (int64_t)(fs / 2) * c->den) /
			((int64_t)fs * c->den));
	return rc;
}

/* --------------------------------------------------------- safe state -- */

/* Every DUT-facing line released; NRST released; sources off. Power unchanged. */
static int lines_safe(void)
{
	int rc = 0;

	edge_disarm();
	for (size_t i = 0; i < NCHAN; i++) {
		const struct chan *c = &chans[i];

		switch (c->kind) {
		case K_DI:
			rc |= drive(c, ST_Z);
			break;
		case K_AI:
			if (c->src.dev != NULL) {
				src_off(c);
			}
			break;
		case K_NRST:
		case K_SYNC:
			rc |= gpio_pin_configure_dt(&c->gpio, GPIO_OUTPUT_INACTIVE);
			dstate[i] = ST_0;
			break;
		default:
			if (c->gpio.port != NULL && is_input_kind(c->kind)) {
				rc |= gpio_pin_configure_dt(&c->gpio, GPIO_INPUT);
			}
			break;
		}
	}
	return rc;
}

static int set_power(bool on)
{
	const struct chan *p = find_kind(K_PWR);
	int rc;

	if (p == NULL) {
		return -ENODEV;
	}
	rc = gpio_pin_configure_dt(&p->gpio, on ? GPIO_OUTPUT_ACTIVE : GPIO_OUTPUT_INACTIVE);
	if (rc == 0) {
		pwr_on = on;
	}
	return rc;
}

/* ---------------------------------------------------------- commands --- */

static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argv);
	if (argc != 1) { /* protocol 3: a wrong argument count is ERR -22, as for every command */
		return err(sh, E_INVAL, "usage: stim info");
	}
	vdda_update();
	shell_fprintf(sh, SHELL_NORMAL,
		      "OK proto=%d fw=%s board=%s profile=%s uptime_ms=%llu vdda_mv=%d pwr=%d "
		      "rst_n=%d caps=pwmcap,rs485,rstmon chans=",
		      STIM_PROTO, STIM_FW_VERSION, CONFIG_BOARD_TARGET, STIM_PROFILE,
		      (unsigned long long)k_uptime_get(), vdda_mv, pwr_on,
		      (int)atomic_get(&rst_count));
	for (size_t i = 0; i < NCHAN; i++) {
		shell_fprintf(sh, SHELL_NORMAL, "%s%s", i ? "," : "", chans[i].name);
	}
	shell_fprintf(sh, SHELL_NORMAL, "\n");
	return 0;
}

static void print_nospace(const struct shell *sh, const char *key, const char *s)
{
	shell_fprintf(sh, SHELL_NORMAL, " %s=", key);
	for (; *s; s++) {
		shell_fprintf(sh, SHELL_NORMAL, "%c", *s == ' ' ? '_' : *s);
	}
}

static int cmd_chan(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c = argc == 2 ? find(argv[1]) : NULL;

	if (argc != 2) {
		return err(sh, E_INVAL, "usage: stim chan <name>");
	}
	if (c == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	shell_fprintf(sh, SHELL_NORMAL, "OK name=%s kind=%s src=%d sense=%d cap=%d", c->name,
		      kind_name[c->kind], c->src.dev != NULL, c->sense.dev != NULL,
		      c->cap.dev != NULL);
	print_nospace(sh, "pin", c->stim_pin);
	print_nospace(sh, "dut", c->dut_pin);
	shell_fprintf(sh, SHELL_NORMAL, "\n");
	return 0;
}

static int cmd_safe(const struct shell *sh, size_t argc, char **argv)
{
	int rc;

	ARG_UNUSED(argv);
	if (argc != 1) {
		return err(sh, E_INVAL, "usage: stim safe");
	}
	rc = lines_safe();
	rc |= set_power(true);
	if (rc != 0) {
		return err(sh, E_IO, "safe");
	}
	shell_print(sh, "OK pwr=1");
	return 0;
}

/* stim dout <chan> <0|1|z> */
static int cmd_dout(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	int st;
	uint64_t t;

	if (argc != 3) {
		return err(sh, E_INVAL, "usage: stim dout <chan> <0|1|z>");
	}
	c = find(argv[1]);
	if (c == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if (c->kind != K_DI && c->kind != K_SYNC) {
		return err(sh, E_PERM, "not an output channel");
	}
	st = level_arg(argv[2], c->kind == K_DI);
	if (st < 0) {
		return err(sh, E_INVAL, "level");
	}
	if (!level_allowed(c, st)) {
		return err(sh, E_PERM, "di is 0/z only");
	}
	if (drive(c, (uint8_t)st) != 0) {
		return err(sh, E_IO, "gpio");
	}
	t = now_ns();
	shell_print(sh, "OK t_ns=%llu", (unsigned long long)t);
	return 0;
}

/* Level of a pin without reconfiguring it. ao pins stay in their timer function:
 * STM32 IDR reads AF pins; on NXP FlexPWM the submodule's OCTRL.PWMx_IN bit is the
 * level at the capture input (channel 0 = A, 1 = B, 2 = X in pwm_mcux). */
static int pin_level(const struct chan *c)
{
#ifdef STIM_NXP_FLEXPWM
	if (c->kind == K_AO && c->cap_base != 0U && c->cap.channel <= 2U) {
		static const uint16_t in_mask[] = {PWM_OCTRL_PWMA_IN_MASK, PWM_OCTRL_PWMB_IN_MASK,
						   PWM_OCTRL_PWMX_IN_MASK};
		const PWM_Type *base = (const PWM_Type *)c->cap_base;

		return (base->SM[c->cap_sm].OCTRL & in_mask[c->cap.channel]) != 0U ? 1 : 0;
	}
#endif
	return c->gpio.port != NULL ? gpio_pin_get_dt(&c->gpio) : -1;
}

/* stim din <chan> */
static int cmd_din(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	int v;

	if (argc != 2) {
		return err(sh, E_INVAL, "usage: stim din <chan>");
	}
	c = find(argv[1]);
	if (c == NULL || c->gpio.port == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	v = pin_level(c); /* inputs, outputs and ao pins in their timer function */
	if (v < 0) {
		return err(sh, E_IO, "gpio");
	}
	shell_print(sh, "OK v=%d", v);
	return 0;
}

/* stim pulse <chan> <width_us> [count=1] [period_us=2*width] [active=0|1] */
#define PULSE_BUSY_MAX_US 50000U /* IRQ-locked limit, < SysTick wrap (77.6 ms @ 216 MHz) */
/* sleep mode: each k_usleep phase ends on a kernel tick and may run up to 2 ticks long
 * (rounded up, plus the partial current tick), so a train of count pulses can overrun its
 * nominal length by count x 4 ticks (40 s for 100000 pulses at 10 kHz): the command budget
 * must include it, or main starves the IWDG in the middle of a long train
 */
#define PULSE_SLACK_US (4U * (1000000U / CONFIG_SYS_CLOCK_TICKS_PER_SEC))
static int cmd_pulse(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	uint32_t width, count = 1, period, act;
	uint8_t idle;
	uint64_t total, t0;
	bool busy;

	if (argc < 3 || argc > 6) {
		return err(sh, E_INVAL, "usage: stim pulse <chan> <width_us> [count] [period_us] [0|1]");
	}
	c = find(argv[1]);
	if (c == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if (c->kind != K_DI && c->kind != K_SYNC) {
		return err(sh, E_PERM, "not an output channel");
	}
	if (u32_arg(argv[2], 1, 10000000, &width) || (argc > 3 && u32_arg(argv[3], 1, 100000, &count))) {
		return err(sh, E_INVAL, "width/count");
	}
	period = 2 * width;
	if (argc > 4 && u32_arg(argv[4], width + 1, 20000000, &period)) {
		return err(sh, E_INVAL, "period");
	}
	idle = dstate[idx(c)];
	if (argc > 5) {
		int l = level_arg(argv[5], false);

		if (l < 0) {
			return err(sh, E_INVAL, "active level");
		}
		act = (l == ST_1) ? ST_1 : ST_0;
	} else if (idle == ST_Z) {
		return err(sh, E_INVAL, "active level required when z");
	} else {
		act = (idle == ST_1) ? ST_0 : ST_1;
	}
	if (act == idle) {
		return err(sh, E_INVAL, "active level equals idle");
	}
	if (!level_allowed(c, (int)act) || !level_allowed(c, idle)) {
		return err(sh, E_PERM, "di is 0/z only");
	}
	total = (uint64_t)count * period;
	if (total > 600000000ULL) {
		return err(sh, E_RANGE, "train longer than 600 s");
	}
	busy = total <= PULSE_BUSY_MAX_US;
	cmd_begin((total + (busy ? 0U : (uint64_t)count * PULSE_SLACK_US)) / 1000U + 1U);

	if (busy) {
		unsigned int key = irq_lock();

		t0 = k_cycle_get_64();
		for (uint32_t i = 0; i < count; i++) {
			(void)drive(c, (uint8_t)act);
			k_busy_wait(width);
			(void)drive(c, idle);
			k_busy_wait(period - width);
		}
		irq_unlock(key);
	} else {
		t0 = k_cycle_get_64();
		for (uint32_t i = 0; i < count; i++) {
			(void)drive(c, (uint8_t)act);
			k_usleep(width);
			(void)drive(c, idle);
			k_usleep(period - width);
		}
	}
	cmd_end();
	shell_print(sh, "OK n=%u t0_ns=%llu mode=%s", count, (unsigned long long)cyc_ns(t0),
		    busy ? "busy" : "sleep");
	return 0;
}

/* stim edges <chan> <window_ms> [max=64] */
static int cmd_edges(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	uint32_t win, max = 64, n;
	uint64_t t0;
	int rc;

	if (argc < 3 || argc > 4) {
		return err(sh, E_INVAL, "usage: stim edges <chan> <window_ms> [max]");
	}
	c = find(argv[1]);
	if (c == NULL || c->gpio.port == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if (!is_input_kind(c->kind)) {
		return err(sh, E_PERM, "not an input channel");
	}
	if (u32_arg(argv[2], 1, 60000, &win) || (argc > 3 && u32_arg(argv[3], 1, EDGE_MAX, &max))) {
		return err(sh, E_INVAL, "window/max");
	}
	rc = edge_arm(c, GPIO_INT_EDGE_BOTH, -1);
	if (rc == -EBUSY) {
		return err(sh, E_BUSY, "edge engine busy");
	} else if (rc != 0) {
		return err(sh, E_IO, "irq");
	}
	cmd_begin(win);
	t0 = k_cycle_get_64();
	k_msleep(win);
	edge_disarm();
	cmd_end();

	n = edge_n;
	shell_fprintf(sh, SHELL_NORMAL, "OK n=%u t0_ns=%llu trunc=%d t_ns=", n,
		      (unsigned long long)cyc_ns(t0), n > max);
	for (uint32_t i = 0; i < MIN(n, max); i++) {
		shell_fprintf(sh, SHELL_NORMAL, "%s%llu", i ? "," : "",
			      (unsigned long long)cyc_ns(edge_buf[i].cyc));
	}
	shell_fprintf(sh, SHELL_NORMAL, " lv=");
	for (uint32_t i = 0; i < MIN(n, max); i++) {
		shell_fprintf(sh, SHELL_NORMAL, "%s%u", i ? "," : "", edge_buf[i].lv);
	}
	shell_fprintf(sh, SHELL_NORMAL, "\n");
	return 0;
}

/* stim lat <out> <0|1|z> <in> <0|1> <timeout_ms> */
static int cmd_lat(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *out, *in;
	int lo, li, rc;
	uint32_t tmo;
	uint64_t t0, t_in;
	unsigned int key;

	if (argc != 6) {
		return err(sh, E_INVAL, "usage: stim lat <out> <0|1|z> <in> <0|1> <timeout_ms>");
	}
	out = find(argv[1]);
	in = find(argv[3]);
	if (out == NULL || in == NULL || in->gpio.port == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if ((out->kind != K_DI && out->kind != K_SYNC) || !is_input_kind(in->kind)) {
		return err(sh, E_PERM, "out must be di/sync, in must be do/marker/nrst_sense");
	}
	lo = level_arg(argv[2], out->kind == K_DI);
	li = level_arg(argv[4], false);
	if (lo < 0 || li < 0 || u32_arg(argv[5], 1, 60000, &tmo)) {
		return err(sh, E_INVAL, "level/timeout");
	}
	if (!level_allowed(out, lo)) {
		return err(sh, E_PERM, "di is 0/z only");
	}
	li = (li == ST_1) ? 1 : 0;
	if (gpio_pin_get_dt(&in->gpio) == li) {
		return err(sh, E_ALREADY, "in already at level");
	}
	rc = edge_arm(in, li ? GPIO_INT_EDGE_TO_ACTIVE : GPIO_INT_EDGE_TO_INACTIVE, li);
	if (rc != 0) {
		return err(sh, rc == -EBUSY ? E_BUSY : E_IO, "irq");
	}
	cmd_begin(tmo);
	/* The input ISR must not timestamp an edge before t0 exists (lb answers in ~1 us). */
	key = irq_lock();
	(void)drive(out, (uint8_t)lo);
	t0 = k_cycle_get_64();
	irq_unlock(key);
	rc = k_sem_take(&edge_sem, K_MSEC(tmo));
	edge_disarm();
	cmd_end();
	if (rc != 0) {
		return err(sh, E_TIMEDOUT, "timeout");
	}
	t_in = MAX(edge_buf[0].cyc, t0);
	shell_print(sh, "OK lat_ns=%llu t0_ns=%llu", (unsigned long long)cyc_ns(t_in - t0),
		    (unsigned long long)cyc_ns(t0));
	return 0;
}

/* stim dac <chan> <mV|z> */
static int cmd_dac(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	uint32_t mv, code, ref, fs;
	int rc;

	if (argc != 3) {
		return err(sh, E_INVAL, "usage: stim dac <chan> <mV|z>");
	}
	c = find(argv[1]);
	if (c == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if (c->src.dev == NULL) {
		return err(sh, E_NOTSUP, "channel has no source (fixed divider?)");
	}
	if (!device_is_ready(c->src.dev) || src_nak[idx(c)]) {
		return err(sh, E_NODEV, "source device");
	}
	if (strcmp(argv[2], "z") == 0) {
		src_off(c);
		shell_print(sh, "OK mv=z");
		return 0;
	}
	if (u32_arg(argv[2], 0, 5000, &mv)) {
		return err(sh, E_INVAL, "mV");
	}
	if (mv < (uint32_t)c->lo_mv || mv > (uint32_t)c->hi_mv) {
		return err(sh, E_RANGE, "outside usable range");
	}
	if (!pwr_on && mv > 0) {
		return err(sh, E_PERM, "dut unpowered");
	}
	vdda_update();
	if (mv > 0) {
		const struct chan *s = find("v3v3");
		int32_t raw, smv;

		if (s != NULL && s->sense.dev != NULL &&
		    (sense_mv(s, 4, &raw, &smv) != 0 || smv < DUT_ON_MV)) {
			return err(sh, E_PERM, "dut unpowered (v3v3 sense)");
		}
	}
	ref = src_ref_mv(c);
	fs = BIT(c->src.channel_cfg.resolution ? c->src.channel_cfg.resolution : 12U) - 1U;
	code = MIN((mv * fs + ref / 2U) / ref, fs);
	rc = dac_channel_setup_dt(&c->src);
	if (rc == 0) {
		rc = dac_write_value_dt(&c->src, code);
	}
	if (rc != 0) {
		return err(sh, E_IO, "dac");
	}
	src_on[idx(c)] = true;
	k_usleep(2500); /* >= 10 tau of the 2.2 kOhm / 100 nF node (tau 0.22 ms; 1 kOhm: 25 tau) */
	shell_print(sh, "OK mv=%u code=%u vdda_mv=%d", (code * ref + fs / 2U) / fs, code, vdda_mv);
	return 0;
}

/* stim adc <chan> [n=16] */
static int cmd_adc(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	uint32_t n = 16;
	int32_t raw, mv;

	if (argc < 2 || argc > 3) {
		return err(sh, E_INVAL, "usage: stim adc <chan> [n]");
	}
	c = find(argv[1]);
	if (c == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if (c->sense.dev == NULL) {
		return err(sh, E_NOTSUP, "channel has no sense input");
	}
	if (argc == 3 && u32_arg(argv[2], 1, 1024, &n)) {
		return err(sh, E_INVAL, "n");
	}
	vdda_update();
	cmd_begin(n);
	if (sense_mv(c, n, &raw, &mv) != 0) {
		cmd_end();
		return err(sh, E_IO, "adc");
	}
	cmd_end();
	shell_print(sh, "OK mv=%d raw=%d n=%u vdda_mv=%d", mv, raw, n, vdda_mv);
	return 0;
}

/* stim reset [hold_ms=10] */
static int cmd_reset(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *r = find_kind(K_NRST);
	uint32_t ms = 10;
	uint64_t t;

	if (argc > 2 || (argc == 2 && u32_arg(argv[1], 1, 10000, &ms))) {
		return err(sh, E_INVAL, "usage: stim reset [hold_ms 1..10000]");
	}
	if (r == NULL) {
		return err(sh, E_NODEV, "no nrst channel");
	}
	cmd_begin(ms);
	(void)gpio_pin_configure_dt(&r->gpio, GPIO_OUTPUT_ACTIVE);
	k_msleep(ms);
	(void)gpio_pin_set_dt(&r->gpio, 0);
	t = now_ns();
	cmd_end();
	shell_print(sh, "OK held_ms=%u t_ns=%llu", ms, (unsigned long long)t);
	return 0;
}

/* stim power <on|off|cycle> [off_ms=2000] */
static int cmd_power(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t off_ms = 2000;
	int rc = 0;

	if (argc < 2 || argc > 3 || (argc == 3 && u32_arg(argv[2], 10, 60000, &off_ms))) {
		return err(sh, E_INVAL, "usage: stim power <on|off|cycle> [off_ms 10..60000]");
	}
	if (strcmp(argv[1], "on") == 0) {
		rc = set_power(true);
	} else if (strcmp(argv[1], "off") == 0) {
		rc = lines_safe();
		rc |= set_power(false);
	} else if (strcmp(argv[1], "cycle") == 0) {
		cmd_begin(off_ms);
		rc = lines_safe();
		rc |= set_power(false);
		k_msleep(off_ms);
		rc |= set_power(true);
		cmd_end();
	} else {
		return err(sh, E_INVAL, "on|off|cycle");
	}
	if (rc != 0) {
		return err(sh, E_IO, "power");
	}
	shell_print(sh, "OK pwr=%d t_ns=%llu", pwr_on, (unsigned long long)now_ns());
	return 0;
}

/* stim rstmon [clear] : DUT resets seen on nrst_sense since boot/clear */
static int cmd_rstmon(const struct shell *sh, size_t argc, char **argv)
{
	bool clear = argc == 2 && strcmp(argv[1], "clear") == 0;
	unsigned int key;
	atomic_val_t n;
	uint64_t last;

	if (argc > 2 || (argc == 2 && !clear)) {
		return err(sh, E_INVAL, "usage: stim rstmon [clear]");
	}
	if (rst_chan == NULL) {
		return err(sh, E_NODEV, "no nrst_sense channel");
	}
	/* One snapshot with rst_isr held off: the 64-bit time is two loads on the M7, and a reset
	 * between reading and clearing the count would otherwise be lost (atomic_set returns the
	 * count it replaces).
	 */
	key = irq_lock();
	n = clear ? atomic_set(&rst_count, 0) : atomic_get(&rst_count);
	last = rst_last_cyc;
	irq_unlock(key);
	shell_print(sh, "OK n=%d last_ns=%llu in_reset=%d", (int)n, (unsigned long long)cyc_ns(last),
		    gpio_pin_get_dt(&rst_chan->gpio));
	return 0;
}

#ifdef STIM_NXP_FLEXPWM
/*
 * pwm_mcux (Zephyr 4.4.2) as a capture-only user:
 *  - it never programs INIT/VAL1, so a submodule without a PWM output keeps the
 *    reset values (modulo 0: the counter never advances and every capture reads
 *    the same value); it computes the wrap from VAL1 - INIT;
 *  - it starts the counter only when no submodule of the FlexPWM runs at all
 *    (MCTRL.RUN == 0), so the second capture submodule would never be started.
 * Make the submodule a free-running 16-bit counter (INIT 0, VAL1 0xFFFF) and
 * run it. The stimulus never drives a PWM output, so nothing else owns it.
 */
static void cap_prepare(const struct chan *c)
{
	PWM_Type *base = (PWM_Type *)c->cap_base;
	uint8_t sm = (uint8_t)BIT(c->cap_sm);

	if (base == NULL) {
		return;
	}
	if (base->SM[c->cap_sm].VAL1 != 0xFFFFU || base->SM[c->cap_sm].INIT != 0U) {
		PWM_SetPwmLdok(base, sm, false);
		base->SM[c->cap_sm].INIT = 0U;
		base->SM[c->cap_sm].VAL1 = 0xFFFFU;
		PWM_SetPwmLdok(base, sm, true); /* loaded at start, or at the next reload */
	}
	if ((base->MCTRL & PWM_MCTRL_RUN(sm)) == 0U) {
		PWM_StartTimer(base, sm);
	}
}
#else
static inline void cap_prepare(const struct chan *c)
{
	ARG_UNUSED(c);
}
#endif

/* One single-mode capture. The driver is disarmed afterwards in every case:
 * pwm_mcux leaves capture_active set after a completed single capture, so the
 * next capture on that submodule would return -EBUSY (pwm_stm32 disarms itself;
 * a second disable is harmless). */
static int cap_once(const struct chan *c, pwm_flags_t type, uint64_t *period, uint64_t *pulse,
		    uint32_t tmo)
{
	int rc;

	cap_prepare(c);
	rc = pwm_capture_nsec(c->cap.dev, c->cap.channel,
			      c->cap.flags | type | PWM_CAPTURE_MODE_SINGLE, period, pulse,
			      K_MSEC(tmo));
	(void)pwm_disable_capture(c->cap.dev, c->cap.channel);
	return rc;
}

/* stim pwmcap <chan> [timeout_ms=200]  (It2 cap "pwmcap") */
static int cmd_pwmcap(const struct shell *sh, size_t argc, char **argv)
{
	const struct chan *c;
	uint32_t tmo = 200;
	uint64_t period = 0, pulse = 0, unused;
	int rc;

	if (argc < 2 || argc > 3) {
		return err(sh, E_INVAL, "usage: stim pwmcap <chan> [timeout_ms]");
	}
	c = find(argv[1]);
	if (c == NULL) {
		return err(sh, E_NOENT, "channel");
	}
	if (c->cap.dev == NULL) {
		return err(sh, E_NOTSUP, "channel has no capture");
	}
	if (argc == 3 && u32_arg(argv[2], 5, 60000, &tmo)) {
		return err(sh, E_INVAL, "timeout");
	}
	cmd_begin(2U * tmo);
	rc = cap_once(c, PWM_CAPTURE_TYPE_BOTH, &period, &pulse, tmo);
	if (rc == -ENOTSUP) {
		/* pwm_mcux captures one type per arm: period, then pulse (next cycle) */
		rc = cap_once(c, PWM_CAPTURE_TYPE_PERIOD, &period, &unused, tmo);
		if (rc == 0) {
			rc = cap_once(c, PWM_CAPTURE_TYPE_PULSE, &unused, &pulse, tmo);
		}
	}
	cmd_end();
	if (rc == -EAGAIN) {
		int lv = pin_level(c);

		/* no edges: static level = 0 % or 100 % duty */
		shell_print(sh, "OK period_ns=0 pulse_ns=0 duty_ppm=%d static=1 lv=%d",
			    lv == 1 ? 1000000 : 0, lv);
		return 0;
	}
	if (rc == -ERANGE) {
		return err(sh, E_RANGE, "period exceeds timer range");
	}
	if (rc == -ENOTSUP) {
		return err(sh, E_NOTSUP, "capture not supported on this channel");
	}
	if (rc != 0) {
		return err(sh, E_IO, "capture");
	}
	shell_print(sh, "OK period_ns=%llu pulse_ns=%llu duty_ppm=%llu static=0",
		    (unsigned long long)period, (unsigned long long)pulse,
		    (unsigned long long)(period ? pulse * 1000000ULL / period : 0));
	return 0;
}

/* ---------------------------------------------------- RS-485 (It2) ---- */

#define RS485_SLOTS     4
#define RS485_SLOT_SIZE 2048
#define RS485_CHUNK_MAX 200
static uint8_t slot_buf[RS485_SLOTS][RS485_SLOT_SIZE];
static uint16_t slot_len[RS485_SLOTS];

#if DT_NODE_HAS_PROP(STIM_NODE, rs485_uart)
static const struct device *const rs485 = DEVICE_DT_GET(RS485_NODE);
#else
static const struct device *const rs485;
#endif

/* Transmitter idle: last stop bit sent (hardware DE is released at this point). */
static inline bool rs485_tx_done(void)
{
#if defined(STIM_RS485_STM32)
	return LL_USART_IsActiveFlag_TC((USART_TypeDef *)DT_REG_ADDR(RS485_NODE)) != 0U;
#elif defined(STIM_RS485_LPUART)
	return (((LPUART_Type *)DT_REG_ADDR(RS485_NODE))->STAT & LPUART_STAT_TC_MASK) != 0U;
#else
	return true;
#endif
}

static int hex_decode(const char *s, uint8_t *out, size_t max)
{
	size_t n = strlen(s);

	if (n == 0 || (n & 1U) || n / 2 > max) {
		return -EINVAL;
	}
	return (int)hex2bin(s, n, out, max) == (int)(n / 2) ? (int)(n / 2) : -EINVAL;
}

/* stim rs485 load <slot> <off> <hex (<= 200 bytes)> */
static int cmd_rs485_load(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t slot, off;
	uint8_t tmp[RS485_CHUNK_MAX];
	int n;

	if (argc != 4 || u32_arg(argv[1], 0, RS485_SLOTS - 1, &slot) ||
	    u32_arg(argv[2], 0, RS485_SLOT_SIZE - 1, &off)) {
		return err(sh, E_INVAL, "usage: stim rs485 load <slot 0..3> <off> <hex>");
	}
	n = hex_decode(argv[3], tmp, sizeof(tmp));
	if (n < 0) {
		return err(sh, E_INVAL, "hex (even length, <= 200 bytes)");
	}
	if (off + (uint32_t)n > RS485_SLOT_SIZE) {
		return err(sh, E_MSGSIZE, "slot overflow");
	}
	memcpy(&slot_buf[slot][off], tmp, n);
	slot_len[slot] = MAX(slot_len[slot], (uint16_t)(off + n));
	shell_print(sh, "OK slot=%u len=%u crc32=%08x", slot, slot_len[slot],
		    crc32_ieee(slot_buf[slot], slot_len[slot]));
	return 0;
}

/* stim rs485 clear <slot> */
static int cmd_rs485_clear(const struct shell *sh, size_t argc, char **argv)
{
	uint32_t slot;

	if (argc != 2 || u32_arg(argv[1], 0, RS485_SLOTS - 1, &slot)) {
		return err(sh, E_INVAL, "usage: stim rs485 clear <slot>");
	}
	slot_len[slot] = 0;
	shell_print(sh, "OK slot=%u len=0", slot);
	return 0;
}

/* stim rs485 baud <baud> [skew_ppm] */
static int cmd_rs485_baud(const struct shell *sh, size_t argc, char **argv)
{
	struct uart_config cfg;
	uint32_t baud;
	long skew = 0;

	if (argc < 2 || argc > 3 || u32_arg(argv[1], 1200, 1000000, &baud)) {
		return err(sh, E_INVAL, "usage: stim rs485 baud <1200..1000000> [skew_ppm]");
	}
	if (argc == 3) {
		char *end;

		skew = strtol(argv[2], &end, 0);
		if (*end != '\0' || skew < -50000 || skew > 50000) {
			return err(sh, E_INVAL, "skew_ppm -50000..50000");
		}
	}
	if (rs485 == NULL || !device_is_ready(rs485) || uart_config_get(rs485, &cfg) != 0) {
		return err(sh, E_NODEV, "rs485 uart");
	}
	cfg.baudrate = (uint32_t)(((int64_t)baud * (1000000 + skew)) / 1000000);
	cfg.flow_ctrl = UART_CFG_FLOW_CTRL_RS485; /* keeps hardware DE (STM32 DEM, LPUART TXRTSE) */
	if (uart_configure(rs485, &cfg) != 0) {
		return err(sh, E_IO, "uart_configure");
	}
	shell_print(sh, "OK baud=%u", cfg.baudrate);
	return 0;
}

/* stim rs485 tx <@slot|hex> [gap=<idx>:<us>]... [rep=<n>] [per_ms=<p>] */
static int cmd_rs485_tx(const struct shell *sh, size_t argc, char **argv)
{
	static uint8_t inl[RS485_CHUNK_MAX];
	const uint8_t *buf;
	size_t len;
	uint32_t gap_idx[4], gap_us[4], ngap = 0, rep = 1, per_ms = 0;
	uint64_t t0 = 0, t1 = 0, frame_ms;

	if (argc < 2) {
		return err(sh, E_INVAL, "usage: stim rs485 tx <@slot|hex> [gap=i:us] [rep=n] [per_ms=p]");
	}
	if (rs485 == NULL || !device_is_ready(rs485)) {
		return err(sh, E_NODEV, "rs485 uart");
	}
	if (argv[1][0] == '@') {
		uint32_t slot;

		if (u32_arg(&argv[1][1], 0, RS485_SLOTS - 1, &slot) || slot_len[slot] == 0) {
			return err(sh, E_INVAL, "slot");
		}
		buf = slot_buf[slot];
		len = slot_len[slot];
	} else {
		int n = hex_decode(argv[1], inl, sizeof(inl));

		if (n < 0) {
			return err(sh, E_INVAL, "hex (<= 200 bytes inline; use load for more)");
		}
		buf = inl;
		len = (size_t)n;
	}
	for (size_t a = 2; a < argc; a++) {
		char *colon;

		if (strncmp(argv[a], "gap=", 4) == 0 && ngap < ARRAY_SIZE(gap_idx) &&
		    (colon = strchr(argv[a], ':')) != NULL) {
			*colon = '\0';
			if (u32_arg(&argv[a][4], 1, len - 1, &gap_idx[ngap]) ||
			    u32_arg(colon + 1, 1, 1000000, &gap_us[ngap])) {
				return err(sh, E_INVAL, "gap=<idx>:<us>");
			}
			ngap++;
		} else if (strncmp(argv[a], "rep=", 4) == 0) {
			if (u32_arg(&argv[a][4], 1, 10000, &rep)) {
				return err(sh, E_INVAL, "rep");
			}
		} else if (strncmp(argv[a], "per_ms=", 7) == 0) {
			if (u32_arg(&argv[a][7], 1, 60000, &per_ms)) {
				return err(sh, E_INVAL, "per_ms");
			}
		} else {
			return err(sh, E_INVAL, "option");
		}
	}
	/* per repetition the longer of per_ms and the frame on the wire (octets and gaps) */
	frame_ms = rs485_octets_ms(rs485, len);
	for (uint32_t g = 0; g < ngap; g++) {
		frame_ms += gap_us[g] / 1000U + 1U;
	}
	cmd_begin((uint64_t)rep * MAX((uint64_t)per_ms, frame_ms + 1U) + 2000U);
	for (uint32_t r = 0; r < rep; r++) {
		uint64_t start = k_uptime_get();

		for (size_t i = 0; i < len; i++) {
			for (uint32_t g = 0; g < ngap; g++) {
				if (gap_idx[g] == i) {
					while (!rs485_tx_done()) {
					}
					k_busy_wait(gap_us[g]);
				}
			}
			uart_poll_out(rs485, buf[i]);
			if (r == 0 && i == 0) {
				t0 = k_cycle_get_64();
			}
		}
		while (!rs485_tx_done()) {
		}
		t1 = k_cycle_get_64();
		if (per_ms && r + 1 < rep) {
			int64_t wait = (int64_t)per_ms - (k_uptime_get() - (int64_t)start);

			if (wait > 0) {
				k_msleep((int32_t)wait);
			}
		}
	}
	cmd_end();
	shell_print(sh, "OK n=%u rep=%u t0_ns=%llu t_end_ns=%llu", (unsigned int)len, rep,
		    (unsigned long long)cyc_ns(t0), (unsigned long long)cyc_ns(t1));
	return 0;
}

/* stim rs485 rx <timeout_ms> [idle_us=5000] (polled; It2 moves this to an ISR ring) */
static int cmd_rs485_rx(const struct shell *sh, size_t argc, char **argv)
{
	static uint8_t rbuf[RS485_SLOT_SIZE];
	uint32_t tmo, idle_us = 5000;
	size_t n = 0;
	uint64_t t0 = k_cycle_get_64(), tf = 0, tl = 0;
	unsigned char ch;

	if (argc < 2 || argc > 3 || u32_arg(argv[1], 1, 60000, &tmo) ||
	    (argc == 3 && u32_arg(argv[2], 100, 1000000, &idle_us))) {
		return err(sh, E_INVAL, "usage: stim rs485 rx <timeout_ms> [idle_us]");
	}
	if (rs485 == NULL || !device_is_ready(rs485)) {
		return err(sh, E_NODEV, "rs485 uart");
	}
	/* the timeout only bounds the wait for the first octet: a frame that starts late keeps
	 * the loop going until the line idles or the buffer is full, so budget for both
	 */
	cmd_begin((uint64_t)tmo + rs485_octets_ms(rs485, sizeof(rbuf)) + idle_us / 1000U + 1U);
	while (n < sizeof(rbuf)) {
		uint64_t now = k_cycle_get_64();

		if (uart_poll_in(rs485, &ch) == 0) {
			if (n == 0) {
				tf = now;
			}
			tl = now;
			rbuf[n++] = ch;
			continue;
		}
		if (n == 0 && cyc_ns(now - t0) >= (uint64_t)tmo * 1000000ULL) {
			break;
		}
		if (n > 0 && cyc_ns(now - tl) >= (uint64_t)idle_us * 1000ULL) {
			break;
		}
	}
	cmd_end();
	shell_fprintf(sh, SHELL_NORMAL, "OK n=%u t_first_ns=%llu t_last_ns=%llu hex=", (unsigned int)n,
		      (unsigned long long)(n ? cyc_ns(tf) : 0), (unsigned long long)(n ? cyc_ns(tl) : 0));
	for (size_t i = 0; i < n; i++) {
		shell_fprintf(sh, SHELL_NORMAL, "%02x", rbuf[i]);
	}
	shell_fprintf(sh, SHELL_NORMAL, "\n");
	return 0;
}

/* unknown subcommands land here, so the host always gets one OK/ERR line */
static int cmd_stim_root(const struct shell *sh, size_t argc, char **argv)
{
	ARG_UNUSED(argv);
	if (argc < 2) {
		return err(sh, E_INVAL, "usage: stim <command> ...");
	}
	return err(sh, E_NOTSUP, "unknown command");
}


SHELL_STATIC_SUBCMD_SET_CREATE(sub_rs485,
	SHELL_CMD_ARG(baud, NULL, "<baud> [skew_ppm]", cmd_rs485_baud, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(clear, NULL, "<slot>", cmd_rs485_clear, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(load, NULL, "<slot> <off> <hex<=200B>", cmd_rs485_load, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(tx, NULL, "<@slot|hex> [gap=i:us] [rep=n] [per_ms=p]", cmd_rs485_tx, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(rx, NULL, "<timeout_ms> [idle_us]", cmd_rs485_rx, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_SUBCMD_SET_END);

SHELL_STATIC_SUBCMD_SET_CREATE(sub_stim,
	SHELL_CMD_ARG(info, NULL, "protocol/firmware/channel info", cmd_info, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(chan, NULL, "<name>: channel description", cmd_chan, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(safe, NULL, "all DUT-facing lines released, DUT on", cmd_safe, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(dout, NULL, "<chan> <0|1|z>", cmd_dout, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(din, NULL, "<chan>", cmd_din, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(pulse, NULL, "<chan> <width_us> [count] [period_us] [0|1]", cmd_pulse,
		      1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(edges, NULL, "<chan> <window_ms> [max]", cmd_edges, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(lat, NULL, "<out> <0|1|z> <in> <0|1> <timeout_ms>", cmd_lat, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(dac, NULL, "<chan> <mV|z>", cmd_dac, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(adc, NULL, "<chan> [n]", cmd_adc, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(reset, NULL, "[hold_ms]", cmd_reset, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(power, NULL, "<on|off|cycle> [off_ms]", cmd_power, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(rstmon, NULL, "[clear]", cmd_rstmon, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(pwmcap, NULL, "<chan> [timeout_ms]", cmd_pwmcap, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_CMD_ARG(rs485, &sub_rs485, "MS/TP injector", cmd_stim_root, 1, SHELL_OPT_ARG_CHECK_SKIP),
	SHELL_SUBCMD_SET_END);

SHELL_CMD_ARG_REGISTER(stim, &sub_stim, "HIL stimulus commands", cmd_stim_root, 1, SHELL_OPT_ARG_CHECK_SKIP);

int stim_init(void)
{
	int rc = 0;

	for (size_t i = 0; i < NCHAN; i++) {
		const struct chan *c = &chans[i];

		if (c->gpio.port != NULL && !gpio_is_ready_dt(&c->gpio)) {
			rc = -ENODEV;
		}
		if (c->sense.dev != NULL && !adc_is_ready_dt(&c->sense)) {
			rc = -ENODEV;
		}
		if (c->src.dev != NULL && !dac_is_ready_dt(&c->src)) {
			rc = -ENODEV;
		}
		if (c->cap.dev != NULL && !pwm_is_ready_dt(&c->cap)) {
			rc = -ENODEV;
		}
	}
	/* DUT powered and every line released: same as the hardware reset state */
	rc |= set_power(true);
	rc |= lines_safe();

	rst_chan = find_kind(K_NRST_SENSE);
	if (rst_chan != NULL) {
		gpio_init_callback(&rst_cb, rst_isr, BIT(rst_chan->gpio.pin));
		rc |= gpio_add_callback_dt(&rst_chan->gpio, &rst_cb);
		rc |= gpio_pin_interrupt_configure_dt(&rst_chan->gpio, GPIO_INT_EDGE_BOTH);
	}
	vdda_update();
	return rc;
}
