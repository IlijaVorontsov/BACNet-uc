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

## CR-103

1. **`bac_enc_char_string()` rejects text that is not well-formed UTF-8: implemented.**
   - The new static `utf8_well_formed()` checks the text against the RFC 3629 section 4 grammar
     before anything is written. On failure the encoder returns -1 and leaves the buffer untouched.
     It rejects:
     - a continuation octet (80..BF) with no lead octet before it
     - the octets C0, C1 and F5..FF
     - a sequence that is truncated at the end of the text, or whose continuation octet is missing
     - overlong forms (C0/C1 leads, E0 80..9F, F0 80..8F)
     - surrogates U+D800..U+DFFF (ED A0..BF)
     - code points above U+10FFFF (F4 90..BF, F5..)
   - It accepts everything RFC 3629 calls well-formed, including the empty string, U+0000 (as one
     00 octet), noncharacters such as U+FFFE/U+FFFF, and a leading BOM.
   - No conflict: character set 0 (X'00'), the only one this encoder writes, is ISO 10646 UTF-8
     (clause 20.2.9).
   - Checked exhaustively against Python's strict UTF-8 decoder, with 0 mismatches:
     - all 16,843,008 strings of 1-3 octets
     - 12,845,056 4-octet strings (every first/second octet pair, with 14 boundary values for
       octets 3 and 4)
   - Vectors added: `char-string-utf8-valid`, `char-string-utf8-truncated-or-invalid`,
     `char-string-utf8-overlong`, `char-string-utf8-surrogates-and-above-10ffff`.
2. **`bac_enc_real()` / `bac_enc_double()` encode NaN: implemented.**
   - I removed the NaN checks and the `f32_bits_is_nan()`/`f64_bits_is_nan()` helpers. Both
     encoders now write the IEEE-754 bit pattern unchanged: quiet or signalling NaN, either sign,
     with the payload preserved (e.g. `enc_real 7fc00000` -> `44 7f c0 00 00`). REAL and DOUBLE
     still behave the same way, as CR-101 said they must.
   - No conflict: clauses 20.2.6/20.2.7 only require the IEEE-754 single/double format, which
     includes NaN. Rejecting NaN was never a documented requirement. CR-101 recorded it as an
     assumption made to match REAL, and CR-102 kept it only because that CR could not change behaviour.
   - I renamed the CR-102 blocks `real-nan-rejected` and `double-nan-rejected` to
     `real-nan-encoded` and `double-nan-encoded`, with the new expected output (ERR -> OK), and
     updated the header comment of `acceptance.vec` to explain this. I also added
     `nan-round-trip-and-capacity`.
   - The file still does no floating-point work. With clang 18 for thumbv6m at -O0/-O2/-Os, the only
     undefined symbols are `memcpy` and `memset`.
3. **Encoding Unsigned/Enumerated 0 as `20` / `90` (no content octets): NOT implemented, because it
   conflicts with the BACnet standard.**
   - The CR has the standard backwards. Clause 20.2.4 says an Unsigned value is encoded as a
     primitive "with at least one contents octet", and clauses 20.2.11 (Enumerated) and 20.2.5
     (Signed) say the same. The standard's own example in 20.2.11 encodes ENUMERATED 0 as X'91'
     X'00'. That example is the `enumerated-0` vector in `acceptance.vec`. So 0 is `21 00` / `91 00`,
     which is what the encoder already emits.
   - `20` / `90` is an Unsigned/Enumerated with a content length of 0, which is malformed.
     `bac_dec_app()` has always rejected it, and so do conforming peers. Making the change would mean
     sending frames that other devices, and this module's own decoder, reject.
   - The integrator may be thinking of NULL and the application-tagged BOOLEAN, which have no
     content octets (the value is in the LVT field).
   - I left the code unchanged. I documented the rule in the `put_int()` comment and in `bacapp.h`,
     and added the vector `unsigned-enumerated-zero-one-content-octet` (encoders emit `21 00`,
     `91 00`, `19 00`, `29 00`; the decoder rejects `20` and `90`) so this is not changed by mistake
     later. Someone should reply to the integrator. If a device in the field actually sends `20`/`90`,
     accepting it on decode would be a separate robustness CR.

Other changes:
- `Makefile`: I removed `-lm`. The CR-102 notes say the Makefile no longer links it, but the
  Makefile still did. Nothing uses libm, because neither `bacapp.c` nor `driver.c` includes
  `<math.h>`. If `-lm` was put back on purpose, it can be restored without harm.
- Verification:
  - All 40 acceptance vectors pass.
  - I ran a differential test of 9521 commands against the pre-CR-103 build. The only differences
    were 20 NaN encodings (ERR -> OK) and 1967 ill-formed strings (OK -> ERR). It covered:
    - all integer encoders at their length and capacity boundaries
    - context tags 0..255
    - NaN/Inf/-0 bit patterns
    - 3000 random strings
    - strings up to 70000 octets
  - No run produced ERR DIRTY, OVERRUN or NONDET. The output was identical with ASan/UBSan (gcc).
- Code size (clang 18, thumbv6m): `.text` of `bacapp.o` is 2316 -> 2464 bytes at -Os (+148) and
  4288 -> 4472 at -O2, which is the cost of the UTF-8 validator.

Unsure / assumptions:
- Callers that used to pass text that is not UTF-8 now get -1. Examples are Latin-1 text and
  strings cut to a fixed number of bytes in the middle of a character. Code that shortens a string to
  fit must now cut on a character boundary. The callers are not in this directory, so I did not
  check them.
- The decoder does not validate UTF-8, because the CR only asks for the encoder. `bac_dec_app()`
  still passes through whatever text arrives, in any character set. Rejecting it on decode would
  drop frames from peers that do not conform, so that should be a separate decision.
- NaN as "no sample": the encoder now sends NaN, but other BACnet devices will read it as a REAL
  value, not as a missing sample. BACnetLogRecord (clause 21) has choices meant for missing data:
  `log-status`, `null-value` and `failure`. For records exposed over BACnet (Log_Buffer,
  ReadRange), the trend-log team should check with the product owner which of these to use.
- Signalling NaN is preserved on Cortex-M0 (soft-float passes the value in integer registers) and
  on x86-64. On targets that pass `float`/`double` through the x87 FPU (32-bit x86), the value may
  be quieted before it reaches the encoder (7f800001 -> 7fc00001). This is outside the encoder.
- The clause numbers and wording above are from my knowledge of ASHRAE 135 clause 20.2. I had no
  copy of the standard here to check them against.
