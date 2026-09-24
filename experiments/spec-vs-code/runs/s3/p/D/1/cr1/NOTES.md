## CR-101

Neither item conflicts with ASHRAE 135 or with SPEC.md, so both are implemented.
SPEC.md is now v1.1.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double` writes `55 08` and then the 8 octets of the IEEE-754 binary64
     value, big-endian and bit-exact. This is ASHRAE 135 clause 20.2.7, and `55 08` is
     the shortest length form under G4. It follows G1: when `cap < 10` or `buf` is NULL
     it returns -1 and leaves `buf` unchanged.
   - `bac_dec_app` now decodes tag 5 into `v.d` when the content length is exactly 8,
     and returns -1 for any other length. The header is parsed leniently like every
     other tag (D1b), so `55 fe 00 08 ...` is also accepted.
   - SPEC changes:
     - New rule E5b (Double encoding).
     - D2 no longer rejects tag 5. D2 is a [POL] rule, and this CR comes from the
       product owner, which is the sign-off that changing it needs.
     - D5 now covers both Real and Double.
   - Added `_Static_assert(sizeof(double) == 8)` so the build fails on a toolchain
     where `double` is 32-bit (for example avr-gcc defaults), instead of producing
     wrong frames.
   - **Unsure / decision to confirm: NaN.** The CR does not mention NaN. I extended
     E5a (decision D-7: "never as NaN on the wire") to Double, so `bac_enc_double`
     returns -1 for every NaN. ±Infinity and -0.0 are encoded normally. As with Real
     (D5), a received NaN is still decoded bit-exact. If the product owner really
     wants NaN sent for Double, that changes policy E5a/D-7 and needs an explicit
     sign-off.
   - Unsure: the octet order assumes `double` has the same byte order as
     `uint64_t`. `bac_enc_real` already assumes the same for `float`, and it holds on
     all targets we build for.

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented.**
   Both write a context-class tag with the given tag number. Enumerated content
   follows E3 and Signed content follows E4 (shortest two's complement). Tag 255 →
   -1 (G3), and G1 applies. They are documented as new rules E15 and E16.

Tests:
- `tests/unit.vec`: p0526 (`dec_app 55 08 00..00`) used to expect ERR because of the
  old "tag 5 unsupported" rule. It now expects `OK n=10 d 0000000000000000`.
- `tests/unit.vec`: added blocks `c101-*`, tagged CR101, covering encoding, NaN
  refusal, capacity/G1, decoding with bad lengths and truncation, and the context
  encoders including extended tags and tag 255.
- `acceptance.vec`: added `double-72` (the 72.0 example from the standard),
  `decode-double`, `context-enumerated` and `context-signed`.
- `driver.c` and `run_vectors.py` are unchanged. All 348 blocks pass.
