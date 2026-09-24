# CR-202 — RAM budget
From: firmware lead

We now run 512 points on a Cortex-M0 with 16 KB of RAM. `alarm_t` shrinks to 32 bytes
(`uint64_t storage[4]`, see the updated `alarm.h`, already in place). `alarm_init` no longer
copies the configuration: it keeps a pointer, and the caller guarantees that the
`alarm_cfg_t` outlives the point (configurations are shared between points).

External behavior must not change.
