## CR-101

**Item 1: application-tagged Double (tag 5). Implemented.**
- `bac_enc_double` writes `55 08` followed by the 8 octets of the IEEE-754 double, most significant octet first. This follows ASHRAE 135 clause 20.2.7 (its example, 72.0, encodes as `55 08 40 52 00 00 00 00 00 00`). The capacity check runs before any write, so the buffer is untouched on error.
- `bac_dec_app` now decodes tag 5 into `v.d` and requires a content length of exactly 8; any other length is rejected. Encoded forms that the other types already accept also work for Double, i.e. an extended tag number (`f5 05 08 ...`) or a non-minimal extended length (D1b).
- The CR did not say what to do with NaN. `bac_enc_double` rejects NaN (returns -1) because `bac_enc_real` does the same (E5a). Both infinities are accepted, as for REAL. The decoder accepts any 64-bit pattern, including NaN, as the REAL decoder does. **Please confirm the NaN policy.**
- I added `_Static_assert(sizeof(double) == 8)` (and one for `float`). The build now fails on a toolchain where `double` is 32 bits (for example, some AVR compilers). I don't know whether any product target uses such a toolchain.
- The conversion assumes the host stores floating-point values with the same byte order as integers. The existing REAL code makes the same assumption.
- Vectors: `tests/unit.vec` p0526 (`dec_app 5508 00...00`) used to expect ERR, because the D2 rule treated tag 5 as unsupported. This CR changes that behaviour, so the test now expects `OK n=10 d 0000000000000000`. The written requirements (the documents behind the E*/D*/G* IDs) are not in this directory, so I could not update them. **D2 needs to be updated to say that Double is supported.**

**Item 2: `bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`. Implemented.**
- They work like `bac_enc_ctx_unsigned`: context class, extended tag number for tags 15 to 254, context tag 255 rejected (reserved), and the minimum number of content octets (unsigned for Enumerated, two's complement for Signed, clauses 20.2.5 / 20.2.11). The buffer is untouched on error.

**Conflicts:** I found none. Both items match the BACnet standard and the behaviour documented in this directory, apart from the D2 change noted above, which is intended.

**Tests:** I added blocks p0541 to p0604 to `tests/unit.vec` (tagged `CR101`, plus G1/G3/D1b/D2/DV where they apply) and 4 blocks to `acceptance.vec`: the clause 20.2.7 Double example, a Double decode, and one context Enumerated and one context Signed encoding. All 368 blocks pass.

## CR-102

**Item 1: no floating-point arithmetic, floating-point comparisons or `<math.h>`. Implemented.**
- `bacapp.c` no longer includes `<math.h>`. The only floating-point operations were the two `isnan()` calls. They made the Cortex-M0 object depend on `__aeabi_fcmpun` and `__aeabi_dcmpun`.
- NaN is now detected from the bit pattern, which is read with `memcpy`. For REAL, `(bits & 0x7FFFFFFF) > 0x7F800000`; for Double, `(bits & 0x7FFF...F) > 0x7FF0...0`. In both cases that means all exponent bits are set and the fraction is non-zero. The NaN policy is unchanged: NaN is rejected, and both infinities, -0 and subnormals are accepted (E5/E5a, CR-101). Decoders were already copying bits with `memcpy`. They are unchanged.
- The `Makefile` no longer links `-lm`.
- Verification: I cross-compiled `bacapp.c` with `clang --target=thumbv6m-none-eabi -mcpu=cortex-m0 -mfloat-abi=soft`. Before the change it had undefined references to `__aeabi_fcmpun` and `__aeabi_dcmpun`. Now its only undefined symbols are `memcpy` and `memset`, so no soft-float helper is pulled in. The 64-bit mask, compare and shift in `bac_enc_double` compile to plain integer instructions. The bit test gives the same result as `isnan()` for all 2^32 float patterns and for the double patterns around the NaN/Inf boundary.
- The public API still passes `float`/`double` values (`bac_enc_real`, `bac_enc_double`, `bac_value_t.v.r`/`.v.d`), because external behaviour must not change. With the soft-float ABI these values travel in core registers and are only copied, so they need no runtime support.
- Unsure: I could not link against the real M0 toolchain or libc, so I did not measure the ~2 KB saving myself. Code size of `bacapp.o` alone (clang, thumbv6m): with `-Os` it went from 2460 to 2400 bytes of text. With `-O2` it went from 4016 to 4212, because clang inlines the shared writers into each encoder. I don't know which optimisation level the product build uses. As before, the code assumes the target stores floats with the same byte order as integers.

**Item 2: one shared tag-header writer and one shared minimal-length integer writer. Implemented.**
- `put_hdr()` is now the only code that builds a tag header (clause 20.2.1). It handles the class bit, extended tag numbers, the LVT field, extended lengths (1/3/5 octets) and opening/closing tags. With `buf == NULL` it returns only the header length, which replaces the separate `hdr_len()`.
- All encoders now use `put_hdr()`. Before, Null, Boolean, REAL, Double, Date, Time, Object Identifier and opening/closing tags each hard-coded their own header octets. `begin()` is the one shared prologue: it rejects the reserved tag 255, checks capacity before anything is written (so the buffer is untouched on error) and writes the header.
- `put_int()` is now the only minimal-length integer writer. It covers Unsigned and Enumerated as unsigned values and Signed as two's complement, for both application and context tags. It replaces `uint_len`/`sint_len`/`enc_uint`/`enc_sint`. `put_be()` is the one big-endian octet writer used for fixed-width content and extended lengths, and it replaces `put_be64`.
- How I read the item: "all encoders share one minimal-length integer writer" means every encoder that produces a minimal-length integer uses `put_int()`. It does not mean that every encoder must produce minimal-length integers. REAL (20.2.6), Double (20.2.7), Date (20.2.12), Time (20.2.13) and Object Identifier (20.2.14) have fixed 4- or 8-octet contents. The extended-length field must use exactly the 1/3/5-octet forms of 20.2.1.3.1. Sending any of these through the minimal-length writer would break the standard, so they still use fixed-width writes. The application Boolean keeps its value in the LVT field (20.2.3). It calls `put_hdr()` directly because it has no content octets.

**Conflicts:** None found. Neither item conflicts with the BACnet standard or with the documented behaviour, given the reading of item 2 above.

**External behaviour / tests:**
- All vectors pass: `acceptance.vec` plus `tests/unit.vec`, 387 blocks.
- I also ran a differential test against a build of the pre-CR-102 `bacapp.c`: about 230,000 random encoder/decoder commands covering all capacities, extended tags and lengths, NaN/Inf patterns and invalid arguments. All outputs were identical, and the driver never reported DIRTY/OVERRUN/NONDET.
- I added blocks p0605 to p0623 (tag `CR102`) to `tests/unit.vec`. They cover NaN boundary bit patterns for REAL and Double, context tag 255 on opening tags, and capacity edge cases of the shared header writer. The expected results were produced by the pre-change build.
- `driver.c`, `run_vectors.py`, `bacapp.h` and `acceptance.vec` are unchanged.
