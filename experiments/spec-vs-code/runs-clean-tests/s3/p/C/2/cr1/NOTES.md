## CR-101

Item 1 (application-tagged Double, tag 5): **implemented.**
- `bac_enc_double()` writes `55 08` and then the 8 IEEE-754 binary64 octets, most significant first (10 bytes in all). It returns -1 without writing anything if `cap < 10` or `buf` is NULL. This matches the ASHRAE 135 clause 20.2.7 example (72.0 -> `55 08 40 52 00 00 00 00 00 00`).
- `bac_dec_app()` now decodes tag 5 into `v.d` and accepts only a content length of exactly 8. A non-minimal extended length (for example `55 FE 00 08 ...`) is accepted, the same as for the other types (D1b).
- A `_Static_assert` checks that `double` is 8 bytes.

Item 2 (`bac_enc_ctx_enumerated`, `bac_enc_ctx_signed`): **implemented.**
- They use the same minimal-length content encoding as `bac_enc_enumerated` and `bac_enc_signed`, with a context-class header built the same way as in `bac_enc_ctx_unsigned`. Extended tag numbers 15..254 are supported, and tag 255 is rejected (G3).

I found no conflict with the documented requirements or with the BACnet standard in either item.

Test artifacts:
- `tests/unit.vec`: p0526 (`dec_app 5508 00..00`) expected ERR because Double was not supported before. Tag-5 decoding is now required, so it expects `OK n=10 d 0000000000000000` and its tag changed from D2 to DV. p0528 and p0529 (tag 5 with length 4) still expect ERR.
- `tests/unit.vec`: added blocks `c101-*` (tag `CR101`) for Double encoding, rejection, capacity and decoding, and for context Enumerated/Signed (values, tag numbers 15+ and 255, capacity).
- `acceptance.vec`: added the clause 20.2.7 Double example, a double decode, and one context Enumerated and one context Signed example.
- `make`, then `python3 run_vectors.py tests/unit.vec acceptance.vec`: 356/356 pass. It also passes when built with ASan/UBSan.

Things I am unsure about:
- **NaN:** `bac_enc_double` rejects NaN (returns -1), the same way `bac_enc_real` does (E5a). The CR does not say how NaN should be handled; I followed the existing product rule for REAL. The decoder accepts any bit pattern, NaN included, as it does for REAL (D5). If NaN should be encodable as a Double, the check can be removed.
- The new test tags `Edbl`, `Edbla`, `Ddbl` and `Ectx` are placeholders. I did not have the requirements list, so they are not official requirement IDs and should be mapped to real IDs when the requirements document is updated.
