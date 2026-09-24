## CR-101

Spec bumped to v1.1 (SPEC.md). All vectors pass (`make && python3 run_vectors.py
acceptance.vec tests/unit.vec`: 364/364; also clean under ASan/UBSan and
`-Wpedantic -Wconversion`).

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double` writes `55 08` followed by the 8-octet IEEE-754 binary64 value,
     big-endian and bit-exact (for example, 72.0 gives `55 08 40 52 00 00 00 00 00 00`,
     which is the ASHRAE 135 clause 20.2.7 example). This matches the standard, so there
     is no conflict. It needs cap >= 10. Otherwise it returns -1 and leaves the buffer
     untouched (G1). The new spec rule is E15.
   - `bac_dec_app` now decodes tag 5 when the content length is exactly 8, into `v.d`.
     Any other length gives -1. NaN is decoded like any other value, as D5 does for Real.
     Lenient headers such as `55 fe 00 08 ...` are accepted, as D1b allows. The new rule
     is D5a. Rule D2 no longer lists tag 5. The product owner raised this CR, so this
     counts as the sign-off needed to change that [POL] rule.
   - Vector p0526 (`dec_app 5508` + 8 zero octets) used to expect `ERR` under the old D2
     rule. It now expects `OK n=10 d 0000000000000000`. Tests c101-001..033 in
     tests/unit.vec and the standard's Double example in acceptance.vec are new.
   - **Please confirm: NaN.** The CR does not say how to handle NaN for Double. The
     E5a/D-7 policy says NaN must never be sent on the wire, because receivers crash.
     So `bac_enc_double` refuses every NaN (returns -1) and still encodes ±Infinity. This
     is documented as new rule E15a, pending product-owner confirmation. If NaN should be
     allowed for Double, the change is the `isnan` check in `bac_enc_double`, rule E15a
     and vectors c101-011..015.
   - **Please confirm: build check.** I added a `_Static_assert` in bacapp.c that
     requires `double` to be IEEE-754 binary64 (8 bytes, 53-bit mantissa). Without it, a
     toolchain where `double` is 32-bit (for example avr-gcc's default) would silently
     produce wrong encodings. On such a target the build now fails instead. Please check
     that this is right for all our targets. It also assumes that `double` and
     `uint64_t` use the same byte order, the same assumption the Real code already makes
     for `float` and `uint32_t`.

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented.**
   - Both use context class with the given tag number. The content is the same as the
     application form: the E3 minimal unsigned form for Enumerated and the E4 minimal
     two's-complement form for Signed. Extended tag numbers 15..254 work (G3), context
     tag 255 returns -1, and nothing is partially written (G1). There is no conflict with
     the standard or the spec. The new spec rules are E16 and E17. Tests c101-034..062
     are new.

No item conflicted with SPEC.md or ASHRAE 135, so nothing was refused. `bacapp.h`,
`driver.c` and `run_vectors.py` are unchanged.

## CR-102

Both items are implemented. Neither conflicts with SPEC.md or ASHRAE 135, so nothing was
refused. External behavior is unchanged: no spec rule changed and no existing vector
changed. `make && python3 run_vectors.py acceptance.vec tests/unit.vec` gives 383/383
(the 364 existing vectors plus 19 new ones, c102-001..019).

1. **No floating-point arithmetic, comparisons or `<math.h>`: implemented.**
   - The only floating-point operations were the two `isnan()` calls, which are
     floating-point compares. On the Cortex-M0 they pulled in `__aeabi_fcmpun` and
     `__aeabi_dcmpun`. Both calls are replaced by a test on the bit pattern, which is
     copied out with `memcpy`. A value is NaN when its exponent is all ones and its
     fraction is non-zero: `(bits & 0x7FFFFFFF) > 0x7F800000` for Real (E5a), and the
     64-bit equivalent for Double (E15a). This matches exactly the values `isnan` caught
     (quiet or signalling, any sign or payload). ±Infinity is still encoded.
   - `#include <math.h>` is gone. `-lm` is removed from the Makefile, because nothing
     needs libm any more (`driver.c` does not use it either).
   - `<float.h>` is still included, but only for the integer constants in the
     `_Static_assert`s, which are evaluated at compile time and generate no code.
   - `put_be64` is removed. Double is now written as two 32-bit halves through the
     shared `put_be`, which also removes the `__aeabi_llsr` helper (a 64-bit shift) on
     the M0. The decoders already used `memcpy` and are unchanged apart from
     `sizeof` in place of the literal sizes.
   - How I checked it: I compiled with `clang --target=thumbv6m-none-eabi
     -mcpu=cortex-m0 -mfloat-abi=soft` at -O0, -O2 and -Os. `bacapp.o` now imports only
     `memcpy` and `memset`. Before the change it also imported `__aeabi_fcmpun`,
     `__aeabi_dcmpun` and `__aeabi_llsr`. At -Os, `.text` went from 2460 to 2312
     bytes. The x86-64 object contains no floating-point compare or arithmetic
     instructions.

2. **One shared tag-header writer and one minimal-length integer writer: implemented.**
   - `put_hdr()` is now the only code that writes a tag header (G2–G5). Every encoder
     uses it. That includes the encoders that used to hard-code their header octets
     (Null `00`, Boolean `10`/`11`, Real `44`, Double `55 08`, Date `A4`, Time `B4`,
     Object Identifier `C4`) and the opening/closing-tag encoder, which had its own copy
     of the extended-tag logic. The old separate `hdr_len()` is merged into `put_hdr()`:
     called with `buf == NULL`, it only returns the header size. The G3/G4 logic now
     exists in one place.
   - `begin()` now does the G1 fit check (nothing is written unless the whole value
     fits) and the context-tag-255 check (G3) for all encoders. Before, those checks
     were repeated in each encoder.
   - `put_min_int()` is the only minimal-length integer writer. It handles the E3
     unsigned form and the E4 two's-complement form, and it replaces
     `uint_len`/`sint_len` and the duplicate `enc_uint`/`enc_sint`. All six integer
     encoders (Unsigned, Enumerated and Signed, each in application and context form)
     use it through `enc_int()`. Fixed-width fields (Real, Double, Object Identifier and
     the extended-length octets) use `put_be`. They are fixed-width by definition, so
     they are not candidates for the minimal-length writer.

**Verification that behavior did not change.** I built the pre-CR-102 driver and fed the
same 73,842 commands to both drivers. The commands were random and boundary-value cases
for every encoder (with and without a capacity limit, all context tags 0..255 at small
capacities, NaN and Infinity bit patterns, and the integer length boundaries at 2^n±2)
plus random decoder inputs. The output was byte-identical, including the driver's
dirty-buffer (G1) and overrun checks. The same was true for the new code built with
ASan/UBSan. The new code is also warning-free with `-Wpedantic -Wconversion
-Wsign-conversion -Wfloat-equal -Wdouble-promotion` and clang `-Weverything`. The 19 new
vectors (c102-*) pass against both the old and the new build. They pin the NaN boundary
(for example, `ff800001` and `7ff0000100000000` are refused, while `ff800000` and
`ffefffffffffffff` are encoded), opening/closing tags with extended tag numbers, and the
integer length boundaries.

SPEC.md: the module description now lists the no-floating-point constraint, and a
revision-history note records CR-102. No rule changed, so the version stays at v1.1.
`bacapp.h`, `driver.c` and `run_vectors.py` are unchanged.

**Unsure / please confirm:**
- **New build check for `float`.** I added a `_Static_assert` that requires `float` to
  be IEEE-754 binary32. The bit-pattern NaN test depends on that layout, and `isnan` did
  not. It passes on every target I know of, including arm-none-eabi and x86. This is the
  same caveat as the `double` check from CR-101.
- **The 2 KB saving.** I could not verify it: arm-none-eabi-gcc is not installed here,
  and I did not link a firmware image. What I did verify is that `bacapp.o` no longer
  references any soft-float routine. If other modules use `float` or `double`
  arithmetic, the soft-float library will still be linked.
- **The `float`/`double` API parameters stay.** `bac_enc_real`/`bac_enc_double` and
  `bac_value_t` still take `float`/`double` (changing them would change the API). Under
  the soft-float ABI these values are passed and copied as plain integers, so this needs
  no floating-point routines.
- **Stale reference in the CR-101 notes.** They mention "the `isnan` check in
  `bac_enc_double`". That check is now the bit-pattern test in the same function.

## CR-103

None of the three items is implemented. Each one conflicts with SPEC.md, and item 3
also conflicts with ASHRAE 135. None of the requesters is the product owner, so there is
no sign-off to change a [POL] rule. `bacapp.c`, `bacapp.h`, SPEC.md and the vectors are
unchanged. The spec stays at v1.1, and `make && python3 run_vectors.py acceptance.vec
tests/unit.vec` still gives 383/383.

1. **Reject malformed UTF-8 in `bac_enc_char_string` (integration team): not
   implemented. It conflicts with E7a [POL].**
   - E7a says: "The character-string encoder does not validate the bytes (they are sent
     as given)." This request reverses that rule. A [POL] rule needs product-owner
     sign-off to change, and the integration team cannot give it.
   - The standard does not decide this either way. It defines charset 0 as UTF-8, but it
     does not require the encoder to validate what the caller passes.
   - These vectors pin the current behavior (the bytes are passed through): p0209 (`c0af`),
     p0212 (`e282`), p0213 (`80`), p0215 (`c1bf`) and p0216 (`e080af`).
   - If the product owner approves, the change is small. Validate the text in
     `bac_enc_char_string` before `begin()`, so nothing is written on failure (G1). Then
     rewrite E7a and change those five vectors to `ERR`. Two questions for the product
     owner: should U+0000 be accepted (RFC 3629 allows it)? Should the decoder (D7) stay
     lenient? D7 reports the bytes as-is, and a decoder change was not requested.

2. **Encode NaN in `bac_enc_real` / `bac_enc_double` (trend-log team): not implemented.
   It conflicts with E5a and E15a [POL] (decision D-7).**
   - Our BMS front-end and two third-party workstations crash or show garbage when they
     receive NaN. Under D-7, invalid readings are reported through
     Status_Flags/Reliability, never as NaN on the wire.
   - E15a (Double) is still marked "pending product-owner confirmation". A request from
     the trend-log team is not that confirmation. E5a (Real) is an established rule.
   - These vectors pin the refusal: p0159, p0161, p0163, c101-011..015, c102-004..007 and
     c102-010..013.
   - Suggestion for the trend-log team: a BACnet log record can show a missing sample
     without NaN, for example with the log-datum `null-value` or `failure` choice, or with
     Status_Flags. Decoding already accepts NaN from other devices (D5, D5a).

3. **Encode Unsigned/Enumerated 0 with no content octets (`20` / `90`) (system
   integrator): not implemented. It conflicts with ASHRAE 135 and E3 [STD].**
   - The request says the standard requires this, but the standard requires the
     opposite. The content is the value in the minimum number of octets, and at least one
     octet. So 0 is `21 00` / `91 00`, which is what we send.
   - E3 records exactly this, and so do vectors p0007 (`2100`) and acceptance
     `enumerated-0` (`9100`).
   - `20` or `90` is a zero-length Unsigned/Enumerated. That is not a valid encoding. Our
     own decoder rejects it (D4: `dec_app 20` and `dec_app 90` give `ERR`), and
     conformant receivers are entitled to reject it too.
   - The context forms (E12, E16, for example `09 00`) are correct for the same reason.
     The integrator's tool or reading of the standard should be checked instead.

**Unsure / please confirm:**
- For item 3 I used E3 [STD] and clause 20.2.4 (Unsigned) / 20.2.11 (Enumerated) as I
  know them. I did not have the standard text here to quote.
- **Unrelated discrepancy.** The CR-102 notes say `-lm` was removed from the Makefile,
  but the Makefile in this directory still links with `-lm`. It is harmless, because
  nothing in `bacapp.c` or `driver.c` uses libm. I left it unchanged because it is outside
  this CR. It can be removed to match the CR-102 notes.
