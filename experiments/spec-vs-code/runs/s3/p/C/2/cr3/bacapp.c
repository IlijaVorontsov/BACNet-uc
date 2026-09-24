/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/* No floating-point arithmetic, comparisons or <math.h> in this module (CR-102):
 * REAL and Double values are handled only as IEEE-754 bit patterns, obtained
 * with memcpy, so no soft-float support routines are linked on FPU-less targets. */

/* IEEE-754 binary32 / binary64 (clauses 20.2.6, 20.2.7). */
_Static_assert(sizeof(float) == 4, "BACnet REAL requires 32-bit IEEE-754 float");
_Static_assert(sizeof(double) == 8, "BACnet Double requires 64-bit IEEE-754 double");

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* Well-formed UTF-8 (RFC 3629, character set X'00' of clause 20.2.9). Rejects
 * truncated sequences, stray continuation octets, the lead octets C0, C1 and
 * F5..FF, overlong forms (E0 80..9F, F0 80..8F), surrogates U+D800..U+DFFF
 * (ED A0..BF) and code points above U+10FFFF (F4 90..BF). */
static bool utf8_valid(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t b = s[i++];
        uint8_t lo = 0x80, hi = 0xBF; /* permitted range of the second octet */
        size_t n;                     /* number of continuation octets */
        if (b < 0x80)
            continue;
        if (b >= 0xC2 && b <= 0xDF) {
            n = 1;
        } else if (b >= 0xE0 && b <= 0xEF) {
            n = 2;
            if (b == 0xE0)
                lo = 0xA0; /* overlong: below U+0800 */
            else if (b == 0xED)
                hi = 0x9F; /* surrogates U+D800..U+DFFF */
        } else if (b >= 0xF0 && b <= 0xF4) {
            n = 3;
            if (b == 0xF0)
                lo = 0x90; /* overlong: below U+10000 */
            else if (b == 0xF4)
                hi = 0x8F; /* above U+10FFFF */
        } else {
            return false; /* 80..BF continuation, C0/C1 overlong, F5..FF */
        }
        if (len - i < n || s[i] < lo || s[i] > hi)
            return false;
        for (size_t k = 1; k < n; k++)
            if ((s[i + k] & 0xC0) != 0x80)
                return false;
        i += n;
    }
    return true;
}

/* ---- the tag-header writer (clause 20.2.1) ---- */

/* kind: class bit, plus the LVT value for opening/closing tags */
#define K_APP   0x00u /* application tag */
#define K_CTX   0x08u /* context tag */
#define K_OPEN  0x0Eu /* context opening tag (LVT 6) */
#define K_CLOSE 0x0Fu /* context closing tag (LVT 7) */

/* Writes the tag header for tag number `tag` into buf, but only if the header
 * plus `clen` content octets fit into cap. `lvt` is the length of the content
 * (for an application Boolean: the value); it is ignored for opening/closing
 * tags. Tag numbers >= 15 use the extended tag number octet; lengths >= 5 use
 * the extended length (1, 3 or 5 octets). Returns the header length, or -1
 * with nothing written if buf is NULL, the tag number is 255 (reserved) or the
 * encoding does not fit. This is the only place that forms a tag header. */
static int put_tag(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t lvt, size_t clen)
{
    uint8_t h[7];
    size_t n = 1;
    if (!buf || tag == 255)
        return -1;
    h[0] = kind;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0;
        h[n++] = tag;
    }
    if (kind == K_APP || kind == K_CTX) {
        if (lvt < 5) {
            h[0] |= (uint8_t)lvt;
        } else {
            h[0] |= 5;
            if (lvt <= 253) {
                h[n++] = (uint8_t)lvt;
            } else if (lvt <= 65535) {
                h[n++] = 254;
                put_be(h + n, lvt, 2);
                n += 2;
            } else {
                h[n++] = 255;
                put_be(h + n, lvt, 4);
                n += 4;
            }
        }
    }
    if (cap < n || cap - n < clen)
        return -1;
    memcpy(buf, h, n);
    return (int)n;
}

/* Tag header followed by n content octets c[0..n). Returns the total length or -1. */
static int put_prim(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, const uint8_t *c, size_t n)
{
    int h = put_tag(buf, cap, tag, kind, (uint32_t)n, n);
    if (h < 0)
        return -1;
    if (n)
        memcpy(buf + h, c, n);
    return (int)((size_t)h + n);
}

/* ---- the minimal-length integer writer (clauses 20.2.4, 20.2.5, 20.2.11) ---- */

/* Tag plus v in the fewest octets (1..4), most significant octet first. Used by
 * every Unsigned, Signed and Enumerated encoder (application and context).
 * sgn: v is an int32_t in two's complement. A negative value is folded onto
 * ~v, and the magnitude is shifted left once so the sign bit must fit too. */
static int put_int(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t v, bool sgn)
{
    uint8_t c[4];
    uint32_t m = sgn ? ((v & 0x80000000u) ? ~v : v) << 1 : v;
    size_t n = 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    put_be(c, v, n);
    return put_prim(buf, cap, tag, kind, c, n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return put_tag(buf, cap, BAC_TAG_NULL, K_APP, 0, 0);
}

/* clause 20.2.3: the value is carried in the LVT field, no content octets */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return put_tag(buf, cap, BAC_TAG_BOOLEAN, K_APP, v ? 1 : 0, 0);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_UNSIGNED, K_APP, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_ENUMERATED, K_APP, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return put_int(buf, cap, BAC_TAG_SIGNED, K_APP, (uint32_t)v, true);
}

/* REAL (clause 20.2.6): 44 xx*4, the IEEE-754 single-precision bit pattern,
 * most significant octet first. Every value is encoded as given, including NaN
 * of either sign and any payload, infinities, signed zero and subnormals
 * (CR-103). */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits);
    put_be(c, bits, 4);
    return put_prim(buf, cap, BAC_TAG_REAL, K_APP, c, 4);
}

/* Double (clause 20.2.7): tag 5 with extended length 8, then the IEEE-754
 * double-precision value, most significant octet first: 55 08 xx*8.
 * Every value is encoded as given, including NaN, as for bac_enc_real. */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    uint8_t c[8];
    memcpy(&bits, &v, sizeof bits);
    put_be(c, (uint32_t)(bits >> 32), 4);
    put_be(c + 4, (uint32_t)bits, 4);
    return put_prim(buf, cap, BAC_TAG_DOUBLE, K_APP, c, 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    return put_prim(buf, cap, BAC_TAG_OCTET_STRING, K_APP, data, len);
}

/* Character String (clause 20.2.9), character set X'00' (UTF-8). The text must
 * be well-formed UTF-8 (CR-103); anything else gives -1 with nothing written. */
int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8) || !utf8_valid((const uint8_t *)utf8, len))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    int h = put_tag(buf, cap, BAC_TAG_CHARACTER_STRING, K_APP, clen, clen);
    if (h < 0)
        return -1;
    buf[h] = 0x00; /* character set: UTF-8 */
    if (len)
        memcpy(buf + h + 1, utf8, len);
    return (int)((size_t)h + clen);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    if (nbits && !bits)
        return -1;
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1)
            return -1;
    size_t nbytes = (nbits + 7) / 8;
    if (nbytes >= 0xFFFFFFFFu)
        return -1;
    uint32_t clen = (uint32_t)nbytes + 1;
    int h = put_tag(buf, cap, BAC_TAG_BIT_STRING, K_APP, clen, clen);
    if (h < 0)
        return -1;
    buf[h] = (uint8_t)(nbytes * 8 - nbits);
    uint8_t *p = buf + h + 1;
    memset(p, 0, nbytes);
    for (size_t i = 0; i < nbits; i++)
        if (bits[i])
            p[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (int)((size_t)h + clen);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;
    if (year == BAC_YEAR_UNSPECIFIED)
        y = 255;
    else if (year >= 1900 && year <= 2154)
        y = (uint8_t)(year - 1900);
    else
        return -1;
    if (!((month >= 1 && month <= 14) || month == 255))
        return -1;
    if (!((day >= 1 && day <= 34) || day == 255))
        return -1;
    if (!((wday >= 1 && wday <= 7) || wday == 255))
        return -1;
    const uint8_t c[4] = { y, month, day, wday };
    return put_prim(buf, cap, BAC_TAG_DATE, K_APP, c, 4);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    if (!(hour <= 23 || hour == 255))
        return -1;
    if (!(minute <= 59 || minute == 255))
        return -1;
    if (!(second <= 59 || second == 255))
        return -1;
    if (!(hundredths <= 99 || hundredths == 255))
        return -1;
    const uint8_t c[4] = { hour, minute, second, hundredths };
    return put_prim(buf, cap, BAC_TAG_TIME, K_APP, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return put_prim(buf, cap, BAC_TAG_OBJECT_ID, K_APP, c, 4);
}

/* ---- context encoders (tag number 255 is rejected by put_tag) ---- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, K_CTX, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, K_CTX, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return put_int(buf, cap, tag, K_CTX, (uint32_t)v, true);
}

/* clause 20.2.3: a context-tagged Boolean has one content octet, 0 or 1 */
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    const uint8_t c[1] = { v ? 1 : 0 };
    return put_prim(buf, cap, tag, K_CTX, c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_tag(buf, cap, tag, K_OPEN, 0, 0);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_tag(buf, cap, tag, K_CLOSE, 0, 0);
}

/* ---- decoders ---- */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    if (!buf || !out || len < 1)
        return -1;
    size_t i = 1;
    uint8_t b = buf[0];
    uint8_t tag = (uint8_t)(b >> 4);
    bool ctx = (b & 0x08) != 0;
    uint8_t lvt = b & 0x07;
    if (tag == 15) {
        if (len < 2 || buf[1] == 255)
            return -1;
        tag = buf[1];
        i = 2;
    }
    out->tag = tag;
    out->context = ctx;
    out->opening = false;
    out->closing = false;
    out->lvt = 0;
    if (lvt == 6 || lvt == 7) {
        if (!ctx)
            return -1;
        out->opening = (lvt == 6);
        out->closing = (lvt == 7);
        return (int)i;
    }
    if (lvt < 5) {
        out->lvt = lvt;
        return (int)i;
    }
    if (len < i + 1)
        return -1;
    uint8_t e = buf[i++];
    if (e < 254) {
        out->lvt = e;
    } else if (e == 254) {
        if (len < i + 2)
            return -1;
        out->lvt = ((uint32_t)buf[i] << 8) | buf[i + 1];
        i += 2;
    } else {
        if (len < i + 4)
            return -1;
        out->lvt = ((uint32_t)buf[i] << 24) | ((uint32_t)buf[i + 1] << 16) | ((uint32_t)buf[i + 2] << 8) | buf[i + 3];
        i += 4;
    }
    return (int)i;
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++)
        v = (v << 8) | p[i];
    return v;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    int hr = bac_dec_tag(buf, len, &t);
    if (hr < 0 || !out)
        return -1;
    size_t h = (size_t)hr;
    if (t.context || t.opening || t.closing)
        return -1;
    uint8_t raw = buf[0] & 0x07;
    uint32_t L = t.lvt;

    if (t.tag == BAC_TAG_NULL) {
        if (raw != 0)
            return -1;
        out->tag = BAC_TAG_NULL;
        return (int)h;
    }
    if (t.tag == BAC_TAG_BOOLEAN) {
        if (raw > 1)
            return -1;
        out->tag = BAC_TAG_BOOLEAN;
        out->v.boolean = raw == 1;
        return (int)h;
    }
    if (t.tag > BAC_TAG_OBJECT_ID)
        return -1;
    if (L > len - h)
        return -1;
    const uint8_t *c = buf + h;

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (L < 1 || L > 4)
            return -1;
        out->v.u = get_be(c, L);
        break;
    case BAC_TAG_SIGNED: {
        if (L < 1 || L > 4)
            return -1;
        uint32_t u = get_be(c, L);
        if (L < 4 && (c[0] & 0x80))
            u |= 0xFFFFFFFFu << (8 * L);
        out->v.i = (int32_t)u;
        break;
    }
    case BAC_TAG_REAL: {
        if (L != 4)
            return -1;
        uint32_t bits = get_be(c, 4);
        memcpy(&out->v.r, &bits, 4);
        break;
    }
    case BAC_TAG_DOUBLE: {
        if (L != 8)
            return -1;
        uint64_t bits = ((uint64_t)get_be(c, 4) << 32) | get_be(c + 4, 4);
        memcpy(&out->v.d, &bits, 8);
        break;
    }
    case BAC_TAG_OCTET_STRING:
        out->v.octets.data = c;
        out->v.octets.len = L;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (L < 1)
            return -1;
        out->v.str.charset = c[0];
        out->v.str.data = c + 1;
        out->v.str.len = L - 1;
        break;
    case BAC_TAG_BIT_STRING:
        if (L < 1 || c[0] > 7 || (L == 1 && c[0] != 0))
            return -1;
        out->v.bits.data = c + 1;
        out->v.bits.nbits = (size_t)(L - 1) * 8 - c[0];
        break;
    case BAC_TAG_DATE:
        if (L != 4)
            return -1;
        out->v.date.year = c[0] == 255 ? BAC_YEAR_UNSPECIFIED : (uint16_t)(1900 + c[0]);
        out->v.date.month = c[1];
        out->v.date.day = c[2];
        out->v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (L != 4)
            return -1;
        out->v.time.hour = c[0];
        out->v.time.minute = c[1];
        out->v.time.second = c[2];
        out->v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        if (L != 4)
            return -1;
        uint32_t v = get_be(c, 4);
        out->v.oid.type = (uint16_t)(v >> 22);
        out->v.oid.instance = v & 0x3FFFFFu;
        break;
    }
    default:
        return -1;
    }
    out->tag = t.tag;
    return (int)(h + L);
}
