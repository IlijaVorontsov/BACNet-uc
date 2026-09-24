/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/* CR-102: this file performs no floating-point arithmetic or comparisons and
 * does not use <math.h>, so FPU-less targets (Cortex-M0) link no soft-float
 * support code. Real and Double values are only ever handled as IEEE-754 bit
 * patterns, copied between the float/double and an integer with memcpy.
 *
 * E5/E15/D5/D10: Real is IEEE-754 binary32 and Double is binary64. Some
 * bare-metal toolchains make `double` a 32-bit type; refuse to build there
 * instead of emitting garbage. */
_Static_assert(sizeof(float) == 4 && sizeof(uint32_t) == 4,
               "bacapp: Real encoding requires a 32-bit IEEE-754 float");
_Static_assert(sizeof(double) == 8 && sizeof(uint64_t) == 8,
               "bacapp: Double encoding requires a 64-bit IEEE-754 double");

/* Magnitude (all bits except the sign) of +Infinity. A bit pattern is a NaN
 * exactly when its magnitude is greater: exponent all ones, fraction != 0. */
#define REAL_INF_BITS   0x7F800000u
#define REAL_MAG_MASK   0x7FFFFFFFu
#define DOUBLE_INF_BITS 0x7FF0000000000000ull
#define DOUBLE_MAG_MASK 0x7FFFFFFFFFFFFFFFull

/* Fixed-width big-endian store of the low n (1..4) octets of v. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* ---- shared encoder back end ------------------------------------------------
 * CR-102: every encoder produces its output through the two writers below and
 * nothing else writes a tag header or a minimal-length integer:
 *   put_hdr()     - the tag-header writer (G2..G5), which is also the single
 *                   G1 capacity check;
 *   put_min_int() - the minimal-length integer writer (E3, E4).
 */

/* `form` argument of put_hdr() */
#define HDR_APP 0x00u /* application class (G2 class bit 0) */
#define HDR_CTX 0x08u /* context class (G2 class bit 1) */
#define HDR_RAW 0x01u /* LVT bits = lvt as-is, no content octets follow: the
                         Boolean value (E2), opening/closing tags (G5). Without
                         it, lvt is the content length, encoded per G4. */

#define LVT_OPENING 6u /* G5 */
#define LVT_CLOSING 7u

#define HDR_MAX 7u /* tag octet + extended tag number + 255 + 4 length octets */

/* The tag-header writer. Builds the header for tag number `tag` (G2, G3;
 * lengths in the shortest form, G4) and writes it to buf only if the header
 * plus the content (lvt octets, or none with HDR_RAW) fits into cap. Returns
 * the header length, or 0 when nothing was written: buf NULL, the reserved
 * tag number 255 (G3), or the value does not fit (G1).
 * Callers validate their arguments before calling it. Once it has succeeded,
 * writing the content cannot fail, so an encoder never leaves a partial
 * value in buf (G1). */
static size_t put_hdr(uint8_t *buf, size_t cap, uint8_t tag, unsigned form, uint32_t lvt)
{
    uint8_t h[HDR_MAX];
    size_t n = 1;
    uint32_t content = 0;

    if (tag == 255)
        return 0;
    h[0] = (uint8_t)(form & HDR_CTX);
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0;
        h[n++] = tag;
    }
    if (form & HDR_RAW) {
        h[0] |= (uint8_t)(lvt & 0x07);
    } else {
        content = lvt;
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
    if (!buf || cap < n || cap - n < content)
        return 0;
    memcpy(buf, h, n);
    return n;
}

/* The minimal-length integer writer. Stores v big-endian in the fewest octets,
 * at least one, into c[0..n) and returns n.
 * sgn = false: v is unsigned (E3), n is the fewest octets that hold v.
 * sgn = true:  v is the two's complement of a Signed value (E4), n is the
 * fewest octets whose sign extension gives back v. A signed value fits into n
 * octets exactly when its magnitude bits (v, or ~v when negative) followed by
 * one sign bit fit into n octets as an unsigned number. */
static uint32_t put_min_int(uint8_t c[4], uint32_t v, bool sgn)
{
    uint32_t m = v;
    uint32_t n = 1;
    if (sgn)
        m = ((v & 0x80000000u) ? ~v : v) << 1;
    while (n < 4 && (m >> (8 * n)) != 0)
        n++;
    put_be(c, v, n);
    return n;
}

/* Header + n content octets copied from c. */
static int put_value(uint8_t *buf, size_t cap, uint8_t tag, unsigned form,
                     const uint8_t *c, uint32_t n)
{
    size_t h = put_hdr(buf, cap, tag, form, n);
    if (!h)
        return -1;
    if (n)
        memcpy(buf + h, c, n);
    return (int)(h + n);
}

/* A value that is a header only (HDR_RAW): Boolean, opening/closing tags. */
static int put_raw(uint8_t *buf, size_t cap, uint8_t tag, unsigned cls, uint32_t lvt)
{
    size_t h = put_hdr(buf, cap, tag, cls | HDR_RAW, lvt);
    return h ? (int)h : -1;
}

/* Unsigned / Enumerated / Signed content (E3, E4, E12, E16). */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, unsigned cls, uint32_t v, bool sgn)
{
    uint8_t c[4];
    uint32_t n = put_min_int(c, v, sgn);
    return put_value(buf, cap, tag, cls, c, n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return put_value(buf, cap, BAC_TAG_NULL, HDR_APP, NULL, 0);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return put_raw(buf, cap, BAC_TAG_BOOLEAN, HDR_APP, v ? 1 : 0);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_UNSIGNED, HDR_APP, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_ENUMERATED, HDR_APP, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_int(buf, cap, BAC_TAG_SIGNED, HDR_APP, (uint32_t)v, true);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits);
    /* E5a: refuse every NaN (quiet or signalling, any sign/payload). */
    if ((bits & REAL_MAG_MASK) > REAL_INF_BITS)
        return -1;
    put_be(c, bits, 4);
    return put_value(buf, cap, BAC_TAG_REAL, HDR_APP, c, 4);
}

int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    uint8_t c[8];
    memcpy(&bits, &v, sizeof bits);
    /* E5a applies to Double too (decision D-7): refuse every NaN (quiet or
     * signalling, any sign/payload). ±Infinity is encoded. */
    if ((bits & DOUBLE_MAG_MASK) > DOUBLE_INF_BITS)
        return -1;
    put_be(c, (uint32_t)(bits >> 32), 4);
    put_be(c + 4, (uint32_t)bits, 4);
    /* application tag 5, LVT = 5 (extended length), length octet 8 */
    return put_value(buf, cap, BAC_TAG_DOUBLE, HDR_APP, c, 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    return put_value(buf, cap, BAC_TAG_OCTET_STRING, HDR_APP, data, (uint32_t)len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = put_hdr(buf, cap, BAC_TAG_CHARACTER_STRING, HDR_APP, clen);
    if (!h)
        return -1;
    buf[h] = 0x00; /* character set: UTF-8 */
    if (len)
        memcpy(buf + h + 1, utf8, len);
    return (int)(h + clen);
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
    size_t h = put_hdr(buf, cap, BAC_TAG_BIT_STRING, HDR_APP, clen);
    if (!h)
        return -1;
    buf[h] = (uint8_t)(nbytes * 8 - nbits);
    uint8_t *p = buf + h + 1;
    memset(p, 0, nbytes);
    for (size_t i = 0; i < nbits; i++)
        if (bits[i])
            p[i / 8] |= (uint8_t)(0x80 >> (i % 8));
    return (int)(h + clen);
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
    return put_value(buf, cap, BAC_TAG_DATE, HDR_APP, c, 4);
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
    return put_value(buf, cap, BAC_TAG_TIME, HDR_APP, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return put_value(buf, cap, BAC_TAG_OBJECT_ID, HDR_APP, c, 4);
}

/* ---- context encoders ----
 * Context tag 255 is refused by put_hdr() (G3). */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return enc_int(buf, cap, tag, HDR_CTX, (uint32_t)v, true);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* E13: length 1, content 00/01 - the minimal unsigned encoding of 0/1. */
    return enc_int(buf, cap, tag, HDR_CTX, v ? 1 : 0, false);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_raw(buf, cap, tag, HDR_CTX, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_raw(buf, cap, tag, HDR_CTX, LVT_CLOSING);
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
