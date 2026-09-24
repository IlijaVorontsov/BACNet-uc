# bacapp.c review

## Changes made

- `hdr_len` / `put_hdr`: changed `len < 253` to `len <= 253`. Clause 20.2.1.3.1 puts lengths 5..253 in a single extended-length octet. A length of 253 was being encoded in the 3-octet form (`FE 00 FD`), which is non-canonical (for example, `enc_octets` of 253 bytes gave `65 FE 00 FD ...` instead of `65 FD ...`).
- `sint_len`: changed `v > -128` to `v >= -128`. -128 fits in one octet (`0x80`), but it was encoded as `32 FF 80`, which is not the minimum length the spec requires.
- `bac_enc_octet_string`: the capacity check now runs before the header is written. Before, a buffer that could hold the header but not the data got the header written and then returned -1 (driver: `ERR DIRTY`). Every other encoder leaves the buffer untouched on error.
- `bac_enc_ctx_boolean`: now encodes the value as length 1 plus one contents octet (`X9 00` or `X9 01`), as clause 20.2.3 requires for context-tagged booleans. Before, it put the value in the L/V/T field like the application-tagged boolean (for example, `09`/`08` with no contents octet), and peers cannot decode that. The capacity check now also counts the contents octet.
- `bac_enc_bit_string`: now rejects any `bits[i]` other than 0 or 1, before anything is written. The API defines `bits[i]` as the value of a bit, and the other encoders reject out-of-range arguments. The existing capacity test for this case (`enc_bits 1012 @0`, G1/E8a) was generated as an error case: every other G1 test uses the exact encoded size or one less.

## Suspicious-looking behavior left unchanged (intended)

- `bac_enc_char_string` does not validate UTF-8. The E7a vectors expect malformed or overlong UTF-8 to be encoded as-is. The module is a transport encoder, not a text validator.
- `bac_enc_real` rejects NaN but accepts +/-Inf and denormals. The E5 and E5a vectors specify this.
- The decoders are lenient on non-canonical input: extended-length octets smaller than 5, the 254/255 forms for small lengths, extended tag numbers below 15 (`F1 02 ...`), and integers with leading zero or 0xFF octets. The D1b and D4 vectors require this, and it follows the "be liberal in what you accept" approach.
- `bac_dec_app` does not range-check date and time fields (for example month 13, day 35, weekday 8, time 99:99:99.99) or the character-set octet. It returns the raw values, as the D9 and D7 vectors expect, and leaves validation to the caller.
- Application tag 5 (Double) and tags 13-15 are rejected by `bac_dec_app`. The module has no Double support (there is no union member for it), and the D2 vectors expect ERR.
- `bac_dec_app` ignores trailing bytes after the value. By design it returns the number of bytes consumed.
- Encoders return `int`, so a total length above INT_MAX (a buffer of about 2 GB or more) would overflow the return value. `bac_dec_app` has the same limit on `h + L`. This can't happen with firmware-sized buffers, so I left it alone rather than add more error paths.
- In `bac_enc_bit_string`, `(nbits + 7) / 8` could wrap only when `nbits > SIZE_MAX - 7`, and that needs a `bits` array that cannot exist. Left as is.
- Tag number 255 is rejected for context, opening and closing tags, and by `bac_dec_tag`. It is reserved (clause 20.2.1.2), so this is correct.
