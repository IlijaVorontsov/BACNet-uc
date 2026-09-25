/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * IO channels (devicetree "uc,io-channels" catalog) and their binding to
 * BACnet objects (io.json).
 *
 * Channel values are raw engineering values: di/do 0|1, ai millivolts,
 * ao duty percent 0..100. Point values (BACnet Present_Value) apply the
 * io.json scale/offset: analog PV = raw * scale + offset.
 *
 * Scan (uc_io_scan(), BACnet thread, every loop, each point rate-limited
 * by sample_ms):
 *   di -> BI/MSI PV (debounced, invert)     ai -> AI PV (scaled)
 *   BO/BV PV -> do (invert)                 AO/AV PV -> ao (inverse scale,
 *                                                        clamped min/max)
 */
#ifndef UC_IO_H_
#define UC_IO_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "uc_config.h"

#ifdef __cplusplus
extern "C" {
#endif

enum uc_io_kind {
	UC_IO_DI,
	UC_IO_DO,
	UC_IO_AI,
	UC_IO_AO,
};

enum uc_io_hw {
	UC_IO_HW_GPIO,
	UC_IO_HW_ADC,
	UC_IO_HW_PWM,
	UC_IO_HW_SIM,
};

struct uc_io_channel_info {
	int id;
	const char *name;
	const char *desc;
	enum uc_io_kind kind;
	enum uc_io_hw hw;
	bool forced;
	bool bound;
	/* object of the io.json point: meaningful only when bound (0 else) */
	uint16_t obj_type;
	uint32_t obj_instance;
};

const char *uc_io_kind_str(enum uc_io_kind kind);
const char *uc_io_hw_str(enum uc_io_hw hw);

/** Board name from the catalog node ("board-name"), CONFIG_BOARD else. */
const char *uc_io_board_name(void);

/** Initialise channel hardware (GPIO/ADC/PWM) from devicetree. */
int uc_io_init(void);

size_t uc_io_channel_count(void);
int uc_io_channel_info(int id, struct uc_io_channel_info *out);
/** Channel id by name, -ENOENT if absent. */
int uc_io_find(const char *name);

/** Read a channel (forced value if forced). Thread-safe. */
int uc_io_read(int id, double *value);
/** Drive an output channel. -EACCES for inputs. Thread-safe. */
int uc_io_write(int id, double value);
/** Override a channel value until released (inputs: what the scan sees;
 *  outputs: drives the hardware and ignores the bound object). */
int uc_io_force(int id, double value);
int uc_io_release(int id);

/** Bind points: delete objects previously created for IO
 *  (UC_OWNER_IO), create the objects of cfg with name, units, COV
 *  increment, and start scanning. BACnet thread only (called by the BACnet
 *  node at start and via the executor on reload). Points referring to
 *  unknown channels or incompatible kinds are skipped with an error log.
 *  Returns the number of bound points. */
int uc_io_apply_config_locked(const struct uc_io_cfg *cfg);

/** Thread-safe wrapper: runs uc_io_apply_config_locked() via the
 *  executor with the cached io.json. */
int uc_io_apply_config(void);

/** Scan step, BACnet thread only. */
void uc_io_scan(void);

#ifdef __cplusplus
}
#endif

#endif /* UC_IO_H_ */
