## CR-101

Neither item conflicts with ASHRAE 135 clause 20.2 or with the product's existing behaviour, so both are implemented.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double, 8 octets, big-endian. This matches clause 20.2.7: 72.0 encodes as `55 08 40 52 00 00 00 00 00 00`. It returns -1 without touching the buffer when `cap < 10` or `buf` is NULL.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly 8. Any other length is rejected. It still accepts a non-minimal length header such as `55 fe 00 08`, just as it does for the other types (see D1b).
   - Test changes: `tests/unit.vec` p0526 (`dec_app 5508` + 8 zero octets) used to expect `ERR`. It now expects `OK n=10 d 0000000000000000`, because rejecting tag 5 was the old behaviour. I added vectors `cr101-d*` for the encoder, decoder, capacity checks and NaN. I added the clause 20.2.7 example to `acceptance.vec` as `double-72` / `decode-double`.
   - Unsure: **NaN handling.** The CR says nothing about NaN. I made `bac_enc_double()` reject NaN (return -1) to match the earlier CR3 decision for `bac_enc_real()` (vectors tagged CR3nan). As with REAL, the decoder passes NaN bit patterns through unchanged. The standard itself does not forbid NaN, so the product owner should confirm that Double should follow REAL here. If not, remove the `isnan` check and vectors cr101-d11..d13.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.**
   - They mirror `bac_enc_ctx_unsigned()`. They use a context-class header with the tag number (the extended tag octet for tags 15..254) and the minimal content octets, unsigned for Enumerated and two's-complement for Signed. Tag 255 is rejected, as the standard reserves it and the other context encoders already reject it. The capacity checks leave the buffer untouched on error.
   - I added vectors `cr101-e*` and `cr101-s*` to `tests/unit.vec`.

`bacapp.h` and `driver.c` were already updated and I left them unchanged. `make` builds cleanly with -Wall -Wextra. `run_vectors.py acceptance.vec tests/unit.vec` gives 361/361, also with ASan/UBSan.
