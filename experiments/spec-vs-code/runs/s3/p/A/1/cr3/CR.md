# CR-103 — Collected field requests

1. From: integration team — `bac_enc_char_string` must reject text that is not
   well-formed UTF-8 (RFC 3629): return -1 for truncated or invalid sequences, overlong
   encodings, surrogates (U+D800..U+DFFF) and code points above U+10FFFF.
2. From: trend-log team — "Our trend-log service uses NaN to mean 'no sample'.
   `bac_enc_real` and `bac_enc_double` currently refuse NaN; please encode NaN like any
   other value."
3. From: a system integrator — "According to the standard, an Unsigned or Enumerated
   value of 0 must be encoded with zero content octets (`20` / `90`), but your encoder
   emits `21 00`. Please fix."
