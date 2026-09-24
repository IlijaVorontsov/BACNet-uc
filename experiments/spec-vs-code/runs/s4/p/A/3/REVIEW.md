# bacapp.c review

## Changes made (bacapp.c)

- `sint_len`: changed `v > -128` to `v >= -128`. Before, -128 was encoded in two octets (`32ff80`) when one is enough (`3180`); clause 20.2.5 requires the minimum number of octets.
- `hdr_len` / `put_hdr`: changed `len < 253` to `len <= 253`. Before, a length of exactly 253 was written as `FE 00 FD`; clause 20.2.1.3.1 says lengths 5..253 use one extension octet, and the decoder already reads it that way. Both functions changed together so they still agree.
- `bac_enc_octet_string`: now checks that the whole value fits before writing the header. Before, a buffer big enough for the header but too small for the data returned -1 after the header was already written (the driver reports this as `ERR DIRTY`). Every other encoder already leaves the buffer untouched on error.
- `bac_enc_ctx_boolean`: now encodes the value as a context tag with length 1 plus one content octet of 0x00 or 0x01 (clause 20.2.3). Before, it put the value in the L/V/T field and wrote no content octet, which is the application-tag BOOLEAN form. So `[0] TRUE` came out as `09` instead of `0901` and `[0] FALSE` as `08` instead of `0900`. The capacity check now includes the content octet.

- Added `regression.vec`, which covers the four fixes. `acceptance.vec` and `regression.vec` both pass (23/23). Randomized round-trip checks (signed and unsigned values, context unsigned, octet, character and bit strings up to 70 000 bytes) and decoder fuzzing under ASan and UBSan found no other problems. `driver.c`, `run_vectors.py` and the Makefile are unchanged.

## Suspicious behavior deliberately left unchanged

- `bac_enc_real` rejects NaN. BACnet can carry a NaN REAL, but this is an explicit check and looks like a deliberate product decision. The decoder still accepts NaN.
- DOUBLE (tag 5) is not supported: there is no encoder, `bac_value_t` has no member for it, and `bac_dec_app` rejects it. This looks deliberate.
- The decoders accept encodings that are valid but not in minimal form: leading zero or sign-extension octets in UNSIGNED, SIGNED and ENUMERATED values, extended-length form for short lengths, and extended tag-number form for tags below 15. They also accept nonzero unused bits in a BIT STRING. Real devices send these, so being lenient here is reasonable.
- `bac_dec_app` does not range-check DATE and TIME fields, character-set codes or object types. Checking them is left to the caller, and it matches how the other fields are passed through as raw values.
- Encoders and `bac_dec_app` return `int`. A value longer than INT_MAX bytes would overflow the return value. This can't happen with real buffer sizes, so it was not changed.
- `bac_enc_date` accepts day values 32, 33 and 34 and month values 13 and 14. These are the special values the standard defines (last day of month, odd/even days, odd/even months), so this is intended.
