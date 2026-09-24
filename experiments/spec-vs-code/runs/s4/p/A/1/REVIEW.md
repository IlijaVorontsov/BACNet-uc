# bacapp.c review

## Changes made

- `sint_len`: `v > -128` changed to `v >= -128`. -128 fits in one octet, but it was encoded as 2 octets (`32ff80` instead of `3180`). Clause 20.2.5 requires the minimum number of octets, and the other bounds in the function are already inclusive.
- `hdr_len` / `put_hdr`: the single-octet extended-length cutoff changed from `len < 253` to `len <= 253`. Clause 20.2.1.3.1 encodes lengths 5..253 in one octet, and this module's own decoder reads `e < 254` as a direct length. A 253-byte value was getting the non-canonical 3-octet form `FE 00 FD`.
- `bac_enc_octet_string`: all capacity checks now run before `put_hdr`. When the buffer held the header but not the data, the header was written and then -1 was returned, leaving the caller's buffer modified (the driver reported "ERR DIRTY"). Every other encoder leaves the buffer untouched on error.
- `bac_enc_ctx_boolean`: now writes a length-1 context tag followed by one content octet (0x00 or 0x01), for example `0901` and `0900`. Before, the value went into the LVT field (`09` / `08`), which is the application-tag form. Clause 20.2.3 says a context-tagged Boolean has one content octet. The capacity check and return value now include that octet.
- `bac_enc_octet_string`, `bac_enc_char_string`, `bac_enc_bit_string`: added a guard that returns -1 when the total length would be more than INT_MAX. On 64-bit builds, lengths up to 0xFFFFFFFF were accepted, so `(int)(h + len)` could overflow the return value. This has no effect on any realistic size.
- `bac_enc_bit_string`: `(nbits + 7) / 8` changed to `nbits / 8 + (nbits % 8 != 0)`, so a very large `nbits` can no longer wrap the byte count. The result is the same for every non-overflowing input.
- `bac_dec_app`: added a matching guard that rejects a content length that would overflow the `int` return value (`h + L > INT_MAX`). This is only reachable with buffers larger than 2 GiB.
- Added `#include <limits.h>` for INT_MAX.

## Left unchanged on purpose

- `bac_enc_real` returns -1 for NaN, but the decoder accepts NaN. The check is explicit and separate, so it reads as a deliberate policy of never sending NaN. I kept it; the author should confirm it is intended.
- The decoders accept non-minimal encodings: a sign-extended or zero-padded integer (`32ff80`, `220048`), an extended length used for small values (`65fe00fd`, or an extended octet below 5), and an extended tag number below 15. Being lenient on input is normal robustness practice, and rejecting these could break interoperability.
- `bac_dec_app` rejects DOUBLE (tag 5) and tags 13-15. The API has no double type, and those tags are reserved.
- `bac_enc_date` accepts month 13/14 (odd/even months) and day 32/33/34 (last/odd/even days). These are valid special values in ASHRAE 135.
- Tag 255 is rejected in the context, opening and closing encoders and in `bac_dec_tag`. 255 is reserved for extended tag numbers.
- The decoders do not range-check date/time fields, the character-set byte, or the unused trailing bits of a bit string. Only the encoders validate, and the lenient decode passes the raw values to the caller.
- `bac_dec_tag` fills `*out` partially before some error returns. Callers must not use `*out` when -1 is returned, and the header documents no guarantee either way.
- `bac_enc_boolean` (application tag) keeps the value in the LVT field (`10` / `11`). This is correct for application-tagged Booleans. Only the context-tagged form needed a content octet.
