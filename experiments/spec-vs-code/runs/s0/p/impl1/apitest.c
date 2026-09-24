/* Direct API tests for things driver.c cannot express. */
#include "bacapp.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

static uint32_t rng = 12345;
static uint32_t rnd(void) { rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng; }

/* G1 at every capacity: 0..need-1 -> -1 and untouched; need..need+3 -> need, only [0,need) written */
typedef int (*enc_t)(uint8_t *, size_t, const void *);
static uint8_t ref[1024], tb[1024];
static void g1(int (*call)(uint8_t *, size_t)) {
    int need = call(ref, sizeof ref);
    if (need < 0) { /* invalid args: must never touch, any cap */
        for (size_t cap = 0; cap < 40; cap++) { memset(tb, 0xC3, sizeof tb); CHECK(call(tb, cap) == -1); for (size_t i = 0; i < sizeof tb; i++) if (tb[i] != 0xC3) { CHECK(!"dirty on invalid"); break; } }
        return;
    }
    for (size_t cap = 0; cap < (size_t)need + 4; cap++) {
        memset(tb, 0xC3, sizeof tb);
        int r = call(tb, cap);
        if (cap < (size_t)need) {
            CHECK(r == -1);
            for (size_t i = 0; i < sizeof tb; i++) if (tb[i] != 0xC3) { CHECK(!"dirty"); break; }
        } else {
            CHECK(r == need);
            CHECK(memcmp(tb, ref, (size_t)need) == 0);
            for (size_t i = (size_t)need; i < sizeof tb; i++) if (tb[i] != 0xC3) { CHECK(!"wrote past returned length"); break; }
        }
    }
}
static uint32_t gu; static int32_t gs; static uint8_t gt; static uint8_t gdata[1200]; static size_t glen; static float gf;
static uint16_t gy; static uint8_t gm, gd, gw;
static int c_uns(uint8_t *b, size_t c) { return bac_enc_unsigned(b, c, gu); }
static int c_sig(uint8_t *b, size_t c) { return bac_enc_signed(b, c, gs); }
static int c_ctx(uint8_t *b, size_t c) { return bac_enc_ctx_unsigned(b, c, gt, gu); }
static int c_cbool(uint8_t *b, size_t c) { return bac_enc_ctx_boolean(b, c, gt, gu & 1); }
static int c_open(uint8_t *b, size_t c) { return bac_enc_opening_tag(b, c, gt); }
static int c_oct(uint8_t *b, size_t c) { return bac_enc_octet_string(b, c, gdata, glen); }
static int c_str(uint8_t *b, size_t c) { return bac_enc_char_string(b, c, (const char *)gdata, glen); }
static int c_bits(uint8_t *b, size_t c) { return bac_enc_bit_string(b, c, gdata, glen); }
static int c_real(uint8_t *b, size_t c) { return bac_enc_real(b, c, gf); }
static int c_date(uint8_t *b, size_t c) { return bac_enc_date(b, c, gy, gm, gd, gw); }
static int c_null(uint8_t *b, size_t c) { return bac_enc_null(b, c); }
static int c_bool(uint8_t *b, size_t c) { return bac_enc_boolean(b, c, gu & 1); }

int main(void) {
    uint8_t buf[64];
    bac_tag_t t; bac_value_t v;

    /* NULL arguments */
    CHECK(bac_enc_null(NULL, 10) == -1);
    CHECK(bac_enc_boolean(NULL, 10, true) == -1);
    CHECK(bac_enc_unsigned(NULL, 10, 1) == -1);
    CHECK(bac_enc_opening_tag(NULL, 10, 1) == -1);
    CHECK(bac_enc_octet_string(buf, sizeof buf, NULL, 3) == -1);
    CHECK(bac_enc_octet_string(buf, sizeof buf, NULL, 0) == 1);
    CHECK(bac_enc_char_string(buf, sizeof buf, NULL, 0) == 2);
    CHECK(bac_enc_char_string(buf, sizeof buf, NULL, 1) == -1);
    CHECK(bac_enc_bit_string(buf, sizeof buf, NULL, 0) == 2);
    CHECK(bac_enc_bit_string(buf, sizeof buf, NULL, 1) == -1);
    CHECK(bac_enc_char_string(buf, sizeof buf, "x", SIZE_MAX) == -1);
    CHECK(bac_dec_tag(NULL, 3, &t) == -1);
    CHECK(bac_dec_tag((const uint8_t *)"\x21\x01", 2, NULL) == -1);
    CHECK(bac_dec_app(NULL, 3, &v) == -1);
    CHECK(bac_dec_app((const uint8_t *)"\x21\x01", 2, NULL) == -1);

    /* decoders leave *out untouched on error */
    memset(&v, 0x77, sizeof v); { bac_value_t s = v; CHECK(bac_dec_app((const uint8_t *)"\x22\x01", 2, &v) == -1); CHECK(memcmp(&s, &v, sizeof v) == 0); }
    memset(&t, 0x77, sizeof t); { bac_tag_t s = t; CHECK(bac_dec_tag((const uint8_t *)"\x25", 1, &t) == -1); CHECK(memcmp(&s, &t, sizeof t) == 0); }

    /* in-place (aliased) octet/char string encoding */
    { uint8_t b[400]; for (int i = 0; i < 300; i++) b[i] = (uint8_t)i;
      CHECK(bac_enc_octet_string(b, sizeof b, b, 300) == 304);
      CHECK(b[0] == 0x65 && b[1] == 254 && b[2] == 1 && b[3] == 44);
      int ok = 1; for (int i = 0; i < 300; i++) ok &= b[4 + i] == (uint8_t)i; CHECK(ok); }
    { uint8_t b[16] = "hello"; CHECK(bac_enc_char_string(b, sizeof b, (const char *)b, 5) == 8);
      CHECK(memcmp(b, "\x75\x06\x00hello", 8) == 0); }
    { uint8_t b[16] = { 0, 0, 0, 'a', 'b', 'c', 'd', 'e', 'f' }; /* source after destination */
      CHECK(bac_enc_octet_string(b, sizeof b, b + 3, 6) == 8);
      CHECK(memcmp(b, "\x65\x06" "abcdef", 8) == 0); }

    /* signalling NaN decode is bit-exact; real round trip */
    { const uint8_t s[] = { 0x44, 0x7f, 0x80, 0x00, 0x01 }; uint32_t bits;
      CHECK(bac_dec_app(s, 5, &v) == 5); memcpy(&bits, &v.v.r, 4); CHECK(bits == 0x7f800001u); }

    /* G1 across capacities and round trips */
    for (int it = 0; it < 20000; it++) {
        gu = rnd() >> (rnd() % 32); gs = (int32_t)(rnd() >> (rnd() % 32)); if (rnd() & 1) gs = -gs - (int32_t)(rnd() & 1);
        gt = (uint8_t)rnd();
        g1(c_uns); g1(c_sig); g1(c_ctx); g1(c_cbool); g1(c_open); g1(c_null); g1(c_bool);
        glen = rnd() % 400; if (it % 50 == 0) glen = 250 + rnd() % 10;
        for (size_t i = 0; i < glen; i++) gdata[i] = (uint8_t)rnd();
        g1(c_oct); g1(c_str);
        for (size_t i = 0; i < glen; i++) gdata[i] = (rnd() % 500) ? (uint8_t)(rnd() & 1) : (uint8_t)(2 + rnd() % 254);
        g1(c_bits);
        { uint32_t fb = rnd(); memcpy(&gf, &fb, 4); g1(c_real); }
        gy = (uint16_t)(rnd() % 3 ? 1890 + rnd() % 280 : rnd()); gm = (uint8_t)rnd(); gd = (uint8_t)rnd(); gw = (uint8_t)rnd();
        if (rnd() & 1) { gm = (uint8_t)(1 + rnd() % 14); gd = (uint8_t)(1 + rnd() % 34); gw = (uint8_t)(1 + rnd() % 7); }
        g1(c_date);

        /* round trips */
        int n;
        n = bac_enc_unsigned(buf, sizeof buf, gu); CHECK(bac_dec_app(buf, (size_t)n, &v) == n && v.tag == BAC_TAG_UNSIGNED && v.v.u == gu);
        n = bac_enc_enumerated(buf, sizeof buf, gu); CHECK(bac_dec_app(buf, (size_t)n, &v) == n && v.tag == BAC_TAG_ENUMERATED && v.v.u == gu);
        n = bac_enc_signed(buf, sizeof buf, gs); CHECK(bac_dec_app(buf, (size_t)n, &v) == n && v.tag == BAC_TAG_SIGNED && v.v.i == gs);
        n = bac_enc_ctx_unsigned(buf, sizeof buf, gt, gu);
        if (gt == 255) CHECK(n == -1); else { CHECK(bac_dec_tag(buf, (size_t)n, &t) > 0 && t.context && t.tag == gt && !t.opening && !t.closing); CHECK(bac_dec_app(buf, (size_t)n, &v) == -1); }
        n = bac_enc_opening_tag(buf, sizeof buf, gt);
        if (gt == 255) CHECK(n == -1); else CHECK(bac_dec_tag(buf, (size_t)n, &t) == n && t.opening && !t.closing && t.tag == gt);
        n = bac_enc_closing_tag(buf, sizeof buf, gt);
        if (gt == 255) CHECK(n == -1); else CHECK(bac_dec_tag(buf, (size_t)n, &t) == n && !t.opening && t.closing && t.tag == gt);
        { static uint8_t big[1300]; for (size_t i = 0; i < glen; i++) gdata[i] = (uint8_t)rnd();
          n = bac_enc_octet_string(big, sizeof big, gdata, glen); CHECK(bac_dec_app(big, (size_t)n, &v) == n && v.v.octets.len == glen && memcmp(v.v.octets.data, gdata, glen) == 0);
          n = bac_enc_char_string(big, sizeof big, (const char *)gdata, glen); CHECK(bac_dec_app(big, (size_t)n, &v) == n && v.v.str.charset == 0 && v.v.str.len == glen && memcmp(v.v.str.data, gdata, glen) == 0);
          for (size_t i = 0; i < glen; i++) gdata[i] = (uint8_t)(rnd() & 1);
          n = bac_enc_bit_string(big, sizeof big, gdata, glen); CHECK(bac_dec_app(big, (size_t)n, &v) == n && v.v.bits.nbits == glen);
          if (n > 0) { int ok = 1; for (size_t i = 0; i < glen; i++) ok &= ((v.v.bits.data[i / 8] >> (7 - i % 8)) & 1) == gdata[i];
            if (glen % 8) { ok &= (v.v.bits.data[glen / 8] & (0xFF >> (glen % 8))) == 0; } CHECK(ok); } }
        { uint32_t fb = rnd(); float f; memcpy(&f, &fb, 4); n = bac_enc_real(buf, sizeof buf, f);
          int nan = (fb & 0x7f800000u) == 0x7f800000u && (fb & 0x7fffffu);
          if (nan) CHECK(n == -1); else { uint32_t ob; CHECK(bac_dec_app(buf, (size_t)n, &v) == 5); memcpy(&ob, &v.v.r, 4); CHECK(ob == fb); } }
        { uint16_t ty = (uint16_t)(rnd() % 1024); uint32_t in = rnd() & 0x3FFFFF; n = bac_enc_object_id(buf, sizeof buf, ty, in);
          CHECK(bac_dec_app(buf, (size_t)n, &v) == 5 && v.v.oid.type == ty && v.v.oid.instance == in); }
        n = bac_enc_date(buf, sizeof buf, gy, gm, gd, gw);
        if (n > 0) CHECK(bac_dec_app(buf, (size_t)n, &v) == 5 && v.v.date.year == gy && v.v.date.month == gm && v.v.date.day == gd && v.v.date.wday == gw);
        { uint8_t h = (uint8_t)(rnd() % 24), mi = (uint8_t)(rnd() % 60), s = (uint8_t)(rnd() % 60), hu = (uint8_t)(rnd() % 100);
          if (rnd() % 4 == 0) h = 255;
          n = bac_enc_time(buf, sizeof buf, h, mi, s, hu); CHECK(bac_dec_app(buf, (size_t)n, &v) == 5 && v.v.time.hour == h && v.v.time.minute == mi && v.v.time.second == s && v.v.time.hundredths == hu); }
    }
    /* trailing bytes are ignored */
    { const uint8_t s[] = { 0x21, 0x05, 0xAA, 0xBB }; CHECK(bac_dec_app(s, 4, &v) == 2 && v.v.u == 5); }
    printf("%s (%d failures)\n", fails ? "FAILED" : "all API tests passed", fails);
    return fails != 0;
}
