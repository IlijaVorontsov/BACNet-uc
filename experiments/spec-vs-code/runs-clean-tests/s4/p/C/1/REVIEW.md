# bacapp.c review

Verification: `make` builds warning-free; `run_vectors.py acceptance.vec tests/unit.vec` passes 279/279. I also compared 80k randomized commands against an independent ASHRAE 135 clause 20.2 model, including capacity and dirty-buffer checks, and ran them under ASan/UBSan. All matched with no sanitizer findings. The scratch tooling was deleted afterwards.

## Changes made (bacapp.c only)

1. `hdr_len`/`put_hdr`: a data length of exactly 253 now uses the one-octet extended length form (`x5 FD`). Clause 20.2.1.3.1 gives that form for 5..253. The old `< 253` sent 253 through the `FE hh ll` form, making the output 2 bytes longer, non-canonical, and the capacity checks off by 2.
2. `sint_len`: -128 now encodes in one octet (`31 80`). The old `v > -128` made it `32 FF 80`, but clause 20.2.5 requires the minimum number of octets.
3. `bac_enc_octet_string`: the full capacity (header + data) is now checked before anything is written. Before, the header was written and then -1 was returned when the data did not fit, so the caller's buffer was modified on error. No other encoder does this.
4. `bac_enc_ctx_boolean`: a context-tagged Boolean is now encoded as L/V/T = 1 followed by one contents octet 00/01 (clause 20.2.3; e.g. `[0] TRUE` = `09 01`). Before, the value went in the L/V/T field the way an application Boolean does (`09`), which peers read as a 1-octet value with its contents missing. The capacity requirement is now header + 1.
5. `bac_enc_octet_string`, `bac_enc_char_string`, `bac_enc_bit_string`, `bac_dec_app`: an encoding or consumed length that cannot be represented in the `int` return value now returns -1. Before, lengths above 2 GiB came back truncated or negative. Nothing in the normal range changes; `<limits.h>` was added for this.
6. `bac_enc_bit_string`: the byte count is now computed as `nbits/8 + (nbits%8 != 0)` so that `(nbits + 7)` cannot wrap. The result is the same for every real input.

## Suspicious-looking behavior left unchanged on purpose

- Bit-string encoder: any non-zero `bits[i]` counts as a 1 bit. This follows the C truthiness convention the Boolean encoders already use (`v ? 1 : 0`), and nothing in the API contract says to reject such values. Rejecting them could break existing callers that pass masked flag values.
- Character-string encoder does not validate UTF-8. The payload is opaque octets after the charset octet, and the existing tests (E7a) keep accepting malformed sequences.
- The real encoder rejects NaN but accepts ±Inf and denormals. The real decoder returns any bit pattern, NaN included, unchanged. The tests confirm both.
- Decoders accept non-minimal encodings: an extended tag number below 15, an extended length for small lengths, and leading-zero integers. This is the robustness principle, and the D1b/D4 tests confirm it.
- The decoder does not range-check date, time or object-id fields (e.g. month 13, hour 99). These are raw on-the-wire values that callers interpret, and the tests confirm it.
- `bac_dec_app` returns the number of bytes consumed and ignores trailing bytes, so callers can walk a buffer value by value.
- The date encoder accepts month 13/14 and day 32/33/34. These are BACnet special values (odd/even month; last, odd and even day). The year range is 1900..2154 plus 0xFFFF for unspecified.
- DOUBLE (tag 5) and the reserved application tags 13..15 are rejected by the decoder, and application tags with L/V/T 6/7 are rejected. Nothing in the module supports double.
- Context encoders reject tag 255, which is reserved as an extended tag number.
- `bac_dec_tag` may write fields of `*out` before it returns -1 on a truncated header. There is no contract that `*out` stays untouched on error, and `bac_dec_app` never exposes partial results.
- `bac_enc_real` uses `isnan()`. This is correct with the shipped flags but would stop working if built with `-ffast-math`.
- In `bac_dec_app`, `nbits = (L-1)*8` could only overflow a 32-bit `size_t` for bit strings longer than 512 MiB. I did not consider that realistic.
