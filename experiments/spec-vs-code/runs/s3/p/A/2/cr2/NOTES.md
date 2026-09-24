## CR-101

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double in 8 big-endian
     octets (ASHRAE 135 clause 20.2.7; e.g. 72.0 -> `55 08 40 52 00 00 00 00 00 00`).
     It returns -1 without touching the buffer if the capacity is less than 10 or `buf` is NULL.
   - `bac_dec_app()` now accepts tag 5. Before this change tag 5 was explicitly rejected. It
     requires a content length of exactly 8, rejects truncated input, and stores the value in
     `v.d`. Like the other decoders, it accepts a non-minimal extended-length header
     (e.g. `55 FE 00 08 ...`).
   - Acceptance vectors added: `double-72`, `decode-double`, `double-bad-length`.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.** They reuse the same
   minimal-length unsigned/two's-complement content encoding as the application-tagged
   versions. Context tags >= 15 use the extended tag octet, and tag 255 is rejected
   (it is reserved), the same as `bac_enc_ctx_unsigned()`.
   Acceptance vectors added: `context-enumerated`, `context-signed`.

I found no conflict with the BACnet standard or the existing requirements in either item.

Unsure / assumptions:
- NaN: the CR does not say how to handle it. `bac_enc_double()` rejects NaN (returns -1)
  so that it matches the existing `bac_enc_real()` behaviour. Infinities and -0.0 are
  encoded. The decoder passes through any bit pattern, including NaN, just as the
  REAL decoder does. If product wants doubles to handle NaN differently, REAL needs to
  change too.
- The encoder assumes `double` is IEEE-754 binary64 with the same byte order as
  `uint64_t` (true on all supported targets).

## CR-102

1. **No floating point in `bacapp.c`: implemented.**
   - The only floating-point operations were the `isnan()` checks in `bac_enc_real()` and
     `bac_enc_double()`. On Cortex-M0 they compile to calls to `__aeabi_fcmpun` and
     `__aeabi_dcmpun`. I replaced them with integer tests on the bit pattern, which is
     copied with `memcpy` (exponent all ones and a non-zero fraction). The test gives the same
     result as `isnan()` for every bit pattern. NaN is still rejected (quiet or signalling,
     either sign), and infinities and -0.0 are still encoded. The decoders already used
     `memcpy`.
   - I removed `#include <math.h>` and added `_Static_assert`s that `float`/`double` are 4/8 bytes.
   - The `Makefile` no longer links `-lm`, because nothing uses libm now (`driver.c` does not use it either).
   - Checked with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at
     -O0, -O2 and -Os. The only undefined symbols are now `memcpy` and `memset`. Before the
     change, `__aeabi_fcmpun` and `__aeabi_dcmpun` were also undefined.
2. **One tag-header writer and one minimal-length integer writer: implemented.**
   - `put_tag()` is now the only code that writes a tag header. It handles:
     - application or context class
     - extended tag numbers (15..254)
     - extended lengths (the 1-, 3- and 5-octet forms)
     - an LVT that holds the value (NULL, application BOOLEAN)
     - opening/closing tags (LVT 6/7)

     It also checks the capacity (header plus content) before writing anything, and rejects
     the reserved context tag 255. Before, each encoder repeated these checks. `put_tag()`
     replaces `hdr_len()`/`put_hdr()` (which duplicated the length thresholds),
     `enc_open_close()`, and the hand-coded initial octets in the NULL, BOOLEAN, REAL, DOUBLE,
     DATE, TIME and OBJECT IDENTIFIER encoders.
   - `put_int()` is now the only minimal-length writer, for both unsigned and
     two's-complement content. It replaces `enc_uint()`, `enc_sint()`, `uint_len()` and
     `sint_len()`. UNSIGNED, ENUMERATED and SIGNED use it, and so do their context-tagged forms
     and the context-tagged BOOLEAN (one octet, 0 or 1, the same bytes as before).
   - Fixed-width contents do not use the minimal-length writer, on purpose: REAL (4 octets),
     DOUBLE (8), DATE, TIME and OBJECT IDENTIFIER (4). Clause 20.2 fixes their lengths, and
     dropping leading zero octets would produce invalid encodings (object 0,0 must be
     `c4 00 00 00 00`). They use the fixed-width `put_be()`, which does not repeat the
     minimal-length logic.
   - The decoders are unchanged.
3. **"External behavior must not change": met.**
   - All acceptance vectors pass.
   - I ran a differential test of 3789 commands against the pre-CR-102 build, and the
     output was byte-identical. It covered:
     - every encoder at its capacity boundaries
     - NaN/Inf/-0 bit patterns
     - all integer length boundaries
     - context tags 0..255
     - extended-length strings up to 70000 octets
     - the decoders

     The output was also identical with ASan/UBSan enabled.
   - I added 10 regression blocks to `acceptance.vec` (`real-nan-rejected` onwards). Their
     expected output was recorded from the pre-CR-102 build, and they pass on both builds.

I found no conflict with the BACnet standard or the documented requirements.

Unsure / assumptions:
- I measured code size only with clang 18 for thumbv6m, because no arm-none-eabi-gcc was
  available. At -Os, the `.text` of `bacapp.o` went from 2460 to 2316 bytes, and the
  soft-float routines no longer need to be linked. At -O2, clang inlines `put_tag()` into every
  encoder, and `.text` grows from 4016 to 4288 bytes. If the M0 build does not use -Os,
  building with -Os (or marking `put_tag()` noinline) is worth considering.
- The saving of about 2 KB is only real if nothing else in the image pulls in
  `__aeabi_fcmpun`/`__aeabi_dcmpun` or other soft-float routines. I did not check the rest
  of the firmware.
- `bacapp.h` still uses `float`/`double` in the API (parameters of `bac_enc_real()` and
  `bac_enc_double()`, and `bac_value_t.v.r`/`.d`). With the soft-float ABI they are only
  passed and stored, which needs no floating-point instructions. Changing the API would change
  external behavior, so I left it as it is.
- The NaN test assumes the IEEE-754 binary32/binary64 formats. CR-101 already assumed this.
