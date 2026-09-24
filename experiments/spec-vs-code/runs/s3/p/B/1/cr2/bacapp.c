/* bacapp.c - BACnet application-layer primitive encoding
 *
 * CR-102: this file uses no floating-point arithmetic, no floating-point
 * comparisons and no <math.h>, so that the FPU-less Cortex-M0 build does not
 * link the soft-float support routines. Real and Double values are handled
 * only as bit patterns, copied to and from `float`/`double` with memcpy.
 */
#include "bacapp.h"

#include <string.h>

/* The Real and Double encoders/decoders copy the object representation of
 * `float` (IEEE-754 binary32) and `double` (IEEE-754 binary64) to and from
 * the wire. */
_Static_assert(sizeof(float) == 4, "bacapp requires a 32-bit IEEE-754 float");
_Static_assert(sizeof(double) == 8, "bacapp requires a 64-bit IEEE-754 double");

/* ---- shared writers (used by every encoder) ---- */

#define CLS_APP 0x00u     /* G2: class bit = 0, application */
#define CLS_CTX 0x08u     /* G2: class bit = 1, context */
#define LVT_OPENING 6u    /* G5 */
#define LVT_CLOSING 7u    /* G5 */
#define LVT_LENGTH 0xFFu  /* not an LVT value: header carries a content length (G4) */

/* Writes v as n octets (1..4), big-endian. */
static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* put_tag - the one tag-header writer (G1..G5).
 *
 * Writes the header of tag number `tag`, class `cls` (CLS_APP / CLS_CTX)
 * into buf and returns the header length (1..7 octets); the caller then
 * writes the content octets directly after it.
 * - lvt == LVT_LENGTH: `len` content octets follow. The length goes into the
 *   LVT field or into the shortest extended-length form (G4).
 * - otherwise `lvt` is written into the LVT field as-is and no content
 *   follows (`len` is ignored): the application Boolean value (E2) or an
 *   opening/closing tag (G5).
 * Tag numbers 15..254 use the extended tag number octet (G3).
 * Returns -1 and writes nothing (G1) when buf is NULL, the tag number is 255
 * (G3), or the header plus the content does not fit into cap. */
static int put_tag(uint8_t *buf, size_t cap, uint8_t tag, uint8_t cls, uint8_t lvt, uint32_t len)
{
    uint8_t h[7];
    size_t n = 1;
    if (!buf || tag == 255)
        return -1;
    if (tag < 15) {
        h[0] = (uint8_t)(tag << 4);
    } else {
        h[0] = 0xF0;
        h[n++] = tag;
    }
    h[0] |= cls;
    if (lvt != LVT_LENGTH) {
        h[0] |= lvt;
        len = 0;
    } else if (len < 5) {
        h[0] |= (uint8_t)len;
    } else {
        h[0] |= 5;
        if (len <= 253) {
            h[n++] = (uint8_t)len;
        } else if (len <= 65535) {
            h[n++] = 254;
            put_be(h + n, len, 2);
            n += 2;
        } else {
            h[n++] = 255;
            put_be(h + n, len, 4);
            n += 4;
        }
    }
    if (cap < n || cap - n < len)
        return -1;
    memcpy(buf, h, n);
    return (int)n;
}

/* Writes one complete value: header (put_tag) and the `len` octets at
 * `content`. Returns the total length, or -1 with nothing written. */
static int put_value(uint8_t *buf, size_t cap, uint8_t tag, uint8_t cls, const uint8_t *content, uint32_t len)
{
    int h = put_tag(buf, cap, tag, cls, LVT_LENGTH, len);
    if (h < 0)
        return -1;
    if (len)
        memcpy(buf + h, content, len);
    return (int)((size_t)h + len);
}

/* put_int - the one minimal-length integer writer (E3, E4, E12).
 *
 * `v` is the 32-bit pattern of the value (two's complement if is_signed).
 * The content is the fewest big-endian octets (at least one) that still
 * represent the value: for an unsigned value the dropped high octets must be
 * zero; for a signed value they must be copies of the sign bit of the octets
 * kept, i.e. the bits from 8n-1 upward of v (or of ~v if v is negative) must
 * be zero. */
static int put_int(uint8_t *buf, size_t cap, uint8_t tag, uint8_t cls, uint32_t v, bool is_signed)
{
    uint32_t m = (is_signed && (v & 0x80000000u)) ? ~v : v;
    unsigned sign_bit = is_signed ? 1u : 0u;
    size_t n = 1;
    while (n < 4 && (m >> (8 * n - sign_bit)) != 0)
        n++;
    uint8_t c[4];
    put_be(c, v, 4);
    return put_value(buf, cap, tag, cls, c + 4 - n, (uint32_t)n);
}

/* NaN tests on the IEEE-754 bit pattern (exponent all ones, fraction not
 * zero; any sign): with the sign bit cleared, exactly the NaNs compare above
 * the pattern of +Infinity. Integer comparisons only. */
static bool is_nan32(uint32_t bits)
{
    return (bits & 0x7FFFFFFFu) > 0x7F800000u;
}

static bool is_nan64(uint64_t bits)
{
    return (bits & 0x7FFFFFFFFFFFFFFFull) > 0x7FF0000000000000ull;
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return put_tag(buf, cap, BAC_TAG_NULL, CLS_APP, LVT_LENGTH, 0);
}

/* E2: the value is carried in the LVT field, no content. */
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    return put_tag(buf, cap, BAC_TAG_BOOLEAN, CLS_APP, v ? 1u : 0u, 0);
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_UNSIGNED, CLS_APP, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return put_int(buf, cap, BAC_TAG_ENUMERATED, CLS_APP, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return put_int(buf, cap, BAC_TAG_SIGNED, CLS_APP, (uint32_t)v, true);
}

/* Real (E5): 4 octets IEEE-754 binary32, big-endian, bit-exact.
 * NaN is refused (E5a / decision D-7). */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];
    memcpy(&bits, &v, sizeof bits);
    if (is_nan32(bits))
        return -1; /* NaN (any sign / payload, quiet or signalling) */
    put_be(c, bits, 4);
    return put_value(buf, cap, BAC_TAG_REAL, CLS_APP, c, 4);
}

/* Double (E5b): header 55 08, then 8 octets IEEE-754 binary64, big-endian,
 * bit-exact. NaN is refused like for Real (E5a / decision D-7). */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    uint8_t c[8];
    memcpy(&bits, &v, sizeof bits);
    if (is_nan64(bits))
        return -1; /* NaN (any sign / payload, quiet or signalling) */
    put_be(c, (uint32_t)(bits >> 32), 4);
    put_be(c + 4, (uint32_t)bits, 4);
    return put_value(buf, cap, BAC_TAG_DOUBLE, CLS_APP, c, 8);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    return put_value(buf, cap, BAC_TAG_OCTET_STRING, CLS_APP, data, (uint32_t)len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    int h = put_tag(buf, cap, BAC_TAG_CHARACTER_STRING, CLS_APP, LVT_LENGTH, clen);
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
    int h = put_tag(buf, cap, BAC_TAG_BIT_STRING, CLS_APP, LVT_LENGTH, clen);
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
    return put_value(buf, cap, BAC_TAG_DATE, CLS_APP, c, 4);
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
    return put_value(buf, cap, BAC_TAG_TIME, CLS_APP, c, 4);
}

/* E11: always 4 octets (fixed width, not a minimal-length integer). */
int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return put_value(buf, cap, BAC_TAG_OBJECT_ID, CLS_APP, c, 4);
}

/* ---- context encoders ---- */
/* Context tag 255 is refused by put_tag (G3). */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, CLS_CTX, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return put_int(buf, cap, tag, CLS_CTX, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return put_int(buf, cap, tag, CLS_CTX, (uint32_t)v, true);
}

/* E13: length 1, content 00/01 (unlike the application Boolean). */
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    const uint8_t c[1] = { v ? 1u : 0u };
    return put_value(buf, cap, tag, CLS_CTX, c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_tag(buf, cap, tag, CLS_CTX, LVT_OPENING, 0);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return put_tag(buf, cap, tag, CLS_CTX, LVT_CLOSING, 0);
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
