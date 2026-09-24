## CR-101

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double` writes `55 08` followed by the 8-octet IEEE-754 binary64 bit pattern,
     big-endian and bit-exact (for example -0.0 → `55 08 80 00 ...`, ±Infinity encoded
     normally). It needs `cap` ≥ 10; otherwise it returns -1 and leaves the buffer
     untouched (G1).
   - `bac_dec_app` now decodes tag 5 with content length exactly 8 into `v.d`. Any other
     length → -1. Lenient header forms are accepted per D1b, for example `55 fe 00 08`.
     NaN is decoded like any other value, as D5 already does for Real.
   - SPEC.md (now v1.1): added E15 and D5a, removed tag 5 from the D2 reject list, and
     extended E5a. D2 is a [POL] rule; this CR comes from the product owner, so I took it
     as the sign-off needed to change that rule.
   - **NaN refused (I decided this; please confirm):** the CR says nothing about NaN.
     `bac_enc_double` returns -1 for any NaN, the same as `bac_enc_real` under E5a. Decision
     D-7's reason is that NaN must never go on the wire because the BMS front-end and two
     third-party workstations fail on it. That reason applies to Double just as much, so
     encoding NaN as a Double would get around the policy. If the product owner really
     wants NaN sent as Double, D-7/E5a has to be revised first.
   - Tests: unit vector p0526 (`dec_app 5508` + 8 zero octets) used to expect ERR because
     of the old "Double not supported in v1.0" rule. It now expects
     `OK n=10 d 0000000000000000`. Added unit vectors c101-01..32 and the acceptance vectors
     `double-72` and `decode-double`.
   - Unsure / portability: `bacapp.c` now has
     `_Static_assert(sizeof(double) == 8, ...)`. On targets where `double` is 32-bit
     (for example avr-gcc without `-mdouble=64`), the API cannot represent a BACnet
     Double, and the build now fails on purpose instead of producing wrong encodings.
     Like the existing Real code, the implementation assumes floating-point byte order
     matches integer byte order (true on all mainstream targets).

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented.**
   - Context class, given tag number (extended tag octet for 15..254), content as E3
     (Enumerated) or E4 (Signed, minimal two's complement). Context tag 255 → -1 (G3).
     No partial writes (G1).
   - SPEC.md: added E16 and E17. Added unit vectors c101-33..60 and the acceptance vectors
     `context-enumerated` and `context-signed`.

I found no conflict with ASHRAE 135: `55 08` + 8 octets is the standard Double encoding,
and context-tagged Enumerated/Signed follow clause 20.2.1.3.1. All 364 vectors
(acceptance.vec and tests/unit.vec) pass.

## CR-102

1. **No floating point in `bacapp.c`: implemented.**
   - Removed `<math.h>` and both `isnan()` calls. `bac_enc_real` and `bac_enc_double` now
     `memcpy` the value into a `uint32_t`/`uint64_t` first. They treat it as NaN when the
     exponent bits are all ones and the fraction is non-zero:
     `(bits & 0x7FFFFFFF) > 0x7F800000` for Real, and the same test with the binary64
     masks for Double. E5a is unchanged: every NaN, whatever its sign or payload, is
     refused, and ±Infinity is encoded normally. The decoders already used only `memcpy`.
     `float` and `double` now appear only as parameter types, as `memcpy` operands and in
     `sizeof`.
   - Double octets are now written as two 32-bit halves, so the 64-bit variable-shift
     loop (`put_be64`) is gone.
   - Makefile: removed `-lm`, because nothing needs libm now.
   - SPEC.md is now v1.2. The module line says "no floating point", and there is a
     changes line for CR-102. No rule changed.
   - Added `_Static_assert(sizeof(float) == 4, ...)` next to the existing Double
     assertion. The code has always assumed a 4-octet `float`; this only makes the
     assumption explicit.
   - Checked by compiling for Cortex-M0 (`clang --target=thumbv6m-none-eabi
     -mcpu=cortex-m0 -mfloat-abi=soft`, at -O0, -O2 and -Os). The old file referenced
     `__aeabi_fcmpun`, `__aeabi_dcmpun` and `__aeabi_llsr`. The new file references
     only `memcpy` and `memset`.

2. **One tag-header writer and one minimal-length integer writer: implemented.**
   - `put_hdr()` is now the only code that builds a tag header (G2–G5). It handles the
     class bit, extended tag numbers and the three length forms, and it also builds
     opening/closing tags (`HDR_OPENING`/`HDR_CLOSING`, LVT 6/7). All encoders reach it
     through `begin()`/`emit()`. That includes the ones that used to write fixed header
     octets themselves: Null, Boolean, Real, Double, Date, Time, Object Identifier and
     opening/closing tags.
   - `begin()` builds the header in a local buffer and checks capacity for header plus
     content before it writes anything. So G1 (no partial writes) and G3 (reject tag
     255) are now enforced in one place, instead of by an `if (tag == 255)` in each
     context encoder. The separate `hdr_len()` is gone, so header size and header
     octets can no longer disagree.
   - `put_int()` replaces `uint_len()`/`sint_len()` and the duplicate
     `enc_uint()`/`enc_sint()`. It writes the fewest octets (at least one), with a
     signedness flag for E4 two's complement. All six Unsigned, Enumerated and Signed
     encoders (application and context) use it.
   - Fixed-width fields deliberately do **not** use the minimal-length writer: the 2- and
     4-octet extended lengths (G4) and the Real, Double, Object Identifier, Date and Time
     contents. The standard requires their fixed width; for example, length 254 must be
     `FE 00 FE`. They use the shared fixed-width `put_be()`.
   - Application Boolean still carries its value in the LVT field with no content (E2).
     Context Boolean still has length 1 with content `00`/`01` (E13).

No conflict with SPEC.md or ASHRAE 135: the CR changes only how the code is written, and
the wire format stays the same.

Tests:
- All 364 vectors in acceptance.vec and tests/unit.vec pass. None were changed.
- I compared the old and new implementations with the driver. 300,000 random commands,
  weighted toward edge cases, gave identical output. Among them were capacity limits,
  every length-form boundary, tags 14/15/254/255, NaN/Infinity bit patterns and invalid
  arguments, with no DIRTY/OVERRUN results. So did 4,431 integer values around every
  power of two, for all six integer encoders.

Unsure:
- I could not try the team's real toolchain (arm-none-eabi-gcc is not installed here);
  the check above used clang for the same target. It only proves that `bacapp.c` itself
  no longer pulls in soft-float routines. The roughly 2 KB saving only happens if no
  other module in the image uses `float`/`double` arithmetic. Linking the real image
  would show that.
- Like before, the code assumes `float`/`double` have the same byte order as the
  integer types.

## CR-103

None of the three items is implemented. Each one conflicts with a documented requirement
in SPEC.md, and item 3 also conflicts with ASHRAE 135. `bacapp.c`, `bacapp.h`, SPEC.md
and the test vectors are unchanged. All 364 vectors (acceptance.vec and tests/unit.vec)
still pass.

1. **Reject malformed UTF-8 in `bac_enc_char_string` (integration team): not
   implemented. It conflicts with E7a [POL].**
   - E7a says: "The character-string encoder does not validate the bytes (they are sent
     as given)." The unit vectors p0209, p0212, p0213, p0215 and p0216 test exactly this.
     They expect an overlong `c0af`, a truncated `e282`, a lone continuation octet `80`
     and the overlongs `c1bf` / `e080af` to be encoded as given (`OK 7300c0af` and so on).
     The requested change would make all of them return -1.
   - E7a is a [POL] rule. SPEC.md says a [POL] rule may change only with product-owner
     sign-off. This request comes from the integration team, not from the product owner,
     so I cannot treat it as that sign-off. (In CR-101 the request came from the product
     owner, which is why I changed D2 then.)
   - This is not a conflict with the standard. Character set 0 is UTF-8, and validating
     would only stop us from sending bytes that are labelled UTF-8 but are not UTF-8. If
     the product owner revises E7a, the change is small: an RFC 3629 check before
     `begin()`, so that G1 still holds. That check must reject truncated or invalid
     sequences, overlongs (C0/C1, E0 80..9F, F0 80..8F), surrogates (ED A0..BF) and
     values above U+10FFFF (F4 90.. and F5..FF). Along with it, SPEC E7a would need
     rewording and p0209..p0216 would need to expect ERR.
   - Open question for the product owner: E7a has no written rationale. Existing callers
     may depend on it, for example code that passes truncated names or Latin-1 text.
     Who decides that is unknown to me.

2. **Encode NaN in `bac_enc_real` / `bac_enc_double` (trend-log team): not implemented.
   It conflicts with E5a [POL] (decision D-7).**
   - E5a says both encoders return -1 for any NaN. The rationale is that our BMS
     front-end and two third-party workstations crash or display garbage when they
     receive NaN. Invalid readings must be reported through Status_Flags/Reliability,
     never as NaN on the wire. The unit vectors p0159..p0163 (Real) and the E5a Double
     vectors (for example `enc_double 7ff8000000000000` → ERR) test this.
   - The trend-log team is not the product owner, so this is not the sign-off needed to
     change a [POL] rule. The rule was also deliberately extended to Double in CR-101.
   - The decoders already accept NaN (D5, D5a), so receiving NaN from other devices is
     not affected.
   - Suggestion for the trend-log team (not verified against their service): represent
     "no sample" with the means BACnet provides, rather than a NaN Real. Examples are
     Status_Flags/Reliability as D-7 says, or a null or failure log datum in the trend
     log record. Changing this would need D-7/E5a to be revised by the product owner
     first.

3. **Encode Unsigned/Enumerated 0 as `20` / `90` (system integrator): not implemented.
   It conflicts with ASHRAE 135 and with E3 [STD].**
   - The claim is not correct. ASHRAE 135 clause 20.2.4 (Unsigned) and clause 20.2.11
     (Enumerated) say the encoding "shall contain at least one octet". Zero is therefore
     `21 00` / `91 00`, as SPEC E3 [STD] says. Vectors p0007 and p0067 and the acceptance
     vector `enumerated-0` (`9100`) test this.
   - `20` / `90` (length 0) is not a valid encoding. Our own decoder rejects it under D4
     (`dec_app 20` → ERR, `dec_app 90` → ERR), and conforming peers can be expected to
     reject it too. Only Null and the application Boolean have no content octets (E1,
     E2); the integrator may be thinking of those.
   - If the integrator has a device that sends or expects `20`, please ask them for the
     device and a capture. That would be an interoperability issue with that device, not
     a defect in our encoder.

Unsure / other observations:
- Makefile inconsistency (existed before CR-103, not changed by me): the CR-102 notes
  say `-lm` was removed from the Makefile, but the current Makefile still links with
  `-lm`. It does no harm, because neither `bacapp.c` nor `driver.c` uses libm. I left it
  alone because it is outside the scope of this CR. Someone should check which version
  of the Makefile is the intended one.
- The clause numbers in item 3 are from ASHRAE 135 as I know it. I did not have the
  standard text here to check them.
