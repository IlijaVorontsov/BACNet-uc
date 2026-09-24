## CR-101

Neither item conflicts with ASHRAE 135 clause 20.2 or with the requirements the
existing tests encode, so both are implemented. `bacapp.h` and `driver.c` were
already updated, and I did not change them.

### Item 1: application-tagged Double (tag 5): IMPLEMENTED
- `bac_enc_double()` writes `55 08` and then the 8 IEEE-754 binary64 octets,
  most significant octet first. This is the clause 20.2.7 encoding; 72.0
  encodes as `55 08 40 52 00 00 00 00 00 00`. It returns 10, or -1 when
  `buf` is NULL or `cap < 10`. On error it writes nothing (G1).
- `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is
  exactly 8. Any other length, or truncated input, gives -1. As for the other
  tags, a length in the non-minimal extended form (e.g. `55 fe 00 08 ...`)
  and an extended tag-number form (`f5 05 08 ...`) are accepted (D1b).
- A `_Static_assert(sizeof(double) == 8)` was added. The codec copies the
  bytes of the double into a `uint64_t` in the same way `REAL` uses a
  `uint32_t`.
- Test change: `tests/unit.vec` p0526 (D2) expected `55 08 00..00` to be
  rejected, because Double was unsupported. CR-101 changes that behaviour,
  so the vector now expects `OK n=10 d 0000000000000000` and has a comment
  saying why. p0528 and p0529 still expect ERR (truncated or wrong length).

### Item 2: context-tagged Enumerated and Signed: IMPLEMENTED
- `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()` work like
  `bac_enc_ctx_unsigned()`. They use the same minimal-length content as the
  application forms (clauses 20.2.5 and 20.2.11), set the class bit to
  context, and use the extended tag-number form for tags 15 to 254 (G3).
  Tag 255 is reserved and rejected. They check the capacity before writing
  anything (G1).

### Tests
- `tests/unit.vec` has new blocks `c101-*` (tag `CR101`) covering Double
  encode and decode, context Enumerated and context Signed, with boundaries,
  extended tags, tag 255 and capacity limits.
- `acceptance.vec` has new blocks: `double-72` (the clause 20.2.7 example),
  `decode-double`, `context-enumerated` and `context-signed`.
- 363/363 vectors pass. They also pass in a separate ASan/UBSan build.

### Open points to confirm
- **NaN handling for Double.** The CR does not say what to do with NaN. I
  followed the existing REAL rule (E5a): the encoder rejects any NaN and
  returns -1, while +/-Infinity, subnormals and -0.0 are encoded. The decoder,
  like REAL (D5), accepts any bit pattern, NaN included. If the product owner
  wants NaN doubles encoded, the only change needed is to remove the `isnan`
  check in `bac_enc_double()`, plus the vectors c101-11..13.
- **Requirement IDs.** No requirements document is in this directory. The
  new vectors use provisional tags (EDBL, EDBLa, DDBL, ECENUM, ECSGN) until
  the requirements list gets official IDs for these functions. The D2
  requirement text, if it lists tag 5 as unsupported, should be updated to
  match p0526.
- **Platform.** The code assumes `double` is IEEE-754 binary64 with the same
  byte order as `uint64_t`. That holds on all current mainstream targets, but
  not on old mixed-endian ARM FPA ABIs.
