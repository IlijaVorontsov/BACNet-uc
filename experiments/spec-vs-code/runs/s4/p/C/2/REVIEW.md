# bacapp.c review

## Changes made
- `hdr_len`/`put_hdr`: data lengths 5..253 now use the one-octet extended length (was 5..252, so length 253 got the 3-octet `FE 00 FD` form); ASHRAE 135 20.2.1.3.1 requires `B'101'` + one octet for 5..253, and the decoder already reads it that way (affects octet/character/bit strings with 253 content octets).
- `sint_len`: -128 now fits in one octet (`31 80`, was `32 FF 80`); signed values must use the shortest two's-complement form, and `bac_dec_app` already decodes `3180` as -128.
- `bac_enc_octet_string`: the full capacity check (`cap - h < len`) now runs before the header is written; it used to write the tag header and then return -1, leaving the caller's buffer modified on error.
- `bac_enc_ctx_boolean`: now encodes L/V/T = 1 and one contents octet (`X'00'`/`X'01'`), e.g. tag 0 TRUE = `09 01`; context-tagged BOOLEAN has a contents octet (135 20.2.3). The old code put the value in L/V/T with no contents octet, which is the application-tag form, so peers read it as a 0- or 1-octet value.
- `bac_enc_bit_string`: rejects `bits[i]` values other than 0/1 (-1, checked before any write); the old code quietly turned any non-zero value into 1. This matches how the other encoders reject out-of-range input (date/time/oid/tag 255).
- `bac_enc_bit_string`: byte count is now `nbits/8 + (nbits%8 != 0)`, so `nbits + 7` can no longer wrap. This is hardening and changes no output.
- `bac_enc_octet_string`, `bac_enc_char_string`, `bac_enc_bit_string`, `bac_dec_app`: return -1 when the result would not fit in `int`. Before, the `(int)` cast could turn a length over INT_MAX into a wrong or negative count. This is hardening and cannot happen at normal sizes.

## Suspicious-looking behavior deliberately left unchanged
- Unsigned/enumerated 0 is encoded with one contents octet (`21 00`), not zero length. This is intended (tests tagged CR3zero) and matches the standard.
- `bac_enc_real` rejects NaN with -1 even though a BACnet REAL can hold NaN. This is intended (CR3nan). Infinities and denormals are still accepted.
- `bac_enc_char_string` does not check that the bytes are valid UTF-8 and always writes charset 0. This is intended (CR3utf8): the caller must supply valid text.
- The decoders accept non-canonical encodings: extended tag numbers < 15, extended lengths for values < 5 or in a longer form than needed, leading zero octets in integers, non-zero unused trailing bits in bit strings, any charset byte, and NaN REALs. The tests (D1b/D4/D5/D7/D8) show this leniency is intended, following the "be liberal in what you accept" approach.
- `bac_dec_app` does not range-check DATE/TIME fields (e.g. `b463636363` decodes as 99:99:99.99). This is intended (tests D9). The encoders validate their input; the decoders pass fields through unchanged.
- `bac_enc_date` accepts the BACnet special values (month 13/14, day 32/33/34, 255 wildcards) and does not check day-of-month consistency (e.g. Feb 31). That consistency is not an encoding concern.
- DOUBLE (tag 5) is not supported: there is no encoder and the decoder rejects it. This is the module's chosen scope.
- `bac_dec_tag` accepts reserved application tag numbers 13..254 (it only parses headers), while `bac_dec_app` rejects them. Extended tag number 255 is rejected everywhere because it is reserved.
- `len > 0xFFFFFFFFu` in `bac_enc_octet_string` is always false on 32-bit `size_t` targets. It is harmless there, so I left it alone.
