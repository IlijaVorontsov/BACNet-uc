## CR-101

1. Application-tagged Double (tag 5): **implemented.**
   - `bac_enc_double()` writes `55 08` followed by the 8 octets of the IEEE-754
     double in big-endian order (10 bytes in total). It returns -1 when the
     buffer is NULL or smaller than 10 bytes, without writing anything.
   - `bac_dec_app()` now decodes tag 5 into `v.d`. The content length must be
     exactly 8, otherwise it returns -1. A truncated buffer also returns -1.
     The decoder no longer rejects tag 5 outright.
   - This matches ASHRAE 135 clause 20.2.7. The standard's example (72.0 ->
     `55 08 40 52 00 00 00 00 00 00`) was added to `acceptance.vec`, along with
     a decode round trip.
2. `bac_enc_ctx_enumerated()` and `bac_enc_ctx_signed()`: **implemented.**
   - They work like `bac_enc_ctx_unsigned()`: the context class bit is set, the
     content uses the minimal number of octets (unsigned for Enumerated, two's
     complement for Signed), tags 15-254 use the extended tag-number octet, and
     tag 255 (reserved) is rejected. Vectors were added to `acceptance.vec`.

No item conflicts with the BACnet standard, so nothing was refused.

Open question:
- NaN handling for Double. `bac_enc_real()` rejects NaN, so `bac_enc_double()`
  rejects NaN too, for consistency: it returns -1 and writes nothing. Infinities
  and -0.0 are encoded as normal, as they are for Real. The CR does not mention
  NaN, so the product owner should confirm this. On decode, NaN bit patterns are
  passed through unchanged, as `bac_dec_app()` already does for Real.
