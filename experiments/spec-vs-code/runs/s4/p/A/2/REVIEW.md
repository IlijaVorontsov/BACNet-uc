# bacapp.c review

## Changes made

- `hdr_len`/`put_hdr`: extended-length boundary changed from `len < 253` to `len <= 253`. Clause 20.2.1.3.1 encodes lengths 5..253 in one octet. A length of exactly 253 was sent as `FE 00 FD`, which is not the canonical form. The decoder already treated 253 as a one-octet length.
- `sint_len`: changed `v > -128` to `v >= -128`. -128 fits in one octet (`31 80`), but the code used two octets (`32 FF 80`), so it was not the minimum-length encoding the standard requires.
- `bac_enc_octet_string`: moved the `cap - h < len` capacity check ahead of `put_hdr`. Before, a buffer that was too small got its header written anyway and the function then returned -1 (a dirty buffer on error). Every other encoder leaves the buffer untouched on error.
- `bac_enc_ctx_boolean`: now encodes the value as L/V/T=1 plus one content octet (0x00/0x01), as clause 20.2.3 requires for context-tagged BOOLEAN. It used to put the value in L/V/T with no content octet, which is the application-tag form, so peers could not read it (e.g. `[0] TRUE` came out as `09` instead of `09 01`).
- `bac_enc_octet_string` / `bac_enc_char_string` / `bac_enc_bit_string`: added a hardening check that returns -1 when the total length would not fit in the `int` return value, and a check that `nbits + 7` does not wrap. Before, a very large `cap` or length could produce a truncated or negative return count after writing the data. No normal-size input behaves differently.
- Added `#include <limits.h>` for `INT_MAX`.

## Deliberately left unchanged

- `bac_enc_real` returns -1 for NaN. This is an explicit check, so I treated it as policy rather than a mistake. The decoder still accepts NaN bit patterns, so decoding stays lenient.
- DOUBLE (tag 5) is rejected by the decoder, and there is no double encoder. `bac_value_t` has no double member, so this is a scope limit.
- The decoder accepts non-minimal encodings (extended-length form for lengths below 5, leading zero octets in Unsigned/Enumerated, extended tag numbers below 15). Being lenient on receive is normal practice and harmless.
- The decoder does not range-check date/time fields, the character-set octet, or the object type. It reports the raw values, and checking them is the caller's job.
- The decoder rejects zero-length Unsigned/Signed/Enumerated and any Real/Date/Time/ObjectId whose length is not 4. This matches the standard's encodings.
- The encoders accept month 13/14 (odd/even months) and day 32..34 (last/odd/even days). These values are defined by the standard.
- Tag number 255 is rejected by the context/opening/closing encoders and by `bac_dec_tag`, and application-class L/V/T 6/7 is rejected by `bac_dec_tag`. Both are reserved or invalid in the standard.
- `bac_dec_tag` may write partial fields to `*out` before it returns -1. The API does not promise otherwise, and callers must ignore `*out` on error.
