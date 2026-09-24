## CR-101

1. **Application-tagged Double (tag 5): implemented.**
   - `bac_enc_double()` writes `55 08` followed by the IEEE-754 double in 8 big-endian
     octets (ASHRAE 135 clause 20.2.7; e.g. 72.0 -> `55 08 40 52 00 00 00 00 00 00`).
     It returns -1 without touching the buffer if the capacity is less than 10 or `buf` is NULL.
   - `bac_dec_app()` now accepts tag 5. Before this change tag 5 was explicitly rejected. It
     requires a content length of exactly 8, rejects truncated input, and stores the value in
     `v.d`. Like the other decoders, it accepts a non-minimal extended-length header
     (e.g. `55 FE 00 08 ...`).
   - Acceptance vectors added: `double-72`, `decode-double`, `double-bad-length`.
2. **`bac_enc_ctx_enumerated()` / `bac_enc_ctx_signed()`: implemented.** They reuse the same
   minimal-length unsigned/two's-complement content encoding as the application-tagged
   versions. Context tags >= 15 use the extended tag octet, and tag 255 is rejected
   (it is reserved), the same as `bac_enc_ctx_unsigned()`.
   Acceptance vectors added: `context-enumerated`, `context-signed`.

I found no conflict with the BACnet standard or the existing requirements in either item.

Unsure / assumptions:
- NaN: the CR does not say how to handle it. `bac_enc_double()` rejects NaN (returns -1)
  so that it matches the existing `bac_enc_real()` behaviour. Infinities and -0.0 are
  encoded. The decoder passes through any bit pattern, including NaN, just as the
  REAL decoder does. If product wants doubles to handle NaN differently, REAL needs to
  change too.
- The encoder assumes `double` is IEEE-754 binary64 with the same byte order as
  `uint64_t` (true on all supported targets).
