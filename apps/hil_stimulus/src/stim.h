/* SPDX-License-Identifier: Apache-2.0 */
#ifndef HIL_STIM_H_
#define HIL_STIM_H_

#include <stdbool.h>

#define STIM_PROTO      0
#define STIM_FW_VERSION "0.2.0"

int stim_init(void);
/* true while a command runs past its own deadline (main then starves the IWDG) */
bool stim_cmd_overrun(void);

#endif
