# bacapp.c review

## Changes made

1. `hdr_len` / `put_hdr`: the one-octet extended length now covers 5..253 (`len <= 253`, was `< 253`). Before, a content length of 253 was written as `FE 00 FD`, which is not the shortest form that G4 requires.
2. `sint_len`: the 1-octet range now includes -128 (`v >= -128`, was `v > -128`). Before, -128 was encoded as `32 FF 80` when E4 requires `31 80`.
3. `bac_enc_octet_string`: the full capacity check now runs before the header is written. Before, when the header fit but the data did not, the function wrote the header and then returned -1, a partial write that G1 forbids.
4. `bac_enc_bit_string`: added the E8a check that every `bits[i]` is 0 or 1, returning -1 otherwise. The check runs before any byte of `buf` is written (G1). Before, any non-zero value was silently treated as 1.
5. `bac_enc_ctx_boolean`: it now writes length 1 plus a content octet `00`/`01`, as E13 requires (for example tag 3 true gives `39 01`). Before, it put the value in the LVT field with no content, the application-Boolean form, which does not conform.
6. `bac_enc_real`: the NaN test now reads the IEEE-754 bit pattern (exponent all ones and fraction non-zero) instead of calling `isnan()`. This change is defensive: it covers every quiet and signalling NaN with any sign or payload (E5a), and it still works if the build uses `-ffast-math`, where `isnan` can be optimised away. `<math.h>` is replaced by `<limits.h>`.
7. `bac_enc_bit_string`: `nbytes` is now computed as `nbits/8 + (nbits%8 != 0)` instead of `(nbits+7)/8`. The old form could wrap around for very large `nbits`, which gave a wrong byte count and unused-bits value.
8. Octet, character and bit string encoders, and `bac_dec_app`: each now returns -1 when the total length would not fit in the `int` return value. Before, the cast `(int)(h+len)` could overflow and return a garbage or negative count. This only affects absurdly large inputs, or targets with a 16-bit `int`.

## Checked and deliberately left unchanged

- D1b: lenient header parsing is kept. The decoder accepts extended tag octets below 15 and non-minimal extended lengths such as `65 03` and `65 FE 00 10`. This is a [POL] interop decision.
- D4: non-minimal Unsigned, Enumerated and Signed encodings (for example `22 00 05`) are still accepted when decoding ([POL]).
- D5: the decoder accepts NaN reals, while E5a forbids only sending them. The asymmetry is intended ([POL]).
- D9: Date, Time and Object Identifier fields are not validated when decoding ([POL]).
- E7a: the character-string encoder does not validate UTF-8 ([POL]).
- D1: `bac_dec_tag` treats LVT 5 as an extended length for context tags too ("whatever the tag number and class"), which is what the spec requires.
- D2: application tag 5 (Double) is rejected on purpose, because v1.0 does not support it.
- The application Boolean puts its value in LVT (`10`/`11`) while the context Boolean uses a content octet. Both forms are required by the standard (E2 and E13).
- On error, the decoders may already have written some fields of `*out`. G1 ("no partial writes") covers only encoder output buffers, so this is left as is.
- The Makefile still links `-lm`, which is now unused. The link flag is harmless, and the Makefile is build infrastructure, so it was not changed.
