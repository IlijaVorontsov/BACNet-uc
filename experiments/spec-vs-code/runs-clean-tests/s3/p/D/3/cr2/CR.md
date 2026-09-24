# CR-102 — Code size for the Cortex-M0 build
From: firmware lead

Our Cortex-M0 target has no FPU, and linking the soft-float support routines costs about
2 KB of flash. Refactor `bacapp.c`:

1. It must not use floating-point arithmetic, floating-point comparisons or `<math.h>`
   anywhere (access the bit patterns of `float`/`double` with `memcpy` instead).
2. All encoders must share one tag-header writer and one minimal-length integer writer
   (no per-type copies of that logic).

External behavior must not change.
