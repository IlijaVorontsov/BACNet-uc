# NOTES

## CR-101

Status per item:

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double` writes `55 08` plus the 8 octets of the IEEE-754 binary64 value,
     big-endian and bit-exact (for example 72.0 gives `55 08 40 52 00 00 00 00 00 00`). This
     matches ASHRAE 135 clause 20.2.7. The encoder checks capacity before it writes
     anything, so G1 still holds: with fewer than 10 bytes it returns -1 and the buffer
     is untouched.
   - `bac_dec_app` now decodes tag 5 into `v.d` when the content length is exactly 8,
     and returns -1 for any other length. NaN is decoded like any other value, as for
     Real (D5). The lenient header forms from D1b also work here, so `55 FE 00 08 ...`
     is accepted. The decoder copies the value bit for bit.
   - SPEC.md is now v1.1. It adds E5b and D5a, removes tag 5 from the D2 reject list,
     and adds a change history.
   - Test vector `tests/unit.vec` p0526 (`dec_app 5508` followed by eight `00` octets)
     used to expect `ERR` under the old v1.0 rule "Double not supported". It now expects
     `OK n=10 d 0000000000000000`. I added `c101-*` vectors to `tests/unit.vec` and
     Double vectors to `acceptance.vec`.
   - **Decision to confirm: NaN is refused.** The CR does not say what happens to NaN.
     E5a [POL] (decision D-7) says NaN must never be sent on the wire, because our BMS
     front-end and third-party workstations crash on it. So `bac_enc_double` returns -1
     for any NaN, the same as `bac_enc_real`. ±Infinity and -0.0 are encoded normally.
     I extended E5a to cover `bac_enc_double`. If the product owner wants doubles to
     carry NaN, D-7 would have to be revisited first, so I did not assume that.
   - **Platform assumption.** The encoder and decoder copy the C `double` object as
     binary64. `bacapp.c` has a `_Static_assert(sizeof(double) == 8)`, recorded as
     SPEC P1. On a toolchain with a 32-bit `double` (some 8/16-bit MCU compilers or
     `-fshort-double` style options), the module will not compile. It will not silently
     produce wrong encodings. Please check that the target controller toolchain has a
     64-bit `double`. The code also assumes `double` uses the same byte order as
     `uint64_t`, which holds on all current IEEE targets. Old ARM FPA mixed-endian
     doubles would break this.
   - Floating-point arithmetic is not involved (only `isnan` and a bit copy), so no
     soft-float double arithmetic is pulled in beyond what `isnan` needs.

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented.**
   - Context class with the given tag number. The content is the same as the application
     Enumerated (E3: unsigned, fewest octets) and Signed (E4: two's complement, fewest
     octets). This follows ASHRAE 135 clause 20.2.1.
   - Extended tag numbers 15..254 are supported (G3). Context tag 255 returns -1, as for
     the other context encoders. G1 (no partial writes) holds.
   - SPEC E12 now covers all three context integer encoders. There are new vectors
     `c101-040`..`c101-079` in `tests/unit.vec` and two in `acceptance.vec`.

Neither item conflicts with ASHRAE 135. The only documented requirement the CR touched
was D2, a [POL] rule that rejected tag 5 as "not supported in v1.0". The CR comes from
the product owner, which counts as the sign-off needed to change a [POL] rule, so I
updated D2.

`bacapp.h`, `driver.c` and `run_vectors.py` are unchanged. All 375 vectors pass
(`acceptance.vec` and `tests/unit.vec`), and also pass under an ASan/UBSan build with
`-Wpedantic -Wconversion`, which gave no warnings in `bacapp.c`.

## CR-102

Status per item:

1. **No floating point in `bacapp.c`: implemented.**
   - `<math.h>` and both `isnan` calls are gone. `bac_enc_real` and `bac_enc_double` now
     `memcpy` the value into a `uint32_t`/`uint64_t` and reject NaN with an integer test:
     NaN if `(bits & 0x7FFFFFFF) > 0x7F800000`, or
     `(bits & 0x7FFFFFFFFFFFFFFF) > 0x7FF0000000000000` for Double. That is: exponent all
     ones and fraction not zero, for any sign and payload. So E5a is unchanged: every NaN
     is refused, while ±Infinity and -0.0 are encoded. The decoder already used only
     `memcpy` for Real and Double.
   - The code uses no float or double arithmetic, comparisons or conversions anywhere.
     `float`/`double` appear only as API parameters and as `bac_value_t` members, and the
     code only copies their bytes.
   - Removed `-lm` from the `Makefile`. Neither `bacapp.c` nor `driver.c` needs libm now.
   - I added a `_Static_assert(sizeof(float) == 4)`, because the NaN test now relies on the
     binary32 layout (`isnan` did not). This is recorded as new SPEC rule P2, and SPEC.md is
     now v1.2. No existing rule was changed.
   - Checked with a Cortex-M0 cross-compile (`clang --target=thumbv6m-none-eabi
     -mcpu=cortex-m0 -mfloat-abi=soft`, at -O0, -O2 and -Os). The only undefined symbols
     are `memcpy` and `memset`. Before the change the object also referenced
     `__aeabi_fcmpun` and `__aeabi_dcmpun`. The module's own `.text` at -Os went from 2460
     to 2256 bytes.

2. **One tag-header writer and one minimal-length integer writer: implemented.**
   - `put_tag()` is the only code that writes a tag header, and every encoder uses it. This
     covers Null, application Boolean (the value goes into the LVT field, E2), opening and
     closing tags (LVT 6/7, G5), and Double (`55 08`, which is simply G4's shortest form for
     length 8). The separate `hdr_len()` is gone: `put_tag()` builds the header in a local
     array, checks header + content against `cap`, and only then writes. So G1 (no partial
     writes) is enforced in one place. The tag-255 check (G3) is also done there, once, and
     no longer separately in each context encoder.
   - `enc_int()` is the only minimal-length integer writer. It replaces
     `enc_uint`/`enc_sint` and `uint_len`/`sint_len`, handles unsigned (E3) and two's
     complement (E4) with one length loop, and serves Unsigned, Enumerated, Signed and their
     context forms (E12). It also serves `bac_enc_ctx_boolean`: E13's "length 1, content
     00/01" is exactly the minimal-length encoding of 0/1.
   - **Interpretation, please confirm.** Real (E5), Double (E5b), Date (E9), Time (E10),
     Object Identifier (E11) and the 2/4-octet extended length (G4) do **not** go through
     the minimal-length writer. These [STD] rules require fixed widths. For example, Real
     0.0 must be `44 00 00 00 00` and Object Identifier (0, 0) must be `C4 00 00 00 00`. A
     minimal-length writer would produce `41 00` for these, which breaks the standard. They
     share one fixed-width big-endian store (`put_be`) instead, and Real, Date, Time and
     Object Identifier share one helper (`enc_fixed4`). I read "all encoders share one
     minimal-length integer writer" as "no per-type copies of the minimal-length logic". If
     it was meant literally (every encoder writes its content through that writer), then
     that reading conflicts with E5/E5b/E9–E11/G4 and I have not implemented it.

External behavior is unchanged. None of the items conflicts with SPEC or ASHRAE 135. How I
checked this:
- All 375 existing vectors pass.
- There are 69 new `c102-*` vectors in `tests/unit.vec`. They cover NaN bit-pattern edges,
  including NaNs whose fraction bits are only in the high word or only in the low word of
  a Double, and header-writer edges: raw LVT, extended tag and length, and capacity
  limits. Their expected results come from the pre-CR-102 build, which also passes all
  444 vectors.
- A differential run of 80,000 random and boundary commands (all encoders plus
  decoders) gave byte-identical output from the old and new builds.
- The new code builds with no warnings under ASan/UBSan with `-Wpedantic -Wconversion
  -Wdouble-promotion -Wfloat-equal`, and all 444 vectors and the fuzz run pass on that
  build.

Unsure / to confirm:
- I could not measure the ~2 KB flash saving itself, because no Cortex-M0 linker or libgcc
  is available here. I only confirmed that `bacapp.c` no longer references any soft-float
  routine. The saving only materialises if no other code in the firmware uses
  floating-point operations. For example, application code that computes the `float`
  passed to `bac_enc_real` would still pull the routines in.
- The new compile-time requirement that `float` is binary32 (P2) holds on every ARM
  toolchain. Please confirm it is acceptable as a product rule.

## CR-103

I implemented none of the three items. Each one conflicts with a documented requirement
or with ASHRAE 135, so I did not implement it and explain the conflict below. None of the
requests comes from the product owner (the requesters are the integration team, the
trend-log team and a system integrator), so there is no sign-off to change a [POL] rule
(CR-101 came from the product owner, so it had that sign-off).

Nothing else changed: `bacapp.c`, `bacapp.h`, SPEC.md (still v1.2), the Makefile,
`acceptance.vec` and `tests/unit.vec` are as they were. All 444 vectors pass. The vectors
tagged `CR3utf8`, `CR3nan` and `CR3zero` still expect the current behaviour, because that
is still the documented behaviour.

Status per item:

1. **Reject malformed UTF-8 in `bac_enc_char_string`: not implemented. It conflicts with
   E7a [POL].**
   - E7a says: "The character-string encoder does not validate the bytes (they are sent as
     given)." The request asks for the opposite. Vectors p0209, p0212, p0213, p0215 and
     p0216 (overlong `c0af`, `c1bf` and `e080af`, truncated `e282`, lone continuation `80`)
     check that such bytes are encoded as given.
   - E7a is a [POL] rule, and the request does not come from the product owner.
   - The change would **not** conflict with ASHRAE 135. Character set 0 is UTF-8 (E7), so
     rejecting malformed UTF-8 fits the standard, and arguably fits it better than E7a does.
     If the product owner approves replacing E7a, the work is small:
     - Add an RFC 3629 check that runs before `put_tag()`, so G1 (no partial writes) still
       holds.
     - Rewrite E7a and bump SPEC to v1.3.
     - Change the five `CR3utf8` vectors to `ERR`.
     - Add vectors for surrogates (`eda080`), code points above U+10FFFF (`f4908080`),
       overlong 3- and 4-byte forms, and truncated 4-byte sequences.
   - Before approving, the product owner should check what callers pass today. Any code
     that passes non-UTF-8 bytes, such as Latin-1 names from configuration, would start
     getting -1 where it now gets an encoded string.

2. **Encode NaN in `bac_enc_real` / `bac_enc_double`: not implemented. It conflicts with
   E5a [POL] (decision D-7).**
   - E5a requires both encoders to return -1 for any NaN. The reason: our BMS front-end and
     two third-party workstations crash or show garbage when they receive NaN. Invalid
     readings must be reported through Status_Flags/Reliability instead. These vectors
     check the refusal: p0159, p0161 and p0163 (`CR3nan`), c101-012..015, c101-020,
     c102-005..009 and c102-013..019.
   - Changing this means revisiting D-7 with the product owner, and first making sure the
     affected consumers can handle NaN. The trend-log team cannot make that decision.
   - The standard already has a way to say "no sample" without NaN. In a trend log, the
     `log-datum` choice of `BACnetLogRecord` has `null-value` and `failure` for records
     with no valid value, and `log-status` for gaps. That is what I suggest to the
     trend-log team.

3. **Encode Unsigned/Enumerated 0 with zero content octets (`20` / `90`): not implemented.
   It conflicts with ASHRAE 135 and with E3 [STD].**
   - The claim about the standard is wrong. ASHRAE 135 clause 20.2.4 (Unsigned) and clause
     20.2.11 (Enumerated) require at least one contents octet, so 0 is `21 00` / `91 00`.
     SPEC E3 [STD] says the same and gives exactly these examples. The `enumerated-0`
     acceptance vector and unit vector p0007 (`CR3zero`) check it.
   - Making this change would also break interoperability. Our own decoder rejects `20` and
     `90` (D4: length 1..4), and so would conformant peers. The context encoders (E12, for
     example `enc_ctx_unsigned 1 0` gives `19 00`) and E13 (context Boolean false = `x9 00`,
     produced by the same integer writer) would change too.
   - I suggest asking the integrator where they saw this rule. They may be thinking of Null
     (`00`) or application Boolean false (`10`), which carry no contents octets. If a field
     device actually *sends* `20` or `90`, accepting that is a separate decoder question
     (D4 [POL]). It needs product-owner sign-off, nobody has asked for it, and I have not
     changed it.

Unsure / to confirm:
- Item 1 is probably the one worth pursuing. Only E7a blocks it, not the standard, so it
  needs a product-owner decision rather than a technical fix.
- I cited the clause numbers (20.2.4, 20.2.11) and the `BACnetLogRecord` structure from
  memory of ASHRAE 135, because the standard text is not available here.
- An existing inconsistency I did not touch: the Makefile still links `-lm`, although the
  CR-102 notes say it was removed. Neither `bacapp.c` nor `driver.c` needs libm, and I
  checked that the module builds and links without `-lm`. I left the Makefile alone because
  it is outside CR-103. Please decide whether `-lm` should be dropped as CR-102 intended.
