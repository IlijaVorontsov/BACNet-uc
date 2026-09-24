# bacapp.c review

## Changes made

1. `hdr_len()` / `put_hdr()`: length 253 now uses the 1-octet extended-length form (`len <= 253`, was `< 253`). G4 [STD] / ASHRAE 135 20.2.1.3.1 say 5..253 takes one length octet. Before, 253 was sent as `FE 00 FD`, which is not the shortest form. Both functions changed together so they stay in agreement.
2. `sint_len()`: -128 now encodes in one octet (`v >= -128`, was `v > -128`). E4 [STD] asks for the minimum number of octets. Before, -128 came out as `32 ff 80` instead of `31 80`.
3. `bac_enc_octet_string()`: the `cap - h < len` capacity check now runs before `put_hdr()`. Before, the header was written and then -1 was returned, which broke G1 [POL] (no partial writes). The driver showed this as `ERR DIRTY`.
4. `bac_enc_bit_string()`: added the E8a [POL] check that every `bits[i]` is 0 or 1, run before any write. The comment documented it, but the code was missing, so values such as 2 were silently encoded as 1.
5. `bac_enc_bit_string()`: `nbytes` is now computed as `nbits / 8 + (nbits % 8 != 0)` instead of `(nbits + 7) / 8`. The old form wraps when `nbits` is near SIZE_MAX, which would under-size the capacity check. Output is unchanged for every other input.
6. `bac_enc_ctx_boolean()`: now emits length 1 plus a content octet `00`/`01`, as E13 [STD] requires (tag 3 true gives `39 01`). Before, it put the value in LVT with no content octet, which is the application-Boolean form (E2) and wrong for context tags.

## Suspicious-looking behaviour left unchanged (intentional)

- The decoder accepts non-canonical tags and lengths, such as an extended tag number below 15 or a long-form length that fits a shorter form. This is D1b [POL], a documented interop decision.
- Double (application tag 5) is rejected by `bac_dec_app`, and there is no Double encoder. This is D2 [POL]: Double is not supported in v1.0.
- NaN is refused by `bac_enc_real` but accepted by the decoder. These are E5a / D5 [POL], which restrict only what we send.
- Character-string bytes are not UTF-8 validated, and the decoder accepts any charset octet. These are E7a / D7 [POL].
- Date, time and object-identifier fields are not validated on decode. This is D9 [POL]. The encoder range checks (E9a / E10a / E11a) are intentional policy on top of ASHRAE.
- `bac_dec_app` decodes Null and Boolean from the raw LVT bits, so `15 01` is rejected. This is D3 [POL].
- Year 2155 is rejected by the encoder. Octet 255 means "unspecified", so 1900..2154 is the full encodable range (E9a).
- Return values are computed as `(int)(h + len)` in the string encoders and `bac_dec_app`. They would overflow only for buffers larger than INT_MAX (above 2 GiB), which cannot happen on the target, and the API returns `int`, so I made no change.
- The comparison `len > 0xFFFFFFFFu` in `bac_enc_octet_string` is always false when `size_t` is 32 bits. It is harmless there, because the length then always fits the 4-octet form. Left as is.
