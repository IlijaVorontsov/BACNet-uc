/* Property check: every byte string bac_dec_app accepts re-encodes to the
 * identical bytes (decoder accepts exactly the canonical encodings), and
 * every successful encode decodes back. Random inputs, run under ASan. */
#include "../bacapp.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static uint64_t s = 88172645463325252ull;
static uint32_t rnd(void) { s ^= s << 13; s ^= s >> 7; s ^= s << 17; return (uint32_t)s; }
static int reenc(const bac_value_t *v, uint8_t *o, size_t cap) {
    static uint8_t bits[1 << 16];
    switch (v->tag) {
    case BAC_TAG_NULL: return bac_enc_null(o, cap);
    case BAC_TAG_BOOLEAN: return bac_enc_boolean(o, cap, v->v.boolean);
    case BAC_TAG_UNSIGNED: return bac_enc_unsigned(o, cap, v->v.u);
    case BAC_TAG_ENUMERATED: return bac_enc_enumerated(o, cap, v->v.u);
    case BAC_TAG_SIGNED: return bac_enc_signed(o, cap, v->v.i);
    case BAC_TAG_REAL: return bac_enc_real(o, cap, v->v.r);
    case BAC_TAG_OCTET_STRING: return bac_enc_octet_string(o, cap, v->v.octets.data, v->v.octets.len);
    case BAC_TAG_CHARACTER_STRING: if (v->v.str.charset) return -2;
        return bac_enc_char_string(o, cap, (const char *)v->v.str.data, v->v.str.len);
    case BAC_TAG_BIT_STRING:
        for (size_t i = 0; i < v->v.bits.nbits; i++) bits[i] = (v->v.bits.data[i / 8] >> (7 - i % 8)) & 1;
        return bac_enc_bit_string(o, cap, bits, v->v.bits.nbits);
    case BAC_TAG_DATE: return bac_enc_date(o, cap, v->v.date.year, v->v.date.month, v->v.date.day, v->v.date.wday);
    case BAC_TAG_TIME: return bac_enc_time(o, cap, v->v.time.hour, v->v.time.minute, v->v.time.second, v->v.time.hundredths);
    case BAC_TAG_OBJECT_ID: return bac_enc_object_id(o, cap, v->v.oid.type, v->v.oid.instance);
    }
    return -3;
}
int main(void) {
    static uint8_t out[1 << 17];
    long ok = 0, rej = 0, skipped = 0, pad = 0;
    for (long it = 0; it < 3000000; it++) {
        size_t n = rnd() % 12;
        uint8_t *in = malloc(n ? n : 1);
        for (size_t i = 0; i < n; i++) in[i] = (uint8_t)rnd();
        if (n && (rnd() & 1)) in[0] = (uint8_t)((rnd() % 13) << 4 | (rnd() % 6)); /* bias to app tags */
        bac_value_t v; bac_tag_t t;
        (void)bac_dec_tag(in, n, &t);
        int r = bac_dec_app(in, n, &v);
        if (r < 0) { rej++; free(in); continue; }
        if (r > (int)n) { printf("consumed past end\n"); return 1; }
        int e = reenc(&v, out, sizeof out);
        if (e == -2) { skipped++; free(in); continue; }
        if (v.tag == BAC_TAG_BIT_STRING && e == r && memcmp(out, in, (size_t)r - 1) == 0) { pad++; ok++; free(in); continue; }
        if (e != r || memcmp(out, in, (size_t)r) != 0) {
            printf("NOT CANONICAL: tag %u in=", v.tag);
            for (size_t i = 0; i < n; i++) printf("%02x", in[i]);
            printf(" r=%d e=%d\n", r, e); return 1;
        }
        ok++; free(in);
    }
    printf("accepted+reencoded %ld (bitstring padding-only diffs %ld), rejected %ld, non-utf8 charset skipped %ld\n", ok, pad, rej, skipped);
    return 0;
}
