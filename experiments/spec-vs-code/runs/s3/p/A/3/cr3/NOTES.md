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

## CR-103

1. `bac_enc_char_string()` rejects text that is not well-formed UTF-8: **implemented.**
   - The new `utf8_well_formed()` checks the text against RFC 3629 clause 4,
     which gives the same result as Unicode Table 3-7. It runs before anything
     is written. The function returns -1 and leaves the buffer untouched for
     each of these:
     - stray continuation octets
     - the lead octets C0, C1 and F5..FF
     - truncated sequences, both at the end of the text and cut short by a
       non-continuation octet
     - overlong forms: E0 followed by 80..9F, and F0 followed by 80..8F
     - surrogates: ED followed by A0..BF
     - code points above U+10FFFF: F4 followed by 90..BF
   - Well-formed text is encoded exactly as before. U+0000 and noncharacters
     such as U+FFFE are accepted, because RFC 3629 allows them.
   - I updated the `bacapp.h` comment and added the vectors `utf8-well-formed`
     and `utf8-ill-formed`.
   - How this was checked: the validator gave the same result as Python's
     strict UTF-8 decoder on 22,621,184 inputs. These were every 1-, 2- and
     3-octet string, every 4-octet string with lead F0..F4 and all other octets
     in 70..C7, and 4-octet strings built from 21 boundary octets.
2. `bac_enc_real()` and `bac_enc_double()` encode NaN: **implemented.**
   - This does not conflict with the standard. Clauses 20.2.6 and 20.2.7 define
     REAL and Double as IEEE-754 binary32 and binary64, and they do not exclude
     NaN. It does not conflict with a documented requirement either. Rejecting
     NaN was a choice made in CR-101, and it was recorded as waiting for
     product-owner confirmation.
   - Both encoders now copy every bit pattern unchanged. That includes NaN of
     either sign, quiet or signalling, with its payload. `nan32()` and
     `nan64()` are removed. A buffer that is too small still returns -1 and
     nothing is written.
   - `bac_dec_app()` already passed NaN through, so NaN now survives a round
     trip.
   - Vector changes:
     - `real-nan-boundaries` and `double-nan-boundaries` now expect OK instead
       of ERR, as the CR-102 note said they would.
     - `nan-round-trip` is new.
     - `buffer-too-small` has NaN cases added.
   - The CR-102 rule still holds: the file has no floating-point operations.
     The Cortex-M0 build (clang, soft-float, -Os and -O0) references only
     `memcpy` and `memset`.
3. Unsigned or Enumerated 0 encoded as `20` / `90`: **not implemented, because
   it conflicts with the BACnet standard.**
   - Clause 20.2.4 says: "The encoding of an unsigned integer value shall be
     primitive, with at least one contents octet." Clause 20.2.11 says the same
     for Enumerated. So 0 must be encoded as `21 00` (Unsigned) and `91 00`
     (Enumerated), and as `X9 00` when context-tagged. The current encoder
     already does this.
   - `20` and `90` have zero content octets, so they are not valid encodings.
     This decoder rejects them, and other conforming implementations may too.
     The integrator may be thinking of Null (`00`) or the application Boolean
     (`10`/`11`), which are the only values with no content octets.
   - No behavior change. I added a comment to `enc_int()` and the vector
     `unsigned-enumerated-zero`. It fixes the output at `2100`, `9100` and
     `3900` and checks that `20` and `90` are rejected on decode.

Verification:
- `acceptance.vec` passes 33/33.
- A differential test ran 200,000 random commands against the pre-CR-103
  build. It covered every encoder and both decoders, including small buffers.
  The only differences were the expected ones: ill-formed UTF-8 now gives ERR,
  and NaN now gives OK with the unchanged bit pattern. No run reported
  ERR DIRTY, OVERRUN or NONDET.

Open points:
- Item 2 settles the NaN question left open in CR-101 and CR-102. I assumed the
  product owner accepted this CR item, but the CR does not say so.
- "NaN means no sample" is the trend-log service's own convention. BACnet
  already has standard ways to record a missing sample: the log-status,
  failure and null-value choices of BACnetLogRecord. Other devices will see only
  a REAL or Double NaN, and some of them may not handle NaN well. The
  trend-log team should confirm that this is acceptable on the wire.
- This module keeps signalling-NaN payloads exactly. A caller on an FPU or x87
  target could still quiet a signalling NaN before it reaches the encoder. The
  Cortex-M0 soft-float ABI does not do this.
- The decoder does not check UTF-8. The CR asked only for the encoder, so
  `bac_dec_app()` still returns character-string content unchanged. If received
  text must be checked too, that needs its own CR, which should also say what
  happens to character sets other than UTF-8.
- On Cortex-M0 (clang, -Os, stub libc headers), `bacapp.o` text grew from 2334
  to 2482 bytes because of the UTF-8 check.
- This is outside CR-103: the Makefile still links `-lm`, although the CR-102
  notes say it was removed. It is harmless because nothing uses libm, and I
  did not change it.
