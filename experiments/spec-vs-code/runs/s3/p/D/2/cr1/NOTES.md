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
