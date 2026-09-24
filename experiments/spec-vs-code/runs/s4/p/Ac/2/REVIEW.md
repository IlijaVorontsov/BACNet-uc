# bacapp.c review

## Changes made (all in bacapp.c)

1. `hdr_len()` / `put_hdr()`: `len < 253` -> `len <= 253`. G4 requires the shortest form: length 253 fits the one-octet extended length (`65 fd`), but was encoded as `fe 00 fd` (octet/character/bit strings with a content length of exactly 253). Both functions changed together so they still agree.
2. `sint_len()`: `v > -128` -> `v >= -128`. E4 requires the minimum number of octets: -128 fits in one octet (`31 80`) but was encoded as `32 ff 80`.
3. `bac_enc_octet_string()`: moved the `cap - h < len` check ahead of `put_hdr()`. G1 violation: when the header fit but the data did not, the tag header was written into buf and then -1 was returned (partial write into the MS/TP transmit buffer).
4. `bac_enc_bit_string()`: added the E8a check (every `bits[i]` must be 0 or 1, otherwise -1, before any write). The comment described the rule, but no code enforced it, so values like 2 were silently encoded as 1.
5. `bac_enc_ctx_boolean()`: now writes a context header with length 1 plus a content octet `00`/`01` (E13). It used to put the value in LVT with no content octet, like the application Boolean (E2). That produced a wrong encoding (e.g. `39` instead of `39 01`, and `38` instead of `39 00`).

## Suspicious-looking behaviour deliberately left unchanged

- Encoders return `(int)(h + len)`. Octet, character and bit strings accept content up to 0xFFFFFFFF octets (the G4 maximum), so an encoding over INT_MAX bytes would give an unrepresentable return value. This needs a buffer of more than 2 GB, which cannot happen on the target, and rejecting it would narrow the documented accepted range. Flagged for the spec owner instead of changed.
- `bac_dec_tag()` accepts non-canonical headers (extended tag < 15, extended lengths that would fit a shorter form). This is intended by D1b [POL] (interop issue #12).
- LVT 5 is always read as an extended length, so `15 01` is rejected as a Boolean. This is intended by D1 and D3.
- The decoder does not validate Date, Time or Object Identifier fields (D9), the charset octet (D7) or NaN reals (D5). The encoder accepts ±Infinity (E5a) and does not validate UTF-8 (E7a). All of these are documented [POL] decisions.
- Application tag 5 (Double) is rejected by the decoder, as intended by D2 (not supported in v1.0).
- `bac_dec_tag()` fills `*out` before rejecting an application-class LVT 6/7, and `bac_dec_app()` may leave `*out` partly written on -1. The decoders make no promise to leave `*out` unchanged; G1 applies to encoders only.
- `bac_dec_app()` computes bit-string `nbits` as `(size_t)(L - 1) * 8 - unused`. This could only overflow with a 32-bit size_t and a buffer of more than 512 MB, which cannot happen on the target.
- `len > 0xFFFFFFFFu` in `bac_enc_octet_string()` is always false when size_t is 32 bits. This may cause a -Wtype-limits warning on the target but is harmless.
