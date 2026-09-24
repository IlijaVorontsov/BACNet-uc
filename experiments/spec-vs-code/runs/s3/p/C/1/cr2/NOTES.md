## CR-101

Neither item conflicts with ASHRAE 135 clause 20.2 or with the product's existing behaviour, so both are implemented.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double, 8 octets, big-endian. This matches clause 20.2.7: 72.0 encodes as `55 08 40 52 00 00 00 00 00 00`. It returns -1 without touching the buffer when `cap < 10` or `buf` is NULL.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly 8. Any other length is rejected. It still accepts a non-minimal length header such as `55 fe 00 08`, just as it does for the other types (see D1b).
   - Test changes: `tests/unit.vec` p0526 (`dec_app 5508` + 8 zero octets) used to expect `ERR`. It now expects `OK n=10 d 0000000000000000`, because rejecting tag 5 was the old behaviour. I added vectors `cr101-d*` for the encoder, decoder, capacity checks and NaN. I added the clause 20.2.7 example to `acceptance.vec` as `double-72` / `decode-double`.
   - Unsure: **NaN handling.** The CR says nothing about NaN. I made `bac_enc_double()` reject NaN (return -1) to match the earlier CR3 decision for `bac_enc_real()` (vectors tagged CR3nan). As with REAL, the decoder passes NaN bit patterns through unchanged. The standard itself does not forbid NaN, so the product owner should confirm that Double should follow REAL here. If not, remove the `isnan` check and vectors cr101-d11..d13.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.**
   - They mirror `bac_enc_ctx_unsigned()`. They use a context-class header with the tag number (the extended tag octet for tags 15..254) and the minimal content octets, unsigned for Enumerated and two's-complement for Signed. Tag 255 is rejected, as the standard reserves it and the other context encoders already reject it. The capacity checks leave the buffer untouched on error.
   - I added vectors `cr101-e*` and `cr101-s*` to `tests/unit.vec`.

`bacapp.h` and `driver.c` were already updated and I left them unchanged. `make` builds cleanly with -Wall -Wextra. `run_vectors.py acceptance.vec tests/unit.vec` gives 361/361, also with ASan/UBSan.

## CR-102

Neither item conflicts with ASHRAE 135 clause 20.2 or with the product's documented behaviour, so both are implemented. External behaviour is unchanged.

1. **No floating-point arithmetic, comparisons or `<math.h>`: implemented.**
   - `#include <math.h>` and both `isnan()` calls are gone. `bac_enc_real()` and `bac_enc_double()` now `memcpy` the value into a `uint32_t` / `uint64_t` and reject NaN (exponent all ones, fraction non-zero) with an integer test on the bits: `(bits & 0x7FFFFFFF) > 0x7F800000`, or the 64-bit equivalent `> 0x7FF0000000000000`. This is the same NaN set that `isnan()` matched, so the CR3nan / CR101 NaN decisions stand. Where the CR-101 note above says to "remove the `isnan` check", that now means this bit test in `bac_enc_double()`.
   - The decoder already used `memcpy` and needed no change. `float` / `double` still appear only as API types (parameters and the `bac_value_t` union in `bacapp.h`, which is unchanged). No arithmetic or comparison is done on them.
   - `Makefile`: dropped `-lm`, because nothing links against libm any more.
   - Check: I compiled `bacapp.c` with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at -O0, -Os and -O2. The old file referenced `__aeabi_fcmpun` and `__aeabi_dcmpun`. The new one references only `memcpy` and `memset`. With clang -Os, `.text` for `bacapp.o` went from 2460 to 2322 bytes.
2. **One shared tag-header writer and one minimal-length integer writer: implemented.**
   - `tag_header()` is the only code that forms a tag header. It covers application and context class, extended tag numbers (15..254), inline or extended length (254 / 255 forms), the Boolean value in L/V/T, and opening / closing tags. It replaces `hdr_len()` + `put_hdr()`, the hard-coded initial octets (`0x00`, `0x10|v`, `0x44`, `0xA4`, `0xB4`, `0xC4`) and the separate code in `enc_open_close()`. Called with `buf == NULL` it only measures, so length and bytes come from one piece of logic.
   - `put_min_int()` is the only minimal-length integer writer (unsigned, or two's complement). It replaces `uint_len()` + `sint_len()` and the duplicate `enc_uint()` / `enc_sint()`. All Unsigned / Signed / Enumerated encoders, application and context, go through `enc_int()` → `put_min_int()`.
   - `begin()` / `emit()` hold the single capacity check ("buffer untouched on error") and the single reserved-tag-255 check. Before, each context encoder had its own copy of that check.
   - Interpretation (please confirm): I read "all encoders share one minimal-length integer writer" as "every encoder that writes a minimal-length integer uses the same one". Fixed-width fields keep their fixed width, because clause 20.2 requires it: REAL (4), Double (8), Date / Time (4), Object Identifier (4), and the 2- and 4-octet extended-length fields. Encoding those minimally would change the output and break the standard.
- Verification: `make` builds cleanly with -Wall -Wextra (also clean with -pedantic -Wfloat-equal -Wdouble-promotion under gcc and clang). `run_vectors.py acceptance.vec tests/unit.vec` gives 377/377, also under ASan/UBSan. I compared the old and new builds command by command: about 170k randomized driver commands and a 15.9k-command boundary sweep, covering integer and tag boundaries, header length forms, capacities 0..cap+1 and NaN bit patterns. Every output matched, including the ERR / DIRTY / OVERRUN checks. `driver.c` and `run_vectors.py` are unchanged.
- Test changes: I appended vectors `cr102-r01..r07` and `cr102-d01..d07` to `tests/unit.vec`. They cover REAL / Double NaN and infinity boundaries, including NaNs whose only set fraction bits are in the high or low 32-bit word. I also added `cr102-b01..b02` for application Boolean true through the shared header writer. Every expected result was produced by the pre-CR-102 build.
- Unsure: the ~2 KB figure depends on the firmware lead's toolchain (GCC/newlib, link flags). I checked only this object with clang for Cortex-M0. The final image should be checked for any `__aeabi_f*` / `__aeabi_d*` pulled in by other modules.
