/* Developer test: in-place encoding (data aliases buf) and NULL-argument handling. */
#include "bacapp.h"
#include <stdio.h>
#include <string.h>
int main(void) {
    uint8_t b[400]; int fails = 0; bac_tag_t t; bac_value_t v;
    for (size_t n = 0; n < 300; n += 7) {
        for (size_t i = 0; i < n; i++) b[i] = (uint8_t)(i * 13 + 1);
        int r = bac_enc_octet_string(b, sizeof b, b, n);
        int d = bac_dec_app(b, (size_t)r, &v);
        if (d != r || v.v.octets.len != n) { fails++; continue; }
        for (size_t i = 0; i < n; i++) if (v.v.octets.data[i] != (uint8_t)(i * 13 + 1)) { fails++; break; }
        for (size_t i = 0; i < n; i++) b[i] = 'a' + (uint8_t)(i % 26);
        r = bac_enc_char_string(b, sizeof b, (const char *)b, n);
        d = bac_dec_app(b, (size_t)r, &v);
        if (d != r || v.v.str.len != n || v.v.str.charset != 0) { fails++; continue; }
        for (size_t i = 0; i < n; i++) if (v.v.str.data[i] != 'a' + i % 26) { fails++; break; }
    }
    if (bac_enc_null(NULL, 10) != -1) fails++;
    if (bac_enc_octet_string(b, 10, NULL, 1) != -1) fails++;
    if (bac_enc_octet_string(b, 10, NULL, 0) != 1) fails++;
    if (bac_enc_char_string(b, 10, NULL, 0) != 2) fails++;
    if (bac_enc_bit_string(b, 10, NULL, 0) != 2) fails++;
    if (bac_enc_bit_string(b, 10, NULL, 3) != -1) fails++;
    if (bac_dec_tag(NULL, 3, &t) != -1 || bac_dec_tag(b, 3, NULL) != -1) fails++;
    if (bac_dec_app(NULL, 3, &v) != -1 || bac_dec_app(b, 3, NULL) != -1) fails++;
    /* *out untouched on error */
    memset(&v, 0x77, sizeof v); b[0] = 0x44;
    if (bac_dec_app(b, 3, &v) != -1 || ((uint8_t *)&v)[0] != 0x77) fails++;
    printf("%s (%d failures)\n", fails ? "FAIL" : "PASS", fails);
    return fails != 0;
}
