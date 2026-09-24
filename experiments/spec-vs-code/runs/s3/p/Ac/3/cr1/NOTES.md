# Notes

## CR-101

Neither item conflicts with ASHRAE 135 or with the documented requirements.
The only rule that changes is D2 [POL], which rejected tag 5 ("Double - not
supported in v1.0"). The product owner raised this CR, which is the sign-off
the module header asks for before any [POL] rule changes.

### Item 1: application-tagged Double (tag 5): implemented

- `bac_enc_double()` writes `55 08` and then the IEEE-754 double as 8 bytes,
  most significant byte first. The bits are copied unchanged, so -0.0,
  subnormals and infinities keep their exact bit pattern. This matches
  ASHRAE 135 clause 20.2.7; its example 72.0 encodes as
  `55 08 40 52 00 00 00 00 00 00`, and that is now an acceptance vector.
  - G1 (no partial writes) holds: the encoder checks the full 10 bytes against
    `cap` before it writes anything.
  - **Assumption, E15a [POL]: NaN is refused (-1), the same as for Real
    (E5a).** The CR does not mention NaN. Decision D-7 says invalid readings
    must never go on the wire as NaN, because our front-end and two
    third-party workstations cannot handle it. Encoding a NaN double would
    break that requirement. +/-Infinity is still encoded, as for Real.
    Please confirm. If NaN doubles should be allowed, remove the `isnan()`
    check in `bac_enc_double()`.
- `bac_dec_app()` now decodes tag 5 into `v.d`, with the same byte order and
  unchanged bits (rule D10).
  - The content length must be exactly 8; anything else returns -1.
  - Any of the extended length forms is accepted, e.g. `55 fe 00 08 ...`. The
    decoder already accepts these for other types (D1b).
  - NaN is decoded as it is, the same as for Real (D5).
- D2 amended: tags 13, 14 and >= 15 are still rejected, but tag 5 no longer
  is.
- A compile-time check (`_Static_assert`) makes the build fail on a target
  where `double` is not 64 bits, such as some 8-bit toolchains. Without it,
  such a target would silently produce wrong encodings.

### Item 2: `bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented

- Both work like `bac_enc_ctx_unsigned`: context class, extended tag number
  for tags 15..254, and -1 for tag 255 (G3). They also follow G1.
- Enumerated content is the shortest big-endian unsigned (E3). Signed content
  is the shortest two's-complement form (E4). The code reuses the existing
  `enc_uint()` and `enc_sint()` helpers. Rules E16 and E17.

### Other changes

- In `bacapp.c`, the module header, the rule-to-code map and the comments at
  each change now cite the new rules (E15, E15a, E16, E17, D10) and the change
  to D2.
- `acceptance.vec` has 4 new vectors: `double-72`, `decode-double`,
  `context-enumerated` and `context-signed`. All 23 pass.
- `bacapp.h` and `driver.c` were already updated and were not changed.

### Open points

- **SPEC.md is not in this directory**, so I could not update it. The ids
  E15, E15a, E16, E17 and D10 are my proposals. The SPEC.md owner needs to
  add these rules, update D2 and raise the spec version (currently v1.0). If
  SPEC.md already uses any of these ids for something else, the comments in
  `bacapp.c` must be renumbered to match.
- E15a (refusing NaN doubles) is my reading of D-7, not something the CR
  asked for; see item 1.
