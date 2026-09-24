## CR-101

1. Application-tagged Double (tag 5): **implemented**.
   - `bac_enc_double()` writes `55 08` followed by the 8 octets of the IEEE-754
     double, most significant octet first (ASHRAE 135 clause 20.2.7; the
     standard's example 72.0 -> `55 08 40 52 00 00 00 00 00 00` is now in
     `acceptance.vec`). It needs 10 bytes of capacity and writes nothing on error.
   - `bac_dec_app()` now accepts tag 5 and requires a content length of exactly 8;
     the value is stored in `v.d`. Any other length is rejected. As with the other
     types, a non-minimal extended length (e.g. `55 fe 00 08 ...`) is accepted.
   - Uncertain: the CR does not say how NaN should be handled. For consistency with
     the existing `bac_enc_real()`, `bac_enc_double()` rejects NaN (returns -1).
     Infinities and negative zero are encoded. The decoder, like the REAL decoder,
     does not check the bit pattern, so a received NaN is still decoded.
2. `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()`: **implemented**.
   They mirror `bac_enc_ctx_unsigned()`: the same minimal-length content
   encoding as the application-tagged Enumerated and Signed encoders, context
   class, extended tag numbers for tags 15-254, and tag 255 (reserved) is rejected.

Neither item conflicts with the BACnet standard, so both were implemented.
`bacapp.h` and `driver.c` were already updated and were not changed.
New vectors were added to `acceptance.vec` (double-72, decode-double,
context-enumerated, context-signed). All 23 acceptance blocks pass.
