## CR-101

Double and more context encoders. I found no conflict with ASHRAE 135, so both
items are implemented. `make` builds cleanly and `acceptance.vec` passes
(28/28, including 9 new CR-101 blocks).

### Item 1: application-tagged Double (tag 5): IMPLEMENTED

- `bac_enc_double()` writes `55 08` and then the 8-octet IEEE-754 binary64 bit
  pattern, big-endian and bit-exact (72.0 -> `55 08 40 52 00 00 00 00 00 00`,
  which is the ASHRAE 135 clause 20.2.7 example; -0.0 keeps its sign bit). It
  follows G1: it needs `cap >= 10` and writes nothing when it returns -1.
- `bac_dec_app()` now decodes tag 5 into `v.d`. The content length must be
  exactly 8; any other length returns -1. This changes D2 [POL]: v1.0
  rejected tag 5. I took CR-101 from the product owner as the sign-off for
  that change. As with every other tag, the header may use a non-canonical
  extended length (D1b), so `55 fe 00 08 <8 octets>` is accepted.
- **Needs confirmation: NaN.** The CR does not mention NaN. E5a [POL] refuses
  NaN for Real. Its rationale (decision D-7: front-ends crash on NaN, and
  invalid readings go through Status_Flags/Reliability, "never as NaN on the
  wire") applies to Double just as much. So `bac_enc_double()` returns -1 for
  any NaN and encodes +/-Infinity normally. The decoder accepts NaN as-is, the
  same as D5 does for Real. If the product owner wants NaN doubles sent, that
  goes against D-7 and needs an explicit decision. The `double-nan-refused`
  vector in `acceptance.vec` records the current behaviour.
- **Portability assumption.** A new `_Static_assert` requires
  `sizeof(double) == 8`. The build now fails on toolchains where `double` is
  32-bit (for example avr-gcc by default, or `-fshort-double`). Without the
  check those toolchains would silently send wrong bytes. The code also
  assumes that `double` and `uint64_t` use the same byte order. That holds on
  all mainstream targets but not for legacy ARM FPA mixed-endian doubles.

### Item 2: `bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: IMPLEMENTED

- Both use context class with the given tag and the same content as the
  application form: minimal big-endian unsigned (E3) or minimal
  two's-complement (E4). They follow the same rules as
  `bac_enc_ctx_unsigned()`:
  - Context tag 255 returns -1 (G3).
  - Tags 15..254 use the extended tag octet.
  - There are no partial writes (G1).
- Examples: `enc_ctx_enum 1 256` -> `1a0100`, `enc_ctx_signed 5 -72` -> `59b8`,
  `enc_ctx_signed 1 -129` -> `1aff7f`.

### Other artifacts and open points

- `bacapp.c`: I updated the module contract, the rule references and the
  rule -> code map, and added rule comments at the new code.
  `bacapp.h` and `driver.c` were already updated for this CR, so I did not
  change them.
- `acceptance.vec` has new blocks for:
  - Double encode and decode
  - -0.0
  - NaN refusal
  - Capacity 9 (no partial write)
  - Bad Double length
  - Context Enumerated and Signed
  - Context tag 255
- **SPEC.md is not in this directory**, even though `bacapp.c` says it
  implements it, so I could not update it. The rule ids I used are provisional
  and must be added to SPEC.md, which should probably become v1.1:
  - E5d [STD]: Double encoding.
  - E5a [POL]: NaN refusal, extended to Double.
  - E12e [STD] and E12s [STD]: context Enumerated and context Signed.
  - D5d [POL]: Double decoding, length exactly 8, NaN accepted.
  - D2 [POL]: tag 5 is no longer rejected.
