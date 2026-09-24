## CR-101

### Item 1 — application-tagged Double (tag 5): implemented

- `bac_enc_double` writes `55 08` followed by the 8 octets of the IEEE-754 double,
  big-endian and bit-exact (for example -0.0 → `55 08 80 00 ..`). It follows G1: the
  capacity (10 octets) is checked before anything is written, and on -1 nothing is written.
- `bac_dec_app` now decodes tag 5 when the content length is exactly 8, into `v.d`, bit-exact.
  Any other length → -1. Lenient headers per D1b are accepted (`55 fe 00 08 ..`).
  A NaN on the wire is decoded like any other value, as D5 does for Real.
- This changes the [POL] rule D2, which rejected tag 5 in v1.0. The product owner can
  sign off [POL] changes and sent this CR, so I changed D2 and added the new rule D10.
  The encoding itself matches ASHRAE 135 clause 20.2.7, so the standard is not in conflict.
- **Judgment call (please confirm): `bac_enc_double` refuses NaN.** The CR does not say
  how NaN should be handled. The E5a rationale (decision D-7: "never as NaN on the wire",
  because front-ends crash) applies to Double just as much as to Real. So I extended E5a to
  `bac_enc_double`: every NaN (quiet or signalling, any sign or payload) → -1, and
  ±Infinity is encoded. The check looks at the bit pattern so that compiler or FP settings
  cannot remove it.
- I added `_Static_assert(sizeof(double) == 8)` in `bacapp.c`. Some bare-metal toolchains
  (for example avr-gcc by default) use a 32-bit `double`. On those the encoder would
  silently send wrong data, so the build now fails instead. If a product target uses such
  a toolchain, the build will stop there, and this needs a decision (for example a
  `-mdouble=64` build flag, or an API that takes the raw 64-bit pattern).

### Item 2 — `bac_enc_ctx_enumerated`, `bac_enc_ctx_signed`: implemented

- Context class, with the given tag number. The content is the same as E3 (Enumerated) and
  E4 (Signed): minimal octets, two's complement for Signed. Extended tag numbers 15..254
  follow G3. Tag 255 → -1. The G1 capacity check comes before any write.
- `bac_enc_ctx_enumerated` produces exactly the same octets as `bac_enc_ctx_unsigned`. This
  is correct, because in BACnet the context tag does not carry the datatype.
- SPEC: E12 now also covers `bac_enc_ctx_enumerated`. New rule E16 covers `bac_enc_ctx_signed`.

### Other artifacts updated

- `SPEC.md` is now v1.1: new rules E15 (Double) and D10 (Double decoding), and changes to E5a,
  E12, E16 and D2. It also records the requirement for a 64-bit `double`.
- `tests/unit.vec`: p0526 (`dec_app 55 08 00..00`) used to expect ERR under the old D2.
  It now expects `OK n=10 d 0000000000000000`. I added p0601–p0629 (Double encode, decode,
  NaN, capacity and length errors), p0630–p0643 (ctx enumerated) and p0650–p0670 (ctx signed).
- `acceptance.vec`: added the clause 20.2 Double example (72.0) with its decode, plus a
  ctx-enumerated and a ctx-signed example.
- `bacapp.h` and `driver.c` were already updated and I did not change them.
  368/368 vectors pass, including a build with ASan and UBSan.
