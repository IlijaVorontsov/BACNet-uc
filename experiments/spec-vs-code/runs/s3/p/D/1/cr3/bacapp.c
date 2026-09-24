/* bacapp.c - BACnet application-layer primitive encoding
 *
 * CR-102 (Cortex-M0 build, no FPU): this file contains no floating-point
 * arithmetic, no floating-point comparisons and no math library, so it
 * does not pull in the soft-float support routines. float/double values are
 * only copied bit-for-bit with memcpy and handled as integers.
 * Every encoder writes its tag header through put_hdr() (via begin()) and its
 * variable-length integer content through put_int().
 */
#include "bacapp.h"

#include <float.h>
#include <string.h>

/* Real/Double (E5, E5b) are sent bit-exact and NaN (E5a) is recognised on the
 * bit pattern, which requires IEEE-754 binary32/binary64. */
_Static_assert(sizeof(float) == 4 && FLT_RADIX == 2 && FLT_MANT_DIG == 24 && FLT_MAX_EXP == 128,
               "bac_enc_real requires a 32-bit IEEE-754 float");
_Static_assert(sizeof(double) == 8 && DBL_MANT_DIG == 53 && DBL_MAX_EXP == 1024,
               "bac_enc_double requires a 64-bit IEEE-754 double");

/* ---- shared writers ---- */

/* Header kinds for put_hdr(): the class bit (bit 3) and, for opening/closing
 * tags, their fixed LVT value (G5). */
#define HDR_APP   0x00u /* application class, LVT/extended length (G4) */
#define HDR_CTX   0x08u /* context class, LVT/extended length (G4) */
#define HDR_OPEN  0x0Eu /* context class, LVT = 6 */
#define HDR_CLOSE 0x0Fu /* context class, LVT = 7 */

/* Fixed-width big-endian store of the low n (1..4) octets of v. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* The tag-header writer (G2-G5), used by every encoder.
 * Builds the header for tag number `tag` of the given kind. For HDR_APP and
 * HDR_CTX, `len` is the length field (the content length, or the value for the
 * application Boolean, E2), always in the shortest form; it is ignored for
 * opening/closing tags. Copies the header to buf unless buf is NULL and
 * returns its length (1..7 octets). */
static size_t put_hdr(uint8_t *buf, uint8_t tag, uint8_t kind, uint32_t len)
{
    uint8_t h[7];
    size_t n = 1;
    uint8_t lvt = kind & 0x07u;

    h[0] = (uint8_t)(kind & 0x08u);
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else { /* G3: extended tag number */
        h[0] |= 0xF0u;
        h[n++] = tag;
    }
    if (lvt == 0) { /* not an opening/closing tag: length per G4 */
        if (len < 5) {
            lvt = (uint8_t)len;
        } else {
            lvt = 5;
            if (len <= 253) {
                h[n++] = (uint8_t)len;
            } else if (len <= 65535u) {
                h[n++] = 254;
                put_be(h + n, len, 2);
                n += 2;
            } else {
                h[n++] = 255;
                put_be(h + n, len, 4);
                n += 4;
            }
        }
    }
    h[0] |= lvt;
    if (buf)
        memcpy(buf, h, n);
    return n;
}

/* Starts an encoding. If buf is non-NULL, tag is not 255 (G3) and the header
 * plus `clen` content octets fit into buf[0..cap), writes the header and
 * returns its length. Otherwise returns 0 and leaves buf untouched (G1).
 * `len` is the header's length field: equal to clen except for the
 * application Boolean, which carries its value in the LVT field (E2). */
static size_t begin(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t len, uint32_t clen)
{
    if (!buf || tag == 255)
        return 0;
    size_t h = put_hdr(NULL, tag, kind, len);
    if (cap < h || cap - h < clen)
        return 0;
    return put_hdr(buf, tag, kind, len);
}

/* The minimal-length integer writer (E3, E4), used by every encoder with
 * variable-length integer content.
 * Stores v big-endian in the fewest octets (1..4) that still represent it:
 * unsigned, or two's complement when is_signed (v then holds the bit pattern
 * of the int32_t). A leading octet is dropped while it only repeats the sign
 * of the remaining octets (00 for unsigned; 00/FF for signed).
 * Writes to buf unless buf is NULL and returns the number of octets. */
static size_t put_int(uint8_t *buf, uint32_t v, bool is_signed)
{
    size_t n = 4;
    while (n > 1) {
        uint8_t fill = (is_signed && ((v >> (8 * n - 9)) & 1u)) ? 0xFFu : 0x00u;
        if ((uint8_t)(v >> (8 * (n - 1))) != fill)
            break;
        n--;
    }
    if (buf)
        put_be(buf, v, n);
    return n;
}

static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind, uint32_t v, bool is_signed)
{
    uint32_t n = (uint32_t)put_int(NULL, v, is_signed);
    size_t h = begin(buf, cap, tag, kind, n, n);
    if (!h)
        return -1;
    put_int(buf + h, v, is_signed);
    return (int)(h + n);
}

/* A tag header without content octets (Null, opening/closing tags). */
static int enc_bare(uint8_t *buf, size_t cap, uint8_t tag, uint8_t kind)
{
    size_t h = begin(buf, cap, tag, kind, 0, 0);
    return h ? (int)h : -1;
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return enc_bare(buf, cap, BAC_TAG_NULL, HDR_APP);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* E2: the value is carried in the LVT field, there is no content */
    size_t h = begin(buf, cap, BAC_TAG_BOOLEAN, HDR_APP, v ? 1u : 0u, 0);
    return h ? (int)h : -1;
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
    memcpy(&bits, &v, sizeof bits);
    /* E5a: never send NaN (decision D-7). NaN = exponent all ones and a
     * non-zero fraction, any sign; +-Infinity has a zero fraction. */
    if ((bits & 0x7FFFFFFFu) > 0x7F800000u)
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_REAL, HDR_APP, 4, 4);
    if (!h)
        return -1;
    put_be(buf + h, bits, 4);
    return (int)(h + 4);
}

int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    /* E5a: never send NaN (decision D-7), tested on the bit pattern as above */
    if ((bits & 0x7FFFFFFFFFFFFFFFull) > 0x7FF0000000000000ull)
        return -1;
    /* length 8 uses the extended form: 55 08 (G4) */
    size_t h = begin(buf, cap, BAC_TAG_DOUBLE, HDR_APP, 8, 8);
    if (!h)
        return -1;
    put_be(buf + h, (uint32_t)(bits >> 32), 4);
    put_be(buf + h + 4, (uint32_t)bits, 4);
    return (int)(h + 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_OCTET_STRING, HDR_APP, (uint32_t)len, (uint32_t)len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = begin(buf, cap, BAC_TAG_CHARACTER_STRING, HDR_APP, clen, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_BIT_STRING, HDR_APP, clen, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_DATE, HDR_APP, 4, 4);
    if (!h)
        return -1;
    buf[h] = y;
    buf[h + 1] = month;
    buf[h + 2] = day;
    buf[h + 3] = wday;
    return (int)(h + 4);
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
    size_t h = begin(buf, cap, BAC_TAG_TIME, HDR_APP, 4, 4);
    if (!h)
        return -1;
    buf[h] = hour;
    buf[h + 1] = minute;
    buf[h + 2] = second;
    buf[h + 3] = hundredths;
    return (int)(h + 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_OBJECT_ID, HDR_APP, 4, 4);
    if (!h)
        return -1;
    /* always 4 octets (E11), not minimal length */
    put_be(buf + h, ((uint32_t)type << 22) | instance, 4);
    return (int)(h + 4);
}

/* ---- context encoders (tag 255 is refused by begin(), G3) ---- */

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
    /* E13: length 1, content 00/01 */
    size_t h = begin(buf, cap, tag, HDR_CTX, 1, 1);
    if (!h)
        return -1;
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_bare(buf, cap, tag, HDR_OPEN);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_bare(buf, cap, tag, HDR_CLOSE);
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
    case BAC_TAG_REAL: { /* bit-exact copy, no floating-point operation */
        if (L != 4)
            return -1;
        uint32_t bits = get_be(c, 4);
        memcpy(&out->v.r, &bits, 4);
        break;
    }
    case BAC_TAG_DOUBLE: { /* bit-exact copy, no floating-point operation */
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
