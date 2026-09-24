# bacapp.c review

## Changes made (all in bacapp.c)

1. `hdr_len`/`put_hdr`: the one-octet extended length now covers 5..253 (`len <= 253`, was `< 253`). Clause 20.2.1.3.1 says 253 uses the single-octet form. The old code wrote 253 as `FE 00 FD`, which is not minimal, so a 253-byte value came out 2 bytes longer than it should.
2. `sint_len`: -128 now takes one octet (`v >= -128`, was `v > -128`). Before, `enc_signed -128` gave `32 FF 80` where the minimal form is `31 80`.
3. `bac_enc_octet_string`: the capacity check for header plus data now runs before anything is written. Before, the header was written first and the function then returned -1 when the data did not fit, leaving the caller's buffer modified (`ERR DIRTY`).
4. `bac_enc_ctx_boolean`: a context-tagged Boolean is now encoded as LVT = 1 followed by one contents octet 0x00/0x01, per clause 20.2.3 (context tag 3 TRUE gives `39 01`). The old code put the value in the LVT field, which is only correct for application-tagged Boolean. Peers decoded it as a zero-length or one-length value with the wrong contents.
5. `bac_enc_bit_string`: now returns -1, before writing anything, if any `bits[i]` is not 0 or 1. The header says `bits[i]` is the value of a bit, and the test suite has an argument-error class for bit strings (E8a) like the other encoders do. Before, values like 2..9 were silently treated as 1.
6. `bac_enc_octet_string`, `bac_enc_char_string`, `bac_enc_bit_string`, `bac_dec_app`: added `INT_MAX` guards, with `<limits.h>`, so an encoded or consumed length that cannot fit in the `int` return value gives -1. Before, a length that large overflowed the `(int)` cast into a negative value or a wrong length. It can only happen with buffers over 2 GB, so normal behavior does not change.

## Suspicious-looking behavior left unchanged (intended)

- Unsigned/enumerated 0 is encoded as one content octet (`21 00`), and a zero-length unsigned is rejected on decode. This follows the standard and is pinned by the CR3zero test.
- `bac_enc_real` rejects NaN (all NaN payloads) but accepts ±Inf and denormals. This is a deliberate policy (CR3nan tests). The decoder still accepts NaN bit patterns on the way in.
- `bac_enc_char_string` does not check that the input is valid UTF-8 (overlong, truncated and stray continuation bytes all pass through). This is a deliberate policy (CR3utf8 tests).
- The decoder accepts non-minimal encodings: an extended tag number below 15, an extended length below 5 or in a longer form than needed, and leading zero content octets in unsigned/signed values (D1b/D4 tests). This is intended lenient decoding.
- `bac_dec_app` does not range-check date/time fields (for example day 35, wday 8, 99:99:99.99), does not require unused bit-string pad bits to be zero, and accepts any character-set byte. Only structure is validated, by design (tests p0512, p0516, p0461, p0497).
- NULL and Boolean are validated on the raw LVT bits, so an extended-length form (`05 00`, `15 01`) is rejected. This is intended (tests p0476, p0477).
- The decoder rejects Double (tag 5) and the reserved tags 13–15, because the module does not support them.
- Context tag 255 is rejected by the context/opening/closing encoders and by `bac_dec_tag`. It is reserved.
- In `bac_dec_app`, `(L - 1) * 8` for bit strings could only overflow on a 32-bit `size_t` with a buffer over 512 MB. It is not reachable on the target, so I did not change it (a guard would give a `-Wtype-limits` warning on 64-bit builds).

## Verification

- `make` builds with no warnings. `run_vectors.py acceptance.vec tests/unit.vec` gives 279/279.
- I compared the encoders against an independent Python model of clause 20.2 on about 2400 random and boundary cases, with no mismatches. These covered all header length boundaries (4/5, 253/254, 65535/65536), signed boundaries, exact and exact-minus-one capacity, and context tags 0–254. Decoder round trips also matched.
