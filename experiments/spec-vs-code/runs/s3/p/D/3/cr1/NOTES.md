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
