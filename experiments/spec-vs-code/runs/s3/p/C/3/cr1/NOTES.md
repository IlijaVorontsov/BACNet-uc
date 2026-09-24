# NOTES

## CR-101

Neither item conflicts with the BACnet standard (ASHRAE 135 clause 20.2) or with
the product's existing behavior, so both are implemented. `bacapp.h` and `driver.c`
were already updated and were not modified.

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double in 8 octets,
     big-endian, and returns 10. Content length 8 does not fit in the LVT field, so
     the header uses LVT=5 plus an extended-length octet. This matches the standard
     (clause 20.2.7, e.g. 72.0 -> `55 08 40 52 00 00 00 00 00 00`). If `cap < 10` it
     returns -1 and leaves the buffer untouched.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly
     8. Any other length is rejected. Like every other tag, the length can be given
     in any valid form (for example `55 FE 00 08 ...`), and trailing bytes are ignored.
     The decoder accepts NaN payloads, just as the REAL decoder does.
   - Tests: `tests/unit.vec` block `p0526` expected `ERR` for `dec_app 5508 00...00`
     because Double was not supported before. It now expects
     `OK n=10 d 0000000000000000`. I also removed its `D2` tag: that tag marks decode
     rejection cases, and this block is no longer one. New blocks `cr101-e*` and
     `cr101-d*` were added, and `acceptance.vec` got `double-72` and `decode-double`.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.**
   They work like `bac_enc_ctx_unsigned()`: minimal-length contents (unsigned for
   Enumerated, two's complement for Signed), context class, extended tag number for
   tags >= 15, and -1 for tag 255 (reserved) or when capacity is too small. New blocks
   `cr101-c*` and `cr101-s*` were added, and `acceptance.vec` got `context-enumerated`
   and `context-signed`.

Open questions:
- **NaN in `bac_enc_double()`.** The CR says nothing about NaN. `bac_enc_real()`
  rejects NaN (the CR-3 `CR3nan` vectors), so `bac_enc_double()` also returns -1 for
  any NaN, quiet or signaling. ±Inf, ±0 and subnormals are encoded. The product owner
  should confirm this. The tests for it are tagged `CR101nan`.
- The `E*`/`D*` tags in `tests/unit.vec` appear to be requirement IDs from a document
  that is not in this directory. I did not give the new vectors requirement IDs. They
  are tagged `CR101`, plus `G1` (capacity) or `G3` (extended tag number) where those
  clearly apply. Requirement IDs for Double and the new context encoders should be
  added once the requirements document is updated.
