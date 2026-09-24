# CR-101 — Double and more context encoders
From: product owner

1. Add application-tagged **Double** (tag 5):
   `int bac_enc_double(uint8_t *buf, size_t cap, double v);` — IEEE-754 double precision,
   8 octets big-endian, encoded as `55 08` followed by the 8 octets.
   `bac_dec_app` must decode tag-5 values (content length exactly 8) into the new union
   member `double d`.
2. Add `bac_enc_ctx_enumerated(buf, cap, tag, v)` and `bac_enc_ctx_signed(buf, cap, tag, v)`:
   context-tagged versions of Enumerated and Signed (analogous to `bac_enc_ctx_unsigned`).

The updated `bacapp.h` and `driver.c` (new commands `enc_double <hex64>`,
`enc_ctx_enum <tag> <v>`, `enc_ctx_signed <tag> <v>`; decoded doubles print as
`d <hex64>`) are already in place.
