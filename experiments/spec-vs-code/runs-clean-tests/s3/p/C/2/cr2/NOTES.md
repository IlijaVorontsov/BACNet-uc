## CR-101

Item 1 (application-tagged Double, tag 5): **implemented.**
- `bac_enc_double()` writes `55 08` and then the 8 IEEE-754 binary64 octets, most significant first (10 bytes in all). It returns -1 without writing anything if `cap < 10` or `buf` is NULL. This matches the ASHRAE 135 clause 20.2.7 example (72.0 -> `55 08 40 52 00 00 00 00 00 00`).
- `bac_dec_app()` now decodes tag 5 into `v.d` and accepts only a content length of exactly 8. A non-minimal extended length (for example `55 FE 00 08 ...`) is accepted, the same as for the other types (D1b).
- A `_Static_assert` checks that `double` is 8 bytes.

Item 2 (`bac_enc_ctx_enumerated`, `bac_enc_ctx_signed`): **implemented.**
- They use the same minimal-length content encoding as `bac_enc_enumerated` and `bac_enc_signed`, with a context-class header built the same way as in `bac_enc_ctx_unsigned`. Extended tag numbers 15..254 are supported, and tag 255 is rejected (G3).

I found no conflict with the documented requirements or with the BACnet standard in either item.

Test artifacts:
- `tests/unit.vec`: p0526 (`dec_app 5508 00..00`) expected ERR because Double was not supported before. Tag-5 decoding is now required, so it expects `OK n=10 d 0000000000000000` and its tag changed from D2 to DV. p0528 and p0529 (tag 5 with length 4) still expect ERR.
- `tests/unit.vec`: added blocks `c101-*` (tag `CR101`) for Double encoding, rejection, capacity and decoding, and for context Enumerated/Signed (values, tag numbers 15+ and 255, capacity).
- `acceptance.vec`: added the clause 20.2.7 Double example, a double decode, and one context Enumerated and one context Signed example.
- `make`, then `python3 run_vectors.py tests/unit.vec acceptance.vec`: 356/356 pass. It also passes when built with ASan/UBSan.

Things I am unsure about:
- **NaN:** `bac_enc_double` rejects NaN (returns -1), the same way `bac_enc_real` does (E5a). The CR does not say how NaN should be handled; I followed the existing product rule for REAL. The decoder accepts any bit pattern, NaN included, as it does for REAL (D5). If NaN should be encodable as a Double, the check can be removed.
- The new test tags `Edbl`, `Edbla`, `Ddbl` and `Ectx` are placeholders. I did not have the requirements list, so they are not official requirement IDs and should be mapped to real IDs when the requirements document is updated.

## CR-102

Item 1 (no floating-point arithmetic, floating-point comparisons or math library): **implemented.**
- `#include <math.h>` is removed. The two `isnan()` calls were the only floating-point operations. On an FPU-less target they turned into soft-float compare calls (`__aeabi_fcmpun`, `__aeabi_dcmpun`).
- `bac_enc_real` and `bac_enc_double` now `memcpy` the argument into a `uint32_t`/`uint64_t` first. NaN is then detected with an integer test on the bit pattern (exponent all ones and fraction non-zero: `(bits & 0x7FFFFFFF) > 0x7F800000`, and the same for binary64). NaN is still rejected (E5a, and the CR-101 rule for Double). Infinities, -0 and subnormals are still encoded unchanged.
- The decoder already used only `memcpy` for REAL and Double. A `_Static_assert` now checks that `float` is 4 bytes, next to the existing one for `double`.
- `Makefile`: `-lm` removed, because nothing needs the math library now.
- Check: I compiled `bacapp.c` with clang for `thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft` at -O0/-O1/-O2/-Os/-O3. The only undefined symbols left are `memcpy` and `memset`, and there are no `__aeabi_f*`/`__aeabi_d*` imports. The original file imported `__aeabi_fcmpun` and `__aeabi_dcmpun`. The .text size at -Os went from 2460 to 2308 bytes, not counting the soft-float library that no longer gets linked.

Item 2 (one shared tag-header writer and one shared minimal-length integer writer): **implemented.**
- `put_tag()` is now the only code that builds tag octets. It handles application and context class, extended tag numbers 15..254, the rejection of tag 255 (G3), L/V/T as a length (including the 254/255 extended-length forms), as a value (application Boolean), and opening/closing tags. It also does the capacity check for header plus contents, and writes nothing on failure. It replaces `hdr_len()` and `put_hdr()`, which were two copies of the same decisions. It also replaces the hard-coded header octets in Null, Boolean, REAL, Double, Date, Time and Object Identifier, the separate header logic in `enc_open_close()`, and the per-function `tag == 255` checks.
- `enc_int()` is now the only minimal-length integer writer. It replaces `uint_len`/`sint_len` and `enc_uint`/`enc_sint`, and handles unsigned and two's-complement signed values with one rule: drop leading octets that are only zero or sign extension. Unsigned, Enumerated and Signed use it in both application and context class. So does context Boolean, whose single `00`/`01` contents octet is exactly the minimal encoding of 0/1.
- Object Identifier, Date, Time, REAL and Double still write fixed-length contents, as the standard requires (OID is always 4 octets, clause 20.2.14). They use the shared header writer but not the minimal-length writer. I read "all encoders share one minimal-length integer writer" as "every encoder that writes a minimal-length integer uses the same one". Shortening fixed-length contents would violate the standard.

"External behavior must not change": checked as follows.
- `tests/unit.vec` and `acceptance.vec` pass, with a normal build and with ASan/UBSan.
- A differential test compared a build of the original `bacapp.c` with the new one:
  - Every REAL bit pattern (all 2^32), every Unsigned and Signed value (all 2^32, application class), and every value for context Signed/Enumerated, with the tag number varying.
  - All 256 tag numbers × capacities 0..16 for context Boolean, opening/closing tags and context Unsigned.
  - Structured and 2×10^8 random Double bit patterns.
  - 105,000 random driver commands covering every encoder and decoder, including capacity limits and extended lengths.
- There were no differences in return value or output bytes, and no DIRTY/OVERRUN results. The temporary test harness and reference build were deleted afterwards.

I found no conflict with the documented requirements or with the BACnet standard in either item.

Test artifacts:
- `tests/unit.vec`: added blocks `c102-001`..`c102-049` (tag `CR102`). Their expected outputs were produced by the pre-CR-102 build, so they are regression tests. They cover:
  - NaN detection on bit patterns: largest finite values, -inf, -0, subnormals, and NaNs whose fraction bits are only in the high or only in the low 32-bit half of a Double.
  - The length boundaries of the minimal-length writer for Unsigned and Signed.
  - The header writer: exact capacities, tag 255, extended tag numbers, open/close, and the 254 extended-length form.
- No existing vectors were changed. `acceptance.vec` is unchanged.

Things I am unsure about:
- The public API still has `float`/`double` in `bac_enc_real`, `bac_enc_double` and `bac_value_t`. The CR asks for no external behavior change, so I left them. Under the soft-float EABI these are passed and stored in core registers or memory without any library call, which the object check above confirms. However, application code that builds the `float`/`double` values will still pull in soft-float routines if it does arithmetic on them. That is outside `bacapp.c`.
- Reading a `double` through `memcpy` into a `uint64_t` assumes that `double` has the same byte order as `uint64_t`. This is true for AAPCS/EABI on Cortex-M, but not for the old ARM FPA mixed-endian format. The pre-CR code made the same assumption, both in the encoder and in the decoder.
