## CR-101

Item 1 (application-tagged Double, tag 5): implemented.
- `bac_enc_double` writes `55 08` plus the 8 octets of the IEEE-754 binary64 value,
  big-endian and bit-exact (-0.0 and ±Infinity are encoded as they are). Like every other
  encoder, it returns -1 without touching `buf` if `cap` < 10 (G1).
- `bac_dec_app` now decodes tag 5 into `v.d` when the content length is exactly 8, and
  returns -1 for any other length. Non-canonical extended lengths are accepted per D1b
  (for example `55 fe 00 08 ...`). NaN is decoded like any other value, the same as for
  Real (D5).
- **NaN policy, please confirm:** the CR does not mention NaN. Decision D-7 / E5a says
  invalid readings must never go on the wire as NaN, because our BMS front-end and two
  third-party workstations fail on it. So `bac_enc_double` also returns -1 for any NaN.
  This is the reading most consistent with D-7. If the product owner really wants NaN
  doubles on the wire, D-7 must be revisited first.
- The CR removes tag 5 from the D2 reject list. D2 is a [POL] rule, and this CR comes
  from the product owner, so I took it as sign-off for that change.
- Portability: `bacapp.c` now fails to compile (`_Static_assert`) unless `double` is
  IEEE-754 binary64. Some embedded toolchains default to a 32-bit `double` (for example
  AVR, and RX without `-m64bit-doubles`); there the API cannot carry a real Double, and
  a failed build is safer than wrong encodings. Please check that the production target
  toolchain uses a 64-bit `double`. I only built and tested on the host (x86-64 gcc).
- The code assumes that `double` and `uint64_t` use the same byte order in memory. That
  holds on all current targets, but not on old mixed-endian ARM FPA.

Item 2 (`bac_enc_ctx_enumerated`, `bac_enc_ctx_signed`): implemented.
- They work like `bac_enc_ctx_unsigned`: context class, the given tag number (extended
  tag octet for tags 15..254), content per E3 or E4, and -1 for context tag 255 (G3).
  They leave `buf` untouched on error (G1).

Neither item conflicts with ASHRAE 135 or with the documented requirements, so both are
implemented.

Other artifacts updated:
- SPEC.md is now v1.1: revision history, a binary64 note, new rule E15 (Double), E5a
  extended to Double, E12 extended to context Enumerated and Signed, tag 5 removed from
  D2, and D5 now covers Double.
- acceptance.vec: new CR-101 blocks for the Double encode/decode cases (72.0, -0.0,
  Infinity, NaN refused, capacity 9 and 10, NaN decode, bad lengths) and for context
  Enumerated and Signed (short and extended tags, tag 255, capacity). 30/30 pass.
- bacapp.h and driver.c were already updated and were not changed.
