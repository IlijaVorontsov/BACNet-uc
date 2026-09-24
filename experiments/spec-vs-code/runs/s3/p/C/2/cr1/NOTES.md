## CR-101

Status per item:

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double-precision value,
     8 octets, most significant octet first (ASHRAE 135 clause 20.2.7; e.g. 72.0 ->
     `55 08 40 52 00 00 00 00 00 00`). It returns -1 without touching the buffer when
     `cap < 10` or `buf` is NULL.
   - `bac_dec_app()` now decodes tag 5 into `v.d` when the content length is exactly 8.
     Any other length, or too few content octets, gives -1. As for the other types, a
     non-minimal length or tag-number encoding is accepted (`55 fe 00 08 ...`,
     `f5 05 08 ...`).
   - Added a `_Static_assert(sizeof(double) == 8)`. Like the REAL code, the bit copy
     assumes the host `double` is IEEE-754 binary64 with the same byte order as `uint64_t`.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.** They mirror
   `bac_enc_ctx_unsigned()`: minimal-length content (1-4 octets, two's complement for
   Signed), context class, extended tag number for tags 15..254, and tag 255 rejected.

No item conflicts with the BACnet standard or the existing requirements, so nothing was
declined.

Test artifacts updated:
- `tests/unit.vec`: p0526 (`dec_app 5508 00*8`) used to expect ERR because Double was
  unsupported. It now expects `OK n=10 d 0000000000000000` and is retagged `DV CR101`.
  Added blocks c101-01..c101-53 for the Double encoder and decoder and for the two
  context encoders, including capacity, tag 15/254/255 and malformed-length cases.
- `acceptance.vec`: added double-72 (clause 20.2.7 example), context-enumerated,
  context-signed and decode-double.
- All vectors pass (353/353). The build is also clean with
  `-Wpedantic -fsanitize=address,undefined`.

Open points to confirm:
- **NaN**: the CR does not say how to handle NaN. `bac_enc_double()` rejects NaN (quiet or
  signalling, any sign) the same way `bac_enc_real()` does (E5a / CR3nan). Infinities,
  signed zero and subnormals are encoded. The decoder accepts NaN bit patterns, as it does
  for REAL. If the NaN rule was meant only for REAL, remove the `isnan` check and change
  vectors c101-09..11.
- `bac_enc_double` has no requirement ID yet. The new vectors are tagged only `CR101`
  (plus the generic G1/G3/DV/D1b/D2 tags). The requirements document should get entries
  for Double and for the two context encoders.
