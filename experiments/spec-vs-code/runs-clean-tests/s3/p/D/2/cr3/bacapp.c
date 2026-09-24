/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <string.h>

/* SPEC P2 (CR-102): this file uses no floating-point arithmetic, no
 * floating-point comparisons and no <math.h>, so that FPU-less targets
 * (Cortex-M0) do not link the soft-float support routines.  float and double
 * values are only copied bit for bit (memcpy) to and from uint32_t/uint64_t;
 * everything else, including the NaN test of E5a, is done on those integers. */

/* SPEC P1: Real (E5/D5) and Double (E5b/D5b) are bit-exact copies of the C
 * float and double, which therefore must be IEEE-754 binary32 and binary64
 * (some small-MCU toolchains make double 32 bits wide by default). */
_Static_assert(sizeof(float) == 4, "bacapp requires a 32-bit IEEE-754 float");
_Static_assert(sizeof(double) == 8, "bacapp requires a 64-bit IEEE-754 double");

/* ---- shared writers ----
 * SPEC P3 (CR-102): no encoder has its own copy of this logic:
 *   put_tag() - writes every tag header (G2..G5), for every tag, class and
 *               form; used by all encoders through begin();
 *   put_int() - writes every minimal-length integer content (E3, E4);
 *   put_be()  - fixed-width big-endian octets (Real, Double, Object Id
 *               contents, the extended length of put_tag(), put_int()).
 * put_tag() and put_int() return the number of octets and write nothing when
 * p is NULL, so an encoder can size its complete output before it touches the
 * caller's buffer (G1, see begin()). */

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

/* put_tag() "form": the class bit of the tag octet and, for opening/closing
 * tags, their LVT value (G2, G5). */
#define TAG_APP 0x00u     /* application class, lvt = length or value */
#define TAG_CTX 0x08u     /* context class, lvt = length or value */
#define TAG_OPENING 0x0Eu /* context class, LVT 6; lvt ignored */
#define TAG_CLOSING 0x0Fu /* context class, LVT 7; lvt ignored */

/* Tag header for tag number tag (G3: >= 15 as extended tag octet) with
 * length/value/type lvt (G4: 0..4 in the tag octet, otherwise LVT 5 and the
 * shortest extended length form).  lvt is the content length, except for the
 * application Boolean, whose value it is (E2). */
static size_t put_tag(uint8_t *p, uint8_t tag, uint8_t form, uint32_t lvt)
{
    uint8_t h[7]; /* longest header: tag octet, tag number, 255, 4-octet length */
    size_t n = 1;

    h[0] = form;
    if (tag < 15) {
        h[0] |= (uint8_t)(tag << 4);
    } else {
        h[0] |= 0xF0;
        h[n++] = tag;
    }
    if ((form & 0x07) == 0) { /* not an opening/closing tag */
        if (lvt < 5) {
            h[0] |= (uint8_t)lvt;
        } else {
            size_t w = lvt <= 253 ? 0 : lvt <= 65535 ? 2 : 4;
            h[0] |= 5;
            h[n++] = w == 0 ? (uint8_t)lvt : w == 2 ? 254 : 255;
            put_be(h + n, lvt, w);
            n += w;
        }
    }
    if (p)
        memcpy(p, h, n);
    return n;
}

/* v big-endian in the minimum number of octets, at least one: as an unsigned
 * number (E3), or, when sgn, as a two's complement number (E4), where a leading
 * octet may only be dropped if it is pure sign extension (128 -> 00 80,
 * -128 -> 80, -129 -> ff 7f). */
static size_t put_int(uint8_t *p, uint32_t v, bool sgn)
{
    /* m holds the bits that must be kept; for a negative signed value these
     * are the bits of its one's complement, and a signed form needs one more
     * bit (the sign bit) than an unsigned one. */
    uint32_t m = (sgn && (v & 0x80000000u)) ? ~v : v;
    size_t s = sgn ? 1 : 0;
    size_t n = 1;
    while (n < 4 && (m >> (8 * n - s)) != 0)
        n++;
    if (p)
        put_be(p, v, n);
    return n;
}

/* Common start of every encoder.  Sizes the complete encoding - the header
 * put_tag(tag, form, lvt) followed by clen content octets - and writes the
 * header only if the tag number is valid (G3) and everything fits into
 * buf[0..cap) (G1).  Returns the header length, or 0 with buf untouched. */
static size_t begin(uint8_t *buf, size_t cap, uint8_t tag, uint8_t form, uint32_t lvt, size_t clen)
{
    if (tag == 255)
        return 0;
    size_t h = put_tag(NULL, tag, form, lvt);
    if (!buf || cap < h || cap - h < clen)
        return 0;
    return put_tag(buf, tag, form, lvt);
}

/* Unsigned / Enumerated / Signed, application or context class (E3, E4, E12, E15, E16). */
static int enc_int(uint8_t *buf, size_t cap, uint8_t tag, uint8_t form, uint32_t v, bool sgn)
{
    size_t n = put_int(NULL, v, sgn);
    size_t h = begin(buf, cap, tag, form, (uint32_t)n, n);
    if (!h)
        return -1;
    put_int(buf + h, v, sgn);
    return (int)(h + n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t h = begin(buf, cap, BAC_TAG_NULL, TAG_APP, 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* E2: the value is the LVT, there is no content */
    size_t h = begin(buf, cap, BAC_TAG_BOOLEAN, TAG_APP, v ? 1 : 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_UNSIGNED, TAG_APP, v, false);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_int(buf, cap, BAC_TAG_ENUMERATED, TAG_APP, v, false);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_int(buf, cap, BAC_TAG_SIGNED, TAG_APP, (uint32_t)v, true);
}

/* E5a without floating point: a NaN has an all-ones exponent and a non-zero
 * fraction, i.e. its bits without the sign are above those of +Infinity. */
#define REAL_INF_BITS 0x7F800000u
#define DOUBLE_INF_BITS UINT64_C(0x7FF0000000000000)

/* E5: 44 + 4 octets IEEE-754 single, big-endian. NaN refused (E5a/D-7). */
int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);
    if ((bits & 0x7FFFFFFFu) > REAL_INF_BITS)
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_REAL, TAG_APP, 4, 4);
    if (!h)
        return -1;
    put_be(buf + h, bits, 4);
    return (int)(h + 4);
}

/* E5b: 55 08 + 8 octets IEEE-754 double, big-endian. NaN refused (E5a/D-7). */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    memcpy(&bits, &v, sizeof bits);
    if ((bits & UINT64_C(0x7FFFFFFFFFFFFFFF)) > DOUBLE_INF_BITS)
        return -1;
    size_t h = begin(buf, cap, BAC_TAG_DOUBLE, TAG_APP, 8, 8);
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
    size_t h = begin(buf, cap, BAC_TAG_OCTET_STRING, TAG_APP, (uint32_t)len, len);
    if (!h)
        return -1;
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

/* E7a (CR-103): s[0..n) is well-formed UTF-8 as defined by RFC 3629, section 4:
 *   00..7F
 *   C2..DF  80..BF
 *   E0      A0..BF  80..BF          (E0 80..9F would be overlong)
 *   E1..EC  80..BF  80..BF
 *   ED      80..9F  80..BF          (ED A0..BF would be a surrogate D800..DFFF)
 *   EE..EF  80..BF  80..BF
 *   F0      90..BF  80..BF  80..BF  (F0 80..8F would be overlong)
 *   F1..F3  80..BF  80..BF  80..BF
 *   F4      80..8F  80..BF  80..BF  (F4 90..BF would be above U+10FFFF)
 * Anything else - a stray continuation octet 80..BF, the overlong lead octets
 * C0/C1, F5..FF, a missing or wrong continuation octet, a sequence cut off by
 * the end of the text - is malformed.  Only the first continuation octet has
 * a range that depends on the lead octet. */
static bool utf8_ok(const uint8_t *s, size_t n)
{
    size_t i = 0;
    while (i < n) {
        uint8_t b = s[i++];
        if (b < 0x80)
            continue;
        size_t k;                     /* number of continuation octets */
        uint8_t lo = 0x80, hi = 0xBF; /* range of the first one */
        if (b >= 0xC2 && b <= 0xDF) {
            k = 1;
        } else if (b >= 0xE0 && b <= 0xEF) {
            k = 2;
            if (b == 0xE0)
                lo = 0xA0;
            else if (b == 0xED)
                hi = 0x9F;
        } else if (b >= 0xF0 && b <= 0xF4) {
            k = 3;
            if (b == 0xF0)
                lo = 0x90;
            else if (b == 0xF4)
                hi = 0x8F;
        } else {
            return false;
        }
        if (n - i < k || s[i] < lo || s[i] > hi)
            return false;
        for (size_t j = 1; j < k; j++)
            if ((s[i + j] & 0xC0) != 0x80)
                return false;
        i += k;
    }
    return true;
}

/* E7: 7x + character set 0 (UTF-8) + the text.  The text must be well-formed
 * UTF-8 (E7a, CR-103); it is checked before anything is written (G1). */
int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    if (!utf8_ok((const uint8_t *)utf8, len))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = begin(buf, cap, BAC_TAG_CHARACTER_STRING, TAG_APP, clen, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_BIT_STRING, TAG_APP, clen, clen);
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
    size_t h = begin(buf, cap, BAC_TAG_DATE, TAG_APP, 4, 4);
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
    size_t h = begin(buf, cap, BAC_TAG_TIME, TAG_APP, 4, 4);
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
    size_t h = begin(buf, cap, BAC_TAG_OBJECT_ID, TAG_APP, 4, 4);
    if (!h)
        return -1;
    put_be(buf + h, ((uint32_t)type << 22) | instance, 4);
    return (int)(h + 4);
}

/* ---- context encoders ----
 * Context tag 255 is refused by begin() (G3). */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, TAG_CTX, v, false);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_int(buf, cap, tag, TAG_CTX, v, false);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    return enc_int(buf, cap, tag, TAG_CTX, (uint32_t)v, true);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* E13: unlike the application Boolean, length 1 and the value as content */
    size_t h = begin(buf, cap, tag, TAG_CTX, 1, 1);
    if (!h)
        return -1;
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = begin(buf, cap, tag, TAG_OPENING, 0, 0);
    return h ? (int)h : -1;
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    size_t h = begin(buf, cap, tag, TAG_CLOSING, 0, 0);
    return h ? (int)h : -1;
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
