# CR-201 — Separate return-to-normal delay
From: product owner

Add `uint32_t time_delay_normal_s;` to `alarm_cfg_t` (BACnet Time_Delay_Normal).
Transitions from HIGH or LOW back to NORMAL use `time_delay_normal_s`; all other delayed
transitions (NORMAL→HIGH/LOW, HIGH↔LOW) keep using `time_delay_s`. The value
`0xFFFFFFFF` means "same as `time_delay_s`".

The updated `alarm.h` and `driver.c` are already in place (the driver's `cfg` command
accepts `delay_normal=N`; default 4294967295).
