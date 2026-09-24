# bacapp.c review

## Changes made (bacapp.c only)

1. `hdr_len`/`put_hdr`: one-octet extended-length threshold changed from `len < 253` to `len <= 253`. G4 says lengths 5..253 use a single length octet. Before the fix, a 253-byte content was written in the longer `FE 00 FD` form, which is not the shortest encoding (e.g. `enc_octets` of 253 bytes gave `65 fe 00 fd ...` instead of `65 fd ...`).
2. `sint_len`: `v > -128` changed to `v >= -128`. E4 requires the minimum number of octets, and -128 fits in one (`31 80`). Before the fix it was encoded as `32 ff 80`.
3. `bac_enc_octet_string`: the capacity check now runs before `put_hdr`. G1 forbids partial writes, but the header used to be written into `buf` before the content-length check, so it returned -1 with a dirty buffer (the incident-2024-11 failure mode).
4. `bac_enc_bit_string`: added the E8a check that each `bits[i]` is 0 or 1, with -1 otherwise. It runs before any byte is written, so G1 holds. Before the fix, values such as 2 were silently treated as 1.
5. `bac_enc_bit_string`: the byte count is now `nbits/8 + (nbits%8 != 0)` instead of `(nbits+7)/8`. This stops `nbits + 7` from wrapping when `nbits` is close to `SIZE_MAX`. Hardening only; normal inputs give the same result.
6. `bac_enc_ctx_boolean`: now writes context class, length 1 and a content octet `00`/`01`, as E13 requires (e.g. tag 3 true = `39 01`). Before the fix it wrote the application-Boolean form (value in the LVT, no content), e.g. `39`, which peers decode as a 1-byte value with the content missing.
7. `bac_enc_real`: the NaN test now checks the IEEE-754 bit pattern (exponent all ones, fraction non-zero) instead of calling `isnan()`. The result is the same for every input (E5a: any sign or payload, quiet or signalling). The change keeps the D-7 NaN ban working when the firmware is built with `-ffast-math` or similar flags, where `isnan()` may be optimised to always return false. `<math.h>` is no longer needed; `<limits.h>` was added.
8. Octet, character and bit string encoders, and `bac_dec_app`: added a `> INT_MAX` guard so that an encoded or consumed length that `int` cannot hold returns -1 instead of a wrapped or negative count. Hardening only; lengths that fit in `int` are unaffected.

## Suspicious-looking behavior deliberately left unchanged

- `bac_dec_tag` accepts non-canonical headers: an extended tag number octet below 15 (`F9 03`), and extended lengths that would fit a shorter form (`65 03`, `65 fe 00 10`). This is required by D1b [POL] (interop list #12). As a result, `bac_dec_app` also accepts forms like `F4 04 ...` as a Real.
- `bac_dec_app` accepts non-minimal integers (`22 00 05`, `32 ff ff`). This is D4 [POL].
- `bac_dec_app` decodes NaN Reals even though the encoder refuses NaN. D5 [POL] says E5a restricts only what we send.
- Date, Time and Object Identifier fields are not validated when decoding. This is D9 [POL].
- Null and Boolean decoding use the raw LVT bits, so `15 01` is rejected even though `bac_dec_tag` would read it as an extended length. This is D3 [POL].
- `bac_enc_char_string` does not check that the bytes are valid UTF-8. This is E7a [POL].
- Double (application tag 5) is rejected. It is not supported in v1.0 (D2).
- Bytes after a decoded value are ignored, as the spec for `bac_dec_app` says.
- `bac_dec_tag` may have partly filled `*out` when it returns -1 (for example, an application header with LVT 6/7 or a truncated extended length). The API makes no promise about `*out` on error, and callers must check the return value. Encoders, by contrast, are bound by G1, and I verified that they leave `buf` untouched on every error path.
- ±Infinity is encoded normally. E5a allows it explicitly.
- The encoders check argument validity before checking `buf == NULL` or `cap`. Every path returns -1, so the order is not observable.

## Verification

- `make` builds cleanly with `-Wall -Wextra`, and also with `-Wconversion`. `run_vectors.py acceptance.vec` passes 19/19.
- `fuzz.py` (a test helper I added in this directory; it does not change any shipped file) compares 20,000 random and boundary cases against an independent Python model of SPEC.md. It covers every encoder at different capacities, including the dirty-buffer and overrun checks, plus `dec_tag` and `dec_app`. Result: 0 mismatches. The same script flags the old `< 253` and `> -128` behavior.
