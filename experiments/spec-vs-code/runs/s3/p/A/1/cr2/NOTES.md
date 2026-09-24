## CR-101

1. Application-tagged Double (tag 5): **implemented**.
   - `bac_enc_double()` writes `55 08` followed by the 8 octets of the IEEE-754
     double, most significant octet first (ASHRAE 135 clause 20.2.7; the
     standard's example 72.0 -> `55 08 40 52 00 00 00 00 00 00` is now in
     `acceptance.vec`). It needs 10 bytes of capacity and writes nothing on error.
   - `bac_dec_app()` now accepts tag 5 and requires a content length of exactly 8;
     the value is stored in `v.d`. Any other length is rejected. As with the other
     types, a non-minimal extended length (e.g. `55 fe 00 08 ...`) is accepted.
   - Uncertain: the CR does not say how NaN should be handled. For consistency with
     the existing `bac_enc_real()`, `bac_enc_double()` rejects NaN (returns -1).
     Infinities and negative zero are encoded. The decoder, like the REAL decoder,
     does not check the bit pattern, so a received NaN is still decoded.
2. `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()`: **implemented**.
   They mirror `bac_enc_ctx_unsigned()`: the same minimal-length content
   encoding as the application-tagged Enumerated and Signed encoders, context
   class, extended tag numbers for tags 15-254, and tag 255 (reserved) is rejected.

Neither item conflicts with the BACnet standard, so both were implemented.
`bacapp.h` and `driver.c` were already updated and were not changed.
New vectors were added to `acceptance.vec` (double-72, decode-double,
context-enumerated, context-signed). All 23 acceptance blocks pass.

## CR-102

Neither item conflicts with the BACnet standard or the documented behavior, so
both were implemented. External behavior is unchanged.

1. No floating-point arithmetic, floating-point comparisons or `<math.h>`: **implemented**.
   - `#include <math.h>` and both `isnan()` calls are gone. `bac_enc_real()` and
     `bac_enc_double()` copy the value into a `uint32_t`/`uint64_t` with `memcpy`
     and reject NaN by its bit pattern (exponent all ones and a non-zero fraction:
     `(bits & 0x7FFFFFFF) > 0x7F800000`, and the same test on 64 bits for Double).
     As before, NaN is rejected (quiet, signalling, either sign), while infinities
     and negative zero are encoded. The decoder already used only `memcpy`.
   - `-lm` was removed from the `Makefile` because nothing needs libm now.
   - Two `_Static_assert`s require a 4-byte `float` and an 8-byte `double`. The
     `memcpy` calls depend on those sizes, and a toolchain with a 32-bit `double`
     now fails at compile time instead of reading past the variable.
   - Checked by compiling `bacapp.c` for `thumbv6m-none-eabi` (Cortex-M0,
     `-mfloat-abi=soft`) with clang. The old file called `__aeabi_fcmpun` and
     `__aeabi_dcmpun` from `isnan`. The new file calls no `__aeabi_f*`/`__aeabi_d*`
     routine, only `memcpy`/`memset`. The module's own `.text` at `-Os` went from
     2460 to 2304 bytes. At `-O2` it grew slightly (4016 -> 4044) because the
     shared helper is inlined.
2. One shared tag-header writer and one minimal-length integer writer: **implemented**.
   - `build_hdr()` is now the only code that builds a tag header: tag number
     (extended tag number octet for 15-254), class, and length (0-4 inline, or the
     extended length forms 5-253 / 254+2 octets / 255+4 octets). `begin()` wraps
     it. It checks capacity for the header plus contents, rejects a NULL buffer and
     the reserved tag number 255, and writes the header only when everything fits,
     so a failed call still leaves the buffer unchanged. Every encoder now goes
     through `begin()`. This includes Null, Boolean (the value is written in the
     LVT field), REAL, Double, Date, Time, Object Identifier, and the opening and
     closing tags (LVT 6/7). None of them hard-codes header octets any more. The
     separate `hdr_len()` and the header code inside `enc_open_close()` were
     removed, and so were the four per-encoder `tag == 255` checks.
   - `enc_int()` is the only minimal-length integer writer. It replaces
     `uint_len`/`sint_len`/`enc_uint`/`enc_sint` and serves Unsigned, Enumerated and
     Signed, both application and context tagged. Signed values keep their sign bit,
     so -128 is 1 octet, -129 is 2 octets and 128 is 2 octets.
   - Interpretation: "all encoders share one minimal-length integer writer" applies
     to the encoders whose contents the standard requires to be minimal length
     (Unsigned, Enumerated, Signed). REAL, Double, Object Identifier, Date and Time
     have fixed-length contents, and the extended length field uses fixed 1/2/4-octet
     forms. Encoding those at minimal length would break the standard (e.g.
     Object Identifier 0,0 must stay `c4 00 00 00 00`). They use the plain
     fixed-width big-endian `put_be()` instead.

Verification: an old-versus-new differential run of the test driver over about
460,000 commands produced identical output. The commands covered every encoder,
integer boundaries at each octet width, random and NaN/Inf REAL and Double bit
patterns, all 256 tag numbers, the extended-length boundaries and capacities
0-13, 20, 260 and 70000. The same corpus also passed under ASan/UBSan.
`acceptance.vec` gained 11 regression blocks: NaN rejection, Inf and -0, Boolean
FALSE, context Boolean, extended and reserved tag numbers, minimal-length
integer boundaries, extended lengths, and capacity errors. All 34 blocks pass
with both the old and the new build. `driver.c` and `run_vectors.py` were not
changed.

Unsure:
- NaN detection now assumes IEEE-754 binary32/binary64 bit layouts, with `double`
  in the same byte order as `uint64_t`. This holds for Cortex-M0 with GCC/Clang
  (EABI) and for the host build. Legacy ARM FPA mixed-endian doubles are not
  supported (the decoder already made the same assumption).
- Flash savings were measured with clang only. The final link on the real
  arm-none-eabi-gcc toolchain was not tested here.
