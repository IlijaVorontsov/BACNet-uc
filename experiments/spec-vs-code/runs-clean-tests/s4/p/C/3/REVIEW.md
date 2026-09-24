# bacapp.c review

The shipped code passed all 279 visible vectors, so the bugs were found by reading the code against ASHRAE 135 clause 20.2 and by differential testing. That meant comparing the driver's output with an independent Python reference on about 100k random and boundary inputs, in both normal and ASan/UBSan builds, plus encode-then-decode round trips. After the fixes, the reference and the driver agree on every input, the sanitizers are clean, and 279/279 vectors pass.

## Changes made

- `hdr_len` / `put_hdr`: changed the one-byte extended-length test from `len < 253` to `len <= 253`. A length of exactly 253 was being written as `FE 00 FD` instead of `FD`, and clause 20.2.1.3.1 requires the one-octet form for lengths 5..253. This affected octet strings, character strings and bit strings with a content length of 253.
- `sint_len`: changed `v > -128` to `v >= -128`. -128 was encoded as `32 FF 80` instead of the minimal `31 80`, and clause 20.2.5 requires the minimum number of octets.
- `bac_enc_octet_string`: moved the payload capacity check (`cap - h < len`) ahead of `put_hdr`. Before, if the header fit but the data did not, the function wrote the header into the caller's buffer and then returned -1. No other encoder changes the buffer when it fails.
- `bac_enc_ctx_boolean`: now encodes the header with length 1 followed by one content octet (`00` or `01`), as clause 20.2.3 requires for context-tagged Booleans. Before, it put the value in the length field with no content octet (the application-tag form), so `[0] TRUE` came out as `09` instead of `09 01` and peers would misparse it. The capacity check now includes the content octet.
- `bac_enc_octet_string`, `bac_enc_char_string`, `bac_enc_bit_string`, `bac_dec_app` (plus `#include <limits.h>`): these now return -1 when the byte count would exceed `INT_MAX`. Before, `(int)(h + len)` could wrap, so a buffer of 2 GiB or more (possible on a 64-bit host) could get a small positive count back, for example 5 for a 4 GiB octet string. Nothing changes for any result that fits in an `int`.

## Suspicious-looking behavior left unchanged (deliberately)

- `bac_enc_real` rejects NaN but accepts infinities. The visible vectors (E5a) require NaN to give ERR, so this is intended.
- `bac_enc_char_string` does not check that the input is valid UTF-8 (overlong, truncated and lone continuation bytes are encoded as given). The E7a vectors expect this pass-through.
- `bac_enc_bit_string` treats any nonzero `bits[i]` as 1 instead of rejecting values other than 0 and 1. The header only says `bits[i]` "is the value of bit i", and `if (bits[i])` is plainly deliberate.
- The decoder accepts non-canonical input: non-minimal extended lengths (e.g. `65 FF 00000003`), the extended-tag form for tag numbers below 15, unsigned and signed values with leading zero or sign bytes, nonzero unused trailing bits in bit strings, and date or time fields outside their normal ranges. The D1b, D4, D8 and D9 vectors require this; it follows the usual "strict encoder, lenient decoder" approach.
- `bac_dec_app` rejects Double (tag 5), because `bac_value_t` has no field to hold it. Tags 13 and 14 and all extended application tags are also rejected.
- `bac_dec_app` ignores bytes after the value and returns the number of bytes consumed. That is the documented contract.
- The encoders accept context tag numbers 15..254 in the extended form and reject 255 (reserved), as the spec requires.
- `bac_enc_date` does not check whether the day is valid for the month (e.g. Feb 30). Only the per-field ranges and the special values (13/14, 32/33/34, 255) are checked, which matches the tests and the spec's wildcard semantics.
- `bac_enc_bit_string` computes `(nbits + 7) / 8`, which could wrap only if `nbits` were within 7 of `SIZE_MAX`. No caller can have a bits array that large, so this cannot happen.
- In `bac_dec_app`, `(size_t)(L - 1) * 8` for bit strings could wrap on a 32-bit `size_t` only if the input buffer held more than 512 MiB. That cannot happen on the 32-bit targets this runs on.
- On a 32-bit `size_t`, the `len > 0xFFFFFFFFu` check in `bac_enc_octet_string` is always false. It is harmless.
