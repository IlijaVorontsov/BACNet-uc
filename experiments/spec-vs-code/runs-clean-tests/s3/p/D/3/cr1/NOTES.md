## CR-101

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double` writes `55 08` followed by the 8-octet IEEE-754 binary64 bit pattern,
     big-endian and bit-exact (for example -0.0 → `55 08 80 00 ...`, ±Infinity encoded
     normally). It needs `cap` ≥ 10; otherwise it returns -1 and leaves the buffer
     untouched (G1).
   - `bac_dec_app` now decodes tag 5 with content length exactly 8 into `v.d`. Any other
     length → -1. Lenient header forms are accepted per D1b, for example `55 fe 00 08`.
     NaN is decoded like any other value, as D5 already does for Real.
   - SPEC.md (now v1.1): added E15 and D5a, removed tag 5 from the D2 reject list, and
     extended E5a. D2 is a [POL] rule; this CR comes from the product owner, so I took it
     as the sign-off needed to change that rule.
   - **NaN refused (I decided this; please confirm):** the CR says nothing about NaN.
     `bac_enc_double` returns -1 for any NaN, the same as `bac_enc_real` under E5a. Decision
     D-7's reason is that NaN must never go on the wire because the BMS front-end and two
     third-party workstations fail on it. That reason applies to Double just as much, so
     encoding NaN as a Double would get around the policy. If the product owner really
     wants NaN sent as Double, D-7/E5a has to be revised first.
   - Tests: unit vector p0526 (`dec_app 5508` + 8 zero octets) used to expect ERR because
     of the old "Double not supported in v1.0" rule. It now expects
     `OK n=10 d 0000000000000000`. Added unit vectors c101-01..32 and the acceptance vectors
     `double-72` and `decode-double`.
   - Unsure / portability: `bacapp.c` now has
     `_Static_assert(sizeof(double) == 8, ...)`. On targets where `double` is 32-bit
     (for example avr-gcc without `-mdouble=64`), the API cannot represent a BACnet
     Double, and the build now fails on purpose instead of producing wrong encodings.
     Like the existing Real code, the implementation assumes floating-point byte order
     matches integer byte order (true on all mainstream targets).

2. **`bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`: implemented.**
   - Context class, given tag number (extended tag octet for 15..254), content as E3
     (Enumerated) or E4 (Signed, minimal two's complement). Context tag 255 → -1 (G3).
     No partial writes (G1).
   - SPEC.md: added E16 and E17. Added unit vectors c101-33..60 and the acceptance vectors
     `context-enumerated` and `context-signed`.

I found no conflict with ASHRAE 135: `55 08` + 8 octets is the standard Double encoding,
and context-tagged Enumerated/Signed follow clause 20.2.1.3.1. All 364 vectors
(acceptance.vec and tests/unit.vec) pass.
