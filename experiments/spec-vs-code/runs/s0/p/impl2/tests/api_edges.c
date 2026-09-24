/* API edge cases the CLI driver cannot reach (NULL pointers, huge sizes,
 * out-params on failure, bit values >= 10, overlapping buffers). */
#include "../bacapp.h"
#include <limits.h>
#include <stdio.h>
#include <string.h>
static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); fails++; } } while (0)
int main(void) {
    uint8_t buf[64], d[8] = {1, 2, 3};
    memset(buf, 0xA5, sizeof buf);
    CHECK(bac_enc_null(NULL, 10) == -1);
    CHECK(bac_enc_boolean(NULL, 10, true) == -1);
    CHECK(bac_enc_opening_tag(NULL, 10, 1) == -1);
    CHECK(bac_enc_octet_string(buf, 64, NULL, 3) == -1);
    CHECK(bac_enc_octet_string(buf, 64, NULL, 0) == 1 && buf[0] == 0x60);
    CHECK(bac_enc_char_string(buf, 64, NULL, 1) == -1);
    CHECK(bac_enc_bit_string(buf, 64, NULL, 1) == -1);
    CHECK(bac_enc_bit_string(buf, 64, NULL, 0) == 2);
    /* huge lengths must fail without touching data or buf */
    memset(buf, 0xA5, sizeof buf);
    CHECK(bac_enc_octet_string(buf, SIZE_MAX, d, SIZE_MAX) == -1);
    CHECK(bac_enc_octet_string(buf, SIZE_MAX, d, (size_t)INT_MAX) == -1);
    CHECK(bac_enc_char_string(buf, SIZE_MAX, (const char *)d, SIZE_MAX) == -1);
    CHECK(bac_enc_bit_string(buf, SIZE_MAX, d, SIZE_MAX) == -1);
    for (size_t i = 0; i < sizeof buf; i++) CHECK(buf[i] == 0xA5);
    /* E8a: any bits[i] other than 0/1 */
    uint8_t bm[3] = {1, 0x80, 0};
    CHECK(bac_enc_bit_string(buf, 64, bm, 3) == -1);
    bm[1] = 0xFF; CHECK(bac_enc_bit_string(buf, 64, bm, 3) == -1);
    for (size_t i = 0; i < sizeof buf; i++) CHECK(buf[i] == 0xA5);
    /* decoders: NULL args, out untouched on failure */
    bac_tag_t t; memset(&t, 0x77, sizeof t);
    CHECK(bac_dec_tag(NULL, 5, &t) == -1);
    CHECK(bac_dec_tag(d, 1, NULL) == -1);
    uint8_t bad[] = {0x65, 0xFE, 0x00};
    CHECK(bac_dec_tag(bad, 3, &t) == -1);
    { bac_tag_t z; memset(&z, 0x77, sizeof z); CHECK(memcmp(&t, &z, sizeof t) == 0); }
    bac_value_t v; memset(&v, 0x77, sizeof v);
    CHECK(bac_dec_app(d, 3, NULL) == -1);
    CHECK(bac_dec_app(NULL, 3, &v) == -1);
    uint8_t bad2[] = {0x22, 0x01};
    CHECK(bac_dec_app(bad2, 2, &v) == -1);
    { bac_value_t z; memset(&z, 0x77, sizeof z); CHECK(memcmp(&v, &z, sizeof v) == 0); }
    /* pointers into buf */
    uint8_t s[] = {0x73, 0x00, 'h', 'i', 0xEE};
    CHECK(bac_dec_app(s, sizeof s, &v) == 4 && v.v.str.data == s + 2 && v.v.str.len == 2);
    /* overlapping source/destination for strings */
    uint8_t ov[16] = {'a', 'b', 'c', 'd', 'e', 'f'};
    CHECK(bac_enc_char_string(ov, sizeof ov, (const char *)ov, 6) == 9);
    CHECK(memcmp(ov, "\x75\x07\x00" "abcdef", 9) == 0 || (printf("got %02x %02x\n", ov[0], ov[1]), 0));
    uint8_t ov2[16] = {0, 0, 0, 'x', 'y', 'z'};
    CHECK(bac_enc_octet_string(ov2, sizeof ov2, ov2 + 3, 3) == 4 && memcmp(ov2, "\x63xyz", 4) == 0);
    /* real: sNaN, qNaN, negative NaN via float argument; infinities OK */
    union { uint32_t u; float f; } p;
    uint32_t nans[] = {0x7F800001u, 0x7FC00000u, 0xFFFFFFFFu, 0xFF800001u, 0x7FBFFFFFu};
    for (int i = 0; i < 5; i++) { p.u = nans[i]; CHECK(bac_enc_real(buf, 64, p.f) == -1); }
    p.u = 0xFF800000u; CHECK(bac_enc_real(buf, 64, p.f) == 5 && buf[1] == 0xFF && buf[2] == 0x80);
    /* round trip every tag number for context unsigned header */
    for (unsigned tag = 0; tag < 255; tag++) {
        int n = bac_enc_ctx_unsigned(buf, 64, (uint8_t)tag, 70000);
        CHECK(n == (tag >= 15 ? 5 : 4));
        CHECK(bac_dec_tag(buf, (size_t)n, &t) == (tag >= 15 ? 2 : 1) && t.tag == tag && t.context && t.lvt == 3);
        CHECK(bac_enc_opening_tag(buf, 64, (uint8_t)tag) > 0 && bac_dec_tag(buf, 2, &t) > 0 && t.opening && !t.closing && t.tag == tag);
        CHECK(bac_enc_closing_tag(buf, 64, (uint8_t)tag) > 0 && bac_dec_tag(buf, 2, &t) > 0 && t.closing && !t.opening && t.tag == tag);
    }
    /* signed round trip sweep */
    for (int64_t x = INT32_MIN; x <= INT32_MAX; x += 65521) {
        int n = bac_enc_signed(buf, 64, (int32_t)x);
        CHECK(n > 0 && bac_dec_app(buf, (size_t)n, &v) == n && v.v.i == (int32_t)x);
    }
    printf(fails ? "api_edges: %d failures\n" : "api_edges: all ok\n", fails);
    return fails != 0;
}
