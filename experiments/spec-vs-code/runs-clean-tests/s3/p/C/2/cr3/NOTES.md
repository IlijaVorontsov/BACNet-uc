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

## CR-103

Item 1 (`bac_enc_char_string` rejects text that is not well-formed UTF-8, RFC 3629): **implemented.**
- The new static function `utf8_well_formed()` accepts exactly the byte sequences in the RFC 3629 clause 4 syntax. It rejects stray continuation octets, the lead octets C0, C1 and F5..FF, truncated sequences (including a sequence cut off at the end of the text), overlong forms (E0 80..9F, F0 80..8F), surrogates (ED A0..BF, i.e. U+D800..U+DFFF, so CESU-8 pairs are rejected too) and code points above U+10FFFF (F4 90..BF).
- `bac_enc_char_string()` runs the check before it writes anything. For ill-formed text it returns -1 and leaves the buffer untouched (no `ERR DIRTY`). Valid text is still copied unchanged after the character-set octet X'00'.
- `bacapp.h`: the comment on `bac_enc_char_string` now states the rule.
- It uses integer operations only, so the CR-102 rule still holds. For Cortex-M0 soft-float (clang, -O0/-Os/-O2), the only undefined symbols are still `memcpy` and `memset`. The check adds about 200 bytes of .text at -Os.
- Checks:
  - Every string of 0 to 4 octets (4.36 x 10^9 strings) and 5 x 10^7 random strings of up to 23 octets, biased to boundary octets, were compared with an independent reference validator. That validator decodes the code point, then checks its minimum length, the U+10FFFF limit and the surrogate range. There were no differences, and accepted strings were checked byte for byte.
  - 300,000 random strings were run through `drv` and compared with Python's strict UTF-8 codec. There were no differences.
  - Both vector files pass with an ASan/UBSan build.
  - The temporary harnesses were deleted afterwards.

Item 2 (encode NaN in `bac_enc_real` and `bac_enc_double`): **not implemented. It conflicts with a documented product requirement.**
- Requirement E5a says the REAL encoder rejects NaN (vectors p0159, p0161, p0163 and c102-005..008). CR-101 extended the same rule to Double, and CR-102 kept both.
- The BACnet standard does not forbid NaN. This is a product rule, so it has to be changed in the requirements (E5a) before a field request can override it.
- I did not remove the Double check on its own either. The request covers both functions. Accepting NaN for Double but not for REAL would give the trend-log service inconsistent behavior, and it would break the rule CR-101 set up (Double follows E5a).
- The decoder already accepts NaN from other devices (D5).
- For "no sample", the trend-log service might use the standard's own means instead of NaN. As far as I recall, a BACnetLogRecord's log-datum has a `null-value` choice, and there are also log-status/failure entries. Someone should check this against the standard.
- If the requirements owner does change E5a: remove the two `is_nan32`/`is_nan64` checks, and change the E5a/Edbla vectors to expect the encoded bytes.

Item 3 (encode Unsigned/Enumerated 0 as `20` / `90` with no contents octets): **not implemented. It conflicts with the BACnet standard.**
- ASHRAE 135 clause 20.2.4 (Unsigned) and clause 20.2.11 (Enumerated) require a primitive encoding with at least one contents octet, in the fewest octets possible. For 0 that is one octet, X'00'.
- The standard's own example in clause 20.2.11 encodes Enumerated 0 (ANALOG-INPUT) as X'91' X'00'. That example is the `enumerated-0` block in `acceptance.vec`.
- The integrator's reading of the standard is wrong. The current output `21 00` / `91 00` is correct.
- If the change were made, conforming peers would reject the output, and so would our own decoder: `bac_dec_app` requires a length of 1..4, so `20` and `90` are rejected.
- The same applies to Signed 0 (`31 00`) and to the context-tagged forms (`09 00`).

Test artifacts:
- `tests/unit.vec`: p0209, p0212, p0213, p0215 and p0216 (tag E7a) encoded ill-formed UTF-8 (C0 AF, E2 82, 80, C1 BF, E0 80 AF) and expected `OK`. They now expect `ERR` and have the extra tag `CR103`.
- `tests/unit.vec`: added blocks `c103-*` (tag `CR103`):
  - 001..014: well-formed boundaries: empty, U+0000, U+007F, U+0080, U+07FF, U+0800, U+D7FF, U+E000, U+FFFF, U+10000, U+10FFFF, BOM, mixed text, and a 302-octet string ending in a 2-octet sequence.
  - 020..051: ill-formed input: stray continuations, missing continuation, overlong forms, surrogates, a CESU-8 pair, above U+10FFFF, F5..FF leads, 5-/6-octet forms, truncation in the middle and at the end, bad 3rd/4th octet, and an invalid octet after 300 and after 65000 valid octets.
  - 060..063: capacity combined with validation.
  - 070..075: regression vectors for the declined item 3: Unsigned 0 needs 2 octets, Enumerated 0 is `91 00` (application) and `59 00` (context), and `dec_app 20` / `dec_app 90` are rejected. NaN rejection was already covered by the E5a/Edbla vectors.
- `acceptance.vec`: added a non-ASCII UTF-8 character string ("Größe"), encoded and decoded.
- `make`, then `python3 run_vectors.py tests/unit.vec acceptance.vec`: 463/463 pass.

Things I am unsure about:
- **E7a:** before this CR, the E7a vectors required ill-formed UTF-8 to be passed through unchanged. I do not have the requirements text. I read E7a as "handling of ill-formed character-string input", which this CR changes from "pass through" to "reject". Rejecting also matches clause 20.2.9, where character set X'00' means the contents are UTF-8. If E7a is actually a written requirement that the encoder must not validate, item 1 conflicts with it, and the requirements owner has to update E7a to match this change.
- The decoder is unchanged. `bac_dec_app` still returns character-string contents without validating them (p0501, D7). The CR names only the encoder, and rejecting incoming strings from other devices would be a separate decision.
- These are well-formed under RFC 3629, so they are accepted: U+0000 inside the text, noncharacters (U+FFFE, U+FFFF, U+FDD0..U+FDEF) and a leading BOM (which is not stripped). If the product wants to restrict any of these, that needs its own requirement.
- The requirement tags on the new vectors (E7, E7a, E3, E12, G1, G4, D4) reuse existing IDs. D4 on `dec_app 20` / `dec_app 90` is my guess.
- Unrelated to this CR: `Makefile` still links `-lm`, although the CR-102 notes say it was removed. Nothing needs it and it does no harm. I left it unchanged because it is outside this CR's scope.
