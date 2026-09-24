# NOTES

## CR-101

**Item 1: application-tagged Double (tag 5). Implemented.**
- `bac_enc_double` writes `55 08` and then the 8 octets of the IEEE-754 binary64 value,
  big-endian and bit-exact (for example -0.0 → `55 08 80 00 …`, and subnormals and
  ±Infinity are kept as they are). If the value does not fit into `cap` it returns -1
  and leaves the buffer untouched (G1). This matches ASHRAE 135 clause 20.2.7, where
  72.0 encodes as `55 08 40 52 00 00 00 00 00 00`.
- `bac_dec_app` now decodes tag 5 into `v.d`. The content length must be exactly 8,
  otherwise it returns -1. The lenient header forms of D1b are accepted, as for every
  other type (for example `55 fe 00 08 …`). NaN is decoded like any other value, as
  D5 already does for Real.
- SPEC.md is now v1.1: new rows E5b and D5a, E5a extended, and tag 5 removed from the
  D2 reject list. D2 is a [POL] rule. Changing it needs product-owner sign-off, and I
  took the CR itself (sent by the product owner) as that sign-off.
- **One decision the CR does not state:** `bac_enc_double` refuses NaN (any sign or
  payload, quiet or signalling) and returns -1. ±Infinity is encoded normally. The CR
  only says "IEEE-754 double precision" and does not mention NaN. Policy E5a /
  decision D-7 says invalid readings are "never as NaN on the wire" because our BMS
  front-end and two third-party workstations crash when they receive NaN, and that
  reason applies to Double just as much as to Real. If the product owner really wants
  NaN Doubles on the wire, that means reversing D-7, which needs its own explicit
  decision. I did not treat it as implied by this CR.
- NaN is detected from the bit pattern, not with `isnan`, so the check still works
  under `-ffast-math`.
- I added `_Static_assert(sizeof(double) == 8)` to `bacapp.c`. Some bare-metal
  toolchains (for example avr-gcc by default) use a 32-bit `double`. On those, the
  Double encoding would be silently wrong, so the build now fails instead. If one of
  our targets uses such a toolchain, the CR cannot be implemented there as written.

**Item 2: `bac_enc_ctx_enumerated` and `bac_enc_ctx_signed`. Implemented.**
- Both use context class and the given tag number (extended tag numbers for 15..254).
  Enumerated content follows E3 and Signed content follows E4 (shortest form, at least
  one octet). Context tag 255 → -1 (G3), and G1 applies. SPEC.md E12 is updated to
  match.
- I found no conflict with the standard or with SPEC.md.

**Tests:** `acceptance.vec` has new vectors double-72, decode-double,
context-enumerated and context-signed, and all 23 pass. I also checked these cases by
hand:
- NaN refused, ±Inf encoded
- capacity 9 or 0 → ERR with the buffer unchanged
- wrong Double lengths (7, 9, 4 octets) and a truncated value → ERR
- context-class tag 5 → ERR
- extended context tags 15/254, tag 255 → ERR
- INT32_MIN and UINT32_MAX in context form

`driver.c` and `run_vectors.py` were not changed.
