# bacapp.c review

## Changes made

- `sint_len`: `v > -128` -> `v >= -128`. Reason: -128 fits in one signed octet; it was encoded as 2 octets (`32ff80` instead of `3180`), a non-minimal encoding (clause 20.2.5).
- `hdr_len` / `put_hdr`: extended-length single-octet form now covers 5..253 (`len <= 253`, was `< 253`). Reason: clause 20.2.1.3.1(b) puts lengths below 254 in one length octet; a length of exactly 253 was wrongly sent as `FE 00 FD`. The decoder already reads `FD` as a single-octet length.
- `bac_enc_octet_string`: the full capacity check (header + data) now happens before the header is written. Reason: when the header fit but the data did not, the function wrote the header and then returned -1, leaving the caller's buffer modified on error (`ERR DIRTY`). Every other encoder checks capacity first.
- `bac_enc_ctx_boolean`: now writes the context tag with L/V/T=1 plus one contents octet (`00`/`01`). Reason: clause 20.2.3 requires this form for context-tagged Booleans. The old code put the value in L/V/T (application-Boolean style), so peers would read the value wrong (e.g. `09` instead of `0901`). The capacity check now covers the extra octet.
- `bac_enc_bit_string`: returns -1 (before writing anything) if any `bits[i]` is not 0 or 1. Reason: the API says `bits[i]` is the value of a bit, and the test suite labels input such as `1012` as an invalid-argument case (E8a). Before this change, out-of-range values were quietly treated as 1.

## Suspicious-looking behavior deliberately left unchanged

- `bac_enc_real` rejects NaN. It looks stricter than the standard, but it is intended (vectors tagged CR3nan). `bac_dec_app` still decodes NaN bit patterns, which is also intended (p0492).
- `bac_enc_char_string` does not check that the input is valid UTF-8. This is intended pass-through (vectors tagged CR3utf8).
- Unsigned/enumerated 0 is encoded as one `00` octet, and the decoder rejects zero-length unsigned/signed values. Intended (CR3zero, D4 vectors).
- The decoders accept non-canonical but unambiguous input: extended tag numbers below 15, extended/long length forms for short lengths, and leading zero octets in integers. Intended (D1b/D4 vectors).
- `bac_dec_app` does not range-check date/time fields (e.g. month 13, day 35, hour 99 decode as they are). Intended (p0512, p0516). It only checks lengths; the caller interprets the values.
- `bac_dec_tag` may partly fill `*out` before returning -1 (e.g. a truncated extended length). The API makes no promise about `*out` on failure, and the driver ignores it.
- Encoders return `int`, so an encoding larger than INT_MAX bytes (octet/character string near 2 GB) would give a truncated return value. This cannot happen on the target or in the driver (capacity at most 80000), and fixing it would add a new error case, so I left it alone.
- The `len > 0xFFFFFFFFu` checks can never be true when `size_t` is 32 bits. They are harmless and are needed on 64-bit hosts.
