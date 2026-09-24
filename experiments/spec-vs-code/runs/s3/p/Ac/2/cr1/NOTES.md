# NOTES

## CR-101

Neither item conflicts with ASHRAE 135 or with the documented [POL] rules, so
both are implemented in `bacapp.c`. `bacapp.h` and `driver.c` were already updated
and I did not change them. `make` builds cleanly, and `acceptance.vec` passes
23/23. That count includes 4 new CR-101 blocks: `double-72`, `decode-double`,
`context-enumerated` and `context-signed`.

### Item 1: application-tagged Double (tag 5): IMPLEMENTED
- `bac_enc_double()` writes `55 08` and then the 8 IEEE-754 octets, big-endian. The
  bit pattern is copied without conversion. This matches ASHRAE 135 clause
  20.2.7: tag 5, length 8, extended-length header. For example, 72.0 encodes as
  `55 08 40 52 00 00 00 00 00 00`. G1 also applies: if cap < 10, the function
  returns -1 and writes nothing.
- `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is
  exactly 8, and returns -1 for any other length. I changed rule D2, which rejected
  tag 5 in v1.0, and added rule D10. The CR from the product owner is the
  sign-off for that D2 [POL] change. Because of D1b, the decoder also accepts a
  non-canonical length header such as `55 fe 00 08`. It decodes NaN as-is, the
  same way D5 handles Real.
- **Unsure / needs product-owner confirmation (E15a):** the CR says nothing about
  NaN. I applied the Real NaN policy (E5a) to Double as well, so
  `bac_enc_double()` returns -1 for any NaN and still encodes +/-Infinity. The
  E5a rationale (decision D-7: "never as NaN on the wire") covers the problem
  of front-ends that crash on NaN, and that problem does not depend on precision.
  If Double should be allowed to send NaN, remove the `isnan()` check in
  `bac_enc_double()`.
- I added a C11 `_Static_assert(sizeof(double) == 8)`. The encoder and decoder
  copy the bits of a `double` directly, which is only correct when `double` is
  IEEE-754 binary64. On targets where `double` is 32-bit, the build now fails
  instead of producing wrong bytes on the wire.

### Item 2: `bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: IMPLEMENTED
- These work like `bac_enc_ctx_unsigned()`. The header is context class. The
  content is minimal big-endian unsigned (E3) for Enumerated and minimal
  two's-complement (E4) for Signed. Tags 15..254 use the extended tag form.
  Tag 255 returns -1 (G3). G1 applies: nothing is written on error. The new
  rule ids are E16 and E17.

### Open points
- `SPEC.md` is referenced by `bacapp.c` but is not in this directory, so I could
  not update it. It needs the following changes to match the code:
  - D2 no longer rejects tag 5.
  - New rules E15 (Double), E15a (NaN refused, [POL]), E16 (context
    Enumerated), E17 (context Signed) and D10 (Double decode: length exactly 8,
    bit-exact, NaN accepted, [POL]).

  These rule ids are my proposal, and the comments in `bacapp.c` say so.
- The new union member `double d` (from the updated `bacapp.h`) can change the
  alignment of `bac_value_t` on 32-bit targets, and its size through padding.
  For example, on ARM EABI the union goes from 4-byte to 8-byte alignment. This
  only affects callers that make assumptions about the struct's size or layout.
