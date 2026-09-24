## CR-101

1. Application-tagged Double (tag 5): **implemented.**
   - `bac_enc_double()` writes `55 08` followed by the 8 octets of the IEEE-754
     double in big-endian order (10 bytes in total). It returns -1 when the
     buffer is NULL or smaller than 10 bytes, without writing anything.
   - `bac_dec_app()` now decodes tag 5 into `v.d`. The content length must be
     exactly 8, otherwise it returns -1. A truncated buffer also returns -1.
     The decoder no longer rejects tag 5 outright.
   - This matches ASHRAE 135 clause 20.2.7. The standard's example (72.0 ->
     `55 08 40 52 00 00 00 00 00 00`) was added to `acceptance.vec`, along with
     a decode round trip.
2. `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()`: **implemented.**
   - They work like `bac_enc_ctx_unsigned()`: the context class bit is set, the
     content uses the minimal number of octets (unsigned for Enumerated, two's
     complement for Signed), tags 15-254 use the extended tag-number octet, and
     tag 255 (reserved) is rejected. Vectors were added to `acceptance.vec`.

No item conflicts with the BACnet standard, so nothing was refused.

Open question:
- NaN handling for Double. `bac_enc_real()` rejects NaN, so `bac_enc_double()`
  rejects NaN too, for consistency: it returns -1 and writes nothing. Infinities
  and -0.0 are encoded as normal, as they are for Real. The CR does not mention
  NaN, so the product owner should confirm this. On decode, NaN bit patterns are
  passed through unchanged, as `bac_dec_app()` already does for Real.

## CR-102

1. No floating-point arithmetic, comparisons or `<math.h>`: **implemented.**
   - The only floating-point operations were the two `isnan()` calls in
     `bac_enc_real()` and `bac_enc_double()`. They are replaced by tests on the
     IEEE-754 bit pattern, which is copied with `memcpy`. A value is NaN when its
     exponent bits are all ones and its fraction is not zero. For Double the test
     uses the two 32-bit halves, so it needs no 64-bit arithmetic.
   - `<math.h>` is no longer included, and `-lm` was removed from the Makefile.
     `_Static_assert`s check that `float` is 4 bytes and `double` is 8 bytes.
   - The public API still takes `float`/`double` parameters, because changing
     it would change external behavior. With the soft-float ABI these values
     are passed in core registers, so they need no support routines.
   - How this was checked:
     - Compiled `bacapp.c` for Cortex-M0 with clang (`armv6m-none-eabi`,
       `-mfloat-abi=soft`, at both `-Os` and `-O0`). Before the change the
       object referenced `__aeabi_fcmpun` and `__aeabi_dcmpun`. After it, the
       only undefined symbols are `memcpy` and `memset`. The object's text went
       from 2460 to 2334 bytes. That figure does not include the soft-float
       library, which is no longer linked.
     - The new NaN test gives the same answer as `isnan()` for all 2^32 float
       bit patterns and for 2*10^8 double bit patterns aimed at the boundaries.
2. One shared tag-header writer and one minimal-length integer writer:
   **implemented.**
   - `put_tag()` is the only code that writes a tag header (clause 20.2.1). It
     covers extended tag numbers, extended lengths, the Boolean value in the
     LVT field, and opening/closing tags. With `buf == NULL` it only measures
     the header, which replaces the old `hdr_len()` copy of that logic.
     `begin_value()` wraps it: it checks capacity (so nothing is written on
     error) and is the single place that rejects the reserved tag 255.
   - No encoder writes header octets by hand any more. The removed ones are
     Null `00`, Boolean `1x`, Real `44`, Double `55 08`, Date `A4`, Time `B4`,
     Object ID `C4`, the context Boolean, and the opening/closing tags
     (`enc_open_close()` is gone).
   - `enc_int()` is the only minimal-length integer writer. It replaces
     `enc_uint()`, `enc_sint()`, `uint_len()` and `sint_len()`. It serves
     Unsigned, Enumerated and Signed, both application and context tagged.
   - How I read "one minimal-length integer writer": it covers the integer
     encoders only, and it keeps signed and unsigned rules apart.
     - It takes an `is_signed` flag. BACnet uses the minimal two's-complement
       length for Signed (clause 20.2.5), so 128 must be `32 00 80`. The
       unsigned rule would give `31 80`, which decodes as -128.
     - REAL, DOUBLE, DATE, TIME and OBJECT ID must keep their fixed content
       lengths of 4 or 8 octets (clauses 20.2.6, 20.2.7, 20.2.12 to 20.2.14),
       so they do not use it. Their four-octet contents share `enc_fixed4()`.
     - If the CR meant that every encoder must use the minimal-length writer,
       that would conflict with the standard. I did not do that.
- External behavior is unchanged.
  - A differential test ran 27,706 driver commands against a build of the
    pre-CR-102 source, and the output was byte-identical. The commands covered
    every encoder, including error and short-buffer cases (ERR/DIRTY/OVERRUN
    reporting), all tag numbers 0-255, extended-length boundaries, and random
    input to the decoders.
  - I added 6 regression blocks to `acceptance.vec`: NaN/Inf/-0 boundaries,
    minimal-length boundaries, extended tag and length headers, tag 255, and
    buffers that are too small. Their expected output comes from the
    pre-CR-102 build. `acceptance.vec` passes 29/29.

No item conflicts with the documented requirements or the BACnet standard, so
nothing was refused.

Open points:
- The size change was measured on `bacapp.o` alone, with clang and stub libc
  headers. No arm-none-eabi GCC/newlib is available here, so the real firmware
  link was not checked. Other modules may still pull in soft-float.
- The NaN rejection from CR-101 is still waiting for product-owner
  confirmation. It is kept exactly as it was. The new `real-nan-boundaries` and
  `double-nan-boundaries` vectors lock in the current behavior, so update them
  if that decision changes.
- The bit-pattern code assumes IEEE-754 floats that use the same byte order as
  integers. The previous `memcpy`-based code already assumed this, and it holds
  on Cortex-M0 (EABI) and x86. Only the sizes are checked at compile time.
