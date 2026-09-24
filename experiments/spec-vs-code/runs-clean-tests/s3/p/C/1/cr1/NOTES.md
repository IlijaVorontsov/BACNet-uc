## CR-101

**Item 1: application-tagged Double (tag 5). Implemented.**
- `bac_enc_double` writes `55 08` followed by the 8 octets of the IEEE-754 double, most significant octet first. This follows ASHRAE 135 clause 20.2.7 (its example, 72.0, encodes as `55 08 40 52 00 00 00 00 00 00`). The capacity check runs before any write, so the buffer is untouched on error.
- `bac_dec_app` now decodes tag 5 into `v.d` and requires a content length of exactly 8; any other length is rejected. Encoded forms that the other types already accept also work for Double, i.e. an extended tag number (`f5 05 08 ...`) or a non-minimal extended length (D1b).
- The CR did not say what to do with NaN. `bac_enc_double` rejects NaN (returns -1) because `bac_enc_real` does the same (E5a). Both infinities are accepted, as for REAL. The decoder accepts any 64-bit pattern, including NaN, as the REAL decoder does. **Please confirm the NaN policy.**
- I added `_Static_assert(sizeof(double) == 8)` (and one for `float`). The build now fails on a toolchain where `double` is 32 bits (for example, some AVR compilers). I don't know whether any product target uses such a toolchain.
- The conversion assumes the host stores floating-point values with the same byte order as integers. The existing REAL code makes the same assumption.
- Vectors: `tests/unit.vec` p0526 (`dec_app 5508 00...00`) used to expect ERR, because the D2 rule treated tag 5 as unsupported. This CR changes that behaviour, so the test now expects `OK n=10 d 0000000000000000`. The written requirements (the documents behind the E*/D*/G* IDs) are not in this directory, so I could not update them. **D2 needs to be updated to say that Double is supported.**

**Item 2: `bac_enc_ctx_enumerated` / `bac_enc_ctx_signed`. Implemented.**
- They work like `bac_enc_ctx_unsigned`: context class, extended tag number for tags 15 to 254, context tag 255 rejected (reserved), and the minimum number of content octets (unsigned for Enumerated, two's complement for Signed, clauses 20.2.5 / 20.2.11). The buffer is untouched on error.

**Conflicts:** I found none. Both items match the BACnet standard and the behaviour documented in this directory, apart from the D2 change noted above, which is intended.

**Tests:** I added blocks p0541 to p0604 to `tests/unit.vec` (tagged `CR101`, plus G1/G3/D1b/D2/DV where they apply) and 4 blocks to `acceptance.vec`: the clause 20.2.7 Double example, a Double decode, and one context Enumerated and one context Signed encoding. All 368 blocks pass.
