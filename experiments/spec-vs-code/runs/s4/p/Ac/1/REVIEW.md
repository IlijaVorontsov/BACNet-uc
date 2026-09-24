# bacapp.c review

The spec is the rule comments in `bacapp.c` (SPEC.md summary, G/E/D rule ids) plus ASHRAE 135 clause 20.2. `driver.c`, `run_vectors.py` and `acceptance.vec` were not changed. `regression.vec` is new: it holds one vector block per fix and fails on the original code. `make && python3 run_vectors.py acceptance.vec regression.vec` gives 27/27.

## Changes made

1. `sint_len()`: changed `v > -128` to `v >= -128`. Reason: -128 fits in one two's-complement octet, but it was encoded in two octets (`32 ff 80` instead of `31 80`). This broke the minimal-encoding rules E4 and E3/E4.
2. `hdr_len()` and `put_hdr()`: changed the one-octet extended-length bound from `len < 253` to `len <= 253`, in both functions so they still agree. Reason: G4 puts lengths 5..253 in one octet, but length 253 was written as `fe 00 fd` (3 octets). That is not the shortest form, and the comments in both functions say 5..253.
3. `bac_enc_octet_string()`: moved the `cap - h < len` check ahead of `put_hdr()`. Reason: when the header fit but the data did not, the function wrote the header and then returned -1, a partial write that breaks G1 ("no partial writes").
4. `bac_enc_bit_string()`: added the E8a check that rejects any `bits[i]` other than 0 or 1 before the first write. Reason: the comment documents this [POL] rule, but the code did not check it. Masks such as `2` were encoded silently as a set bit.
5. `bac_enc_bit_string()`: changed the byte count from `(nbits + 7) / 8` to `nbits / 8 + (nbits % 8 != 0)`. Reason: `nbits + 7` can wrap for a huge `nbits`, which would give a tiny `nbytes` and then writes far past `cap`. Results for every valid input are unchanged.
6. `bac_enc_ctx_boolean()`: now writes context length 1 plus one content octet `00`/`01`. The capacity check now includes that octet. Reason: E13 requires this, but the code put the value in LVT with no content (`09` instead of `09 01`), which is the application-Boolean form (E2) and not valid BACnet.

## Suspicious-looking behavior deliberately left unchanged

- Decoder accepts non-canonical headers: extended tag number below 15, and extended lengths that fit a shorter form. This is D1b [POL], interop issue #12.
- Decoder accepts non-minimal Unsigned, Enumerated and Signed contents (for example `32 ff 80` decodes to -128). This is D4 [POL].
- Decoder accepts NaN reals, while the encoder refuses them. This is D5 / E5a [POL]. The encoder does accept +/-Infinity, which E5a says explicitly.
- Character-string bytes are not UTF-8 validated (E7a [POL]). The decoder reports any charset octet as is (D7).
- Date, Time and Object Identifier fields are not validated on decode (D9 [POL]). Date encoding checks field ranges but not calendar consistency (E9a).
- The decoder does not check that the unused trailing bits of a Bit String are zero. D8 does not require it.
- Double (tag 5) and tags 13, 14 and 15+ are rejected by `bac_dec_app`, and there is no Double encoder. This is D2 [POL]: not supported in v1.0.
- The NaN check in `bac_enc_real` runs before the NULL-buffer check. Both paths return -1 with nothing written, so the order does not matter.
- Encoders and `bac_dec_app` return `int`. For a buffer larger than INT_MAX the byte count would not fit. This cannot happen on the target (MS/TP frames), so I left it rather than add a new limit to the API.
- The `len > 0xFFFFFFFFu` and `nbytes >= 0xFFFFFFFFu` guards are always false when `size_t` is 32-bit. They are harmless and correct on 64-bit hosts.
- `bac_dec_app` ignores bytes after the value, and pointer fields in `*out` point into `buf`. Both are documented in the API.
