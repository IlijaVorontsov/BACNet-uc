# bacapp.c review

## Changes made (all in bacapp.c)

1. `hdr_len`/`put_hdr`: one-octet extended length now covers 5..**253** (`len <= 253`, was `< 253`). Reason: G4 requires the shortest form; a 253-byte content was encoded as `FE 00 FD`, not `FD`.
2. `sint_len`: `v >= -128` (was `v > -128`). Reason: E4 minimum octets; -128 was encoded as `32 ff 80` instead of `31 80`.
3. `bac_enc_octet_string`: checks the full size (header + content) before writing anything. Reason: G1; when the header fit but the content did not, it wrote the header and then returned -1 (dirty transmit buffer).
4. `bac_enc_ctx_boolean`: encodes length 1 plus content octet `00`/`01` (e.g. `39 01`), with capacity check for h+1. Reason: E13; it wrote the value into LVT with no content, like the application Boolean.
5. `bac_enc_bit_string`: every `bits[i]` is checked to be 0 or 1 before any byte is written; otherwise -1. Reason: E8a; any non-zero value was silently accepted as 1.
6. `bac_enc_bit_string`: byte count computed as `nbits/8 + (nbits%8 != 0)` instead of `(nbits+7)/8`. Reason: `nbits+7` wraps for huge `nbits`, which gave `nbytes = 0` and then an out-of-bounds write loop.
7. `bac_enc_real`: NaN is detected from the IEEE bit pattern (exponent all ones, mantissa non-zero) instead of `isnan()`; `<math.h>` replaced by `<limits.h>`. Reason: hardening of E5a. Behaviour is the same with the current flags, but `isnan()` is silently optimised away under `-ffast-math`/`-ffinite-math-only`, which is common in firmware builds. That would put NaN on the wire.
8. Octet/character/bit-string encoders: return -1 if the total length would exceed `INT_MAX`. Reason: G1; otherwise `(int)(h+len)` could come out negative after the buffer was already written. This needs caps above 2 GB, so it cannot happen in practice.

Added `regression.vec` (vectors for items 1-5 and 7); `acceptance.vec` + `regression.vec` all pass. A capacity sweep (cap 0..309 on every encoder) shows no `DIRTY`/`OVERRUN`.

## Suspicious-looking behaviour left unchanged (intentional)

- Lenient header parsing: extended tag octet < 15 (`F9 03`) and non-minimal extended lengths (`65 03`, `65 fe 00 10`) are accepted. Required by D1b [POL]. As a result, `F2 02 00 05` decodes as Unsigned 5.
- Non-minimal integer encodings accepted on decode (`22 00 05`, `32 ff ff`). Required by D4 [POL].
- NaN refused by `bac_enc_real` but decoded normally by `bac_dec_app`. Required by E5a/D5 [POL].
- Date/Time/Object-ID fields are validated on encode but not on decode. Required by D9 [POL].
- Character strings are not UTF-8 validated, and any charset octet is accepted on decode. Required by E7a/D7 [POL].
- Null/Boolean decoding uses raw LVT bits, so `15 01` is rejected even though `bac_dec_tag` reads it as an extended length. Required by D3 [POL].
- Context Boolean (length 1 + content) differs from application Boolean (value in LVT). Required by E13/E2 [STD].
- Year 2155 is rejected by `bac_enc_date` (octet 255 means unspecified). Required by E9a.
- Bit-string decoder does not check that the unused trailing bits are zero. Not required by D8.
- Decoders may partly fill `*out` before returning -1. G1 constrains only encoders.
- `bac_dec_app` returns `(int)(h+L)` and computes `(L-1)*8` for bit strings, which could overflow for inputs above `INT_MAX` or above 512 MB with a 32-bit `size_t`. This cannot happen with the controller's buffer sizes, and the spec defines no error for it.
