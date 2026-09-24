/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Bare-metal C11: no heap, no stdio. Only <string.h> memory primitives are used.
 * See SPEC.md for the rule identifiers referenced below ([STD] / [POL]).
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

_Static_assert(sizeof(float) == 4, "REAL requires a 32-bit IEEE-754 float");

/* Largest possible tag header: tag octet + extended tag number + 0xFF + 4 length octets */
#define HDR_MAX 7u

#define CLASS_CONTEXT 0x08u
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u
#define TAG_RESERVED 255u

/* ---- Helpers ------------------------------------------------------------ */

static void put_be32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++) v = (v << 8) | p[i];
    return v;
}

/* Tag octet(s) without length: tag number (G2/G3) and class. tag must be <= 254.
 * Returns the number of octets written to hdr (1 or 2); the LVT bits are left 0. */
static size_t put_tag(uint8_t *hdr, uint8_t tag, bool context)
{
    uint8_t cls = context ? (uint8_t)CLASS_CONTEXT : 0u;
    if (tag <= 14u) {
        hdr[0] = (uint8_t)((unsigned)tag << 4 | cls);
        return 1;
    }
    hdr[0] = (uint8_t)(0xF0u | cls);
    hdr[1] = tag;
    return 2;
}

/* Full header for a value with content length len (G2-G4, shortest form). */
static size_t make_header(uint8_t hdr[HDR_MAX], uint8_t tag, bool context, uint32_t len)
{
    size_t n = put_tag(hdr, tag, context);
    if (len <= 4u) {
        hdr[0] |= (uint8_t)len;
    } else {
        hdr[0] |= (uint8_t)LVT_EXTENDED;
        if (len <= 253u) {
            hdr[n++] = (uint8_t)len;
        } else if (len <= 65535u) {
            hdr[n++] = 254u;
            hdr[n++] = (uint8_t)(len >> 8);
            hdr[n++] = (uint8_t)len;
        } else {
            hdr[n++] = 255u;
            put_be32(&hdr[n], len);
            n += 4;
        }
    }
    return n;
}

/* True when hl + clen bytes fit into cap and the total is representable as int. */
static bool fits(size_t cap, size_t hl, size_t clen)
{
    return hl <= cap && clen <= cap - hl && hl + clen <= (size_t)INT_MAX;
}

/* Write header + optional prefix + content. Nothing is written unless everything fits (G1).
 * content may overlap buf: it is moved into place before anything else is written. */
static int emit(uint8_t *buf, size_t cap, uint8_t tag, bool context,
                const uint8_t *pre, size_t prelen, const uint8_t *content, size_t clen)
{
    uint8_t hdr[HDR_MAX];
    size_t hl;

    if (buf == NULL || tag == TAG_RESERVED) return -1;
    if (clen != 0 && content == NULL) return -1;
    if (clen > (size_t)INT_MAX - prelen) return -1;
    hl = make_header(hdr, tag, context, (uint32_t)(prelen + clen));
    if (!fits(cap, hl, prelen + clen)) return -1;

    if (clen != 0) memmove(buf + hl + prelen, content, clen);
    if (prelen != 0) memcpy(buf + hl, pre, prelen);
    memcpy(buf, hdr, hl);
    return (int)(hl + prelen + clen);
}

/* Minimal big-endian unsigned content (E3). Returns the number of octets (1..4). */
static size_t unsigned_content(uint8_t out[4], uint32_t v)
{
    size_t n = (v <= 0xFFu) ? 1u : (v <= 0xFFFFu) ? 2u : (v <= 0xFFFFFFu) ? 3u : 4u;
    for (size_t i = 0; i < n; i++) out[i] = (uint8_t)(v >> (8u * (n - 1u - i)));
    return n;
}

/* ---- Application-tagged encoders -------------------------------------- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return emit(buf, cap, BAC_TAG_NULL, false, NULL, 0, NULL, 0); /* E1: 00 */
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* E2: value in LVT, no content */
    if (buf == NULL || cap < 1u) return -1;
    buf[0] = (uint8_t)((unsigned)BAC_TAG_BOOLEAN << 4 | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    uint8_t c[4];
    size_t n = unsigned_content(c, v);
    return emit(buf, cap, BAC_TAG_UNSIGNED, false, NULL, 0, c, n);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    uint8_t c[4];
    size_t n = unsigned_content(c, v);
    return emit(buf, cap, BAC_TAG_ENUMERATED, false, NULL, 0, c, n);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    /* E4: two's complement, minimal octets */
    uint8_t c[4];
    uint32_t u = (uint32_t)v; /* well-defined modulo 2^32 conversion */
    size_t n;
    if (v >= -128 && v <= 127) n = 1;
    else if (v >= -32768 && v <= 32767) n = 2;
    else if (v >= -8388608L && v <= 8388607L) n = 3;
    else n = 4;
    for (size_t i = 0; i < n; i++) c[i] = (uint8_t)(u >> (8u * (n - 1u - i)));
    return emit(buf, cap, BAC_TAG_SIGNED, false, NULL, 0, c, n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint8_t c[4];
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits); /* bit-exact (E5), no FP operations */
    /* E5a: refuse any NaN (exponent all ones, non-zero mantissa); +-Inf allowed */
    if ((bits & 0x7F800000u) == 0x7F800000u && (bits & 0x007FFFFFu) != 0u) return -1;
    put_be32(c, bits);
    return emit(buf, cap, BAC_TAG_REAL, false, NULL, 0, c, 4);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    return emit(buf, cap, BAC_TAG_OCTET_STRING, false, NULL, 0, data, len); /* E6 */
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    static const uint8_t charset_utf8 = 0u; /* E7: character set 0 = UTF-8 */
    return emit(buf, cap, BAC_TAG_CHARACTER_STRING, false, &charset_utf8, 1,
                (const uint8_t *)utf8, len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    uint8_t hdr[HDR_MAX];
    size_t nbytes, hl, clen;
    unsigned unused;

    if (buf == NULL) return -1;
    if (nbits != 0 && bits == NULL) return -1;
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1u) return -1; /* E8a */

    nbytes = nbits / 8u + (nbits % 8u != 0u ? 1u : 0u);
    if (nbytes > (size_t)INT_MAX - 1u) return -1;
    clen = nbytes + 1u;
    unused = (unsigned)((8u - nbits % 8u) % 8u);
    hl = make_header(hdr, BAC_TAG_BIT_STRING, false, (uint32_t)clen);
    if (!fits(cap, hl, clen)) return -1;

    memcpy(buf, hdr, hl);
    buf[hl] = (uint8_t)unused;
    for (size_t k = 0; k < nbytes; k++) {
        uint8_t octet = 0;
        size_t base = k * 8u;
        for (unsigned b = 0; b < 8u && base + b < nbits; b++)
            if (bits[base + b]) octet |= (uint8_t)(0x80u >> b);
        buf[hl + 1u + k] = octet;
    }
    return (int)(hl + clen);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t c[4];
    /* E9a */
    if (year != BAC_YEAR_UNSPECIFIED && (year < 1900u || year > 2154u)) return -1;
    if (month != 255u && (month < 1u || month > 14u)) return -1;
    if (day != 255u && (day < 1u || day > 34u)) return -1;
    if (wday != 255u && (wday < 1u || wday > 7u)) return -1;
    c[0] = (year == BAC_YEAR_UNSPECIFIED) ? 255u : (uint8_t)(year - 1900u);
    c[1] = month;
    c[2] = day;
    c[3] = wday;
    return emit(buf, cap, BAC_TAG_DATE, false, NULL, 0, c, 4);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    uint8_t c[4];
    /* E10a */
    if (hour != 255u && hour > 23u) return -1;
    if (minute != 255u && minute > 59u) return -1;
    if (second != 255u && second > 59u) return -1;
    if (hundredths != 255u && hundredths > 99u) return -1;
    c[0] = hour;
    c[1] = minute;
    c[2] = second;
    c[3] = hundredths;
    return emit(buf, cap, BAC_TAG_TIME, false, NULL, 0, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023u || instance > 4194303u) return -1; /* E11a */
    put_be32(c, (uint32_t)type << 22 | instance);
    return emit(buf, cap, BAC_TAG_OBJECT_ID, false, NULL, 0, c, 4);
}

/* ---- Context-tagged encoders ------------------------------------------ */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    uint8_t c[4];
    size_t n = unsigned_content(c, v);
    return emit(buf, cap, tag, true, NULL, 0, c, n); /* E12; tag 255 refused in emit */
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    uint8_t c = v ? 1u : 0u; /* E13: length 1, content 00/01 */
    return emit(buf, cap, tag, true, NULL, 0, &c, 1);
}

static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, unsigned lvt)
{
    uint8_t hdr[2];
    size_t hl;
    if (buf == NULL || tag == TAG_RESERVED) return -1;
    hl = put_tag(hdr, tag, true);
    if (cap < hl) return -1;
    hdr[0] |= (uint8_t)lvt;
    memcpy(buf, hdr, hl);
    return (int)hl;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_OPENING); /* E14 / G5 */
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_CLOSING); /* E14 / G5 */
}

/* ---- Decoders ------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1;
    unsigned raw_lvt;

    if (buf == NULL || out == NULL || len == 0u) return -1; /* D1a */

    t.tag = (uint8_t)(buf[0] >> 4);
    t.context = (buf[0] & CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    t.lvt = 0;
    raw_lvt = buf[0] & 0x07u;

    if (t.tag == 15u) { /* G3: extended tag number */
        if (len < 2u) return -1;
        if (buf[1] == 255u) return -1; /* reserved */
        t.tag = buf[1]; /* D1b: values below 15 accepted */
        pos = 2;
    }

    if (raw_lvt == LVT_OPENING || raw_lvt == LVT_CLOSING) {
        if (!t.context) return -1; /* D1a */
        t.opening = (raw_lvt == LVT_OPENING);
        t.closing = (raw_lvt == LVT_CLOSING);
    } else if (raw_lvt == LVT_EXTENDED) { /* G4, always extended length (D1) */
        uint8_t first;
        if (len - pos < 1u) return -1;
        first = buf[pos++];
        if (first <= 253u) {
            t.lvt = first; /* D1b: short values accepted */
        } else if (first == 254u) {
            if (len - pos < 2u) return -1;
            t.lvt = get_be(&buf[pos], 2);
            pos += 2;
        } else {
            if (len - pos < 4u) return -1;
            t.lvt = get_be(&buf[pos], 4);
            pos += 4;
        }
    } else {
        t.lvt = raw_lvt;
    }

    *out = t;
    return (int)pos;
}

/* uint32 -> int32 without implementation-defined conversion */
static int32_t to_int32(uint32_t u)
{
    if (u <= (uint32_t)INT32_MAX) return (int32_t)u;
    return -(int32_t)(~u) - 1;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t val;
    const uint8_t *c;
    size_t hl, clen;
    unsigned raw_lvt;
    int r;

    if (out == NULL) return -1;
    r = bac_dec_tag(buf, len, &t);
    if (r < 0) return -1;
    if (t.context) return -1; /* D2, includes opening/closing tags */

    hl = (size_t)r;
    raw_lvt = buf[0] & 0x07u;
    memset(&val, 0, sizeof val);
    val.tag = t.tag;

    switch (t.tag) {
    case BAC_TAG_NULL: /* D3 */
        if (raw_lvt != 0u) return -1;
        *out = val;
        return (int)hl;
    case BAC_TAG_BOOLEAN: /* D3 */
        if (raw_lvt > 1u) return -1;
        val.v.boolean = (raw_lvt == 1u);
        *out = val;
        return (int)hl;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_SIGNED:
    case BAC_TAG_REAL:
    case BAC_TAG_OCTET_STRING:
    case BAC_TAG_CHARACTER_STRING:
    case BAC_TAG_BIT_STRING:
    case BAC_TAG_ENUMERATED:
    case BAC_TAG_DATE:
    case BAC_TAG_TIME:
    case BAC_TAG_OBJECT_ID:
        break;
    default: /* D2: Double (5), 13, 14, >= 15 */
        return -1;
    }

    /* D2: content must lie within buf[0..len) and the total must fit an int */
    if (t.lvt > len - hl) return -1;
    clen = (size_t)t.lvt;
    if (clen > (size_t)INT_MAX - hl) return -1;
    c = buf + hl;

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED: /* D4 */
        if (clen < 1u || clen > 4u) return -1;
        val.v.u = get_be(c, clen);
        break;
    case BAC_TAG_SIGNED: { /* D4, sign-extended */
        uint32_t u;
        if (clen < 1u || clen > 4u) return -1;
        u = get_be(c, clen);
        if (clen < 4u && (c[0] & 0x80u) != 0u) u |= 0xFFFFFFFFu << (8u * clen);
        val.v.i = to_int32(u);
        break;
    }
    case BAC_TAG_REAL: { /* D5: bit-exact, NaN passed through */
        uint32_t bits;
        if (clen != 4u) return -1;
        bits = get_be(c, 4);
        memcpy(&val.v.r, &bits, sizeof bits);
        break;
    }
    case BAC_TAG_OCTET_STRING: /* D6 */
        val.v.octets.data = c;
        val.v.octets.len = clen;
        break;
    case BAC_TAG_CHARACTER_STRING: /* D7 */
        if (clen < 1u) return -1;
        val.v.str.charset = c[0];
        val.v.str.data = c + 1;
        val.v.str.len = clen - 1u;
        break;
    case BAC_TAG_BIT_STRING: { /* D8 */
        unsigned unused;
        if (clen < 1u) return -1;
        unused = c[0];
        if (unused > 7u) return -1;
        if (clen == 1u && unused != 0u) return -1;
        val.v.bits.data = c + 1;
        val.v.bits.nbits = (clen - 1u) * 8u - unused;
        break;
    }
    case BAC_TAG_DATE: /* D9: no field validation */
        if (clen != 4u) return -1;
        val.v.date.year = (c[0] == 255u) ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        val.v.date.month = c[1];
        val.v.date.day = c[2];
        val.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME: /* D9 */
        if (clen != 4u) return -1;
        val.v.time.hour = c[0];
        val.v.time.minute = c[1];
        val.v.time.second = c[2];
        val.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: { /* D9 */
        uint32_t v;
        if (clen != 4u) return -1;
        v = get_be(c, 4);
        val.v.oid.type = (uint16_t)(v >> 22);
        val.v.oid.instance = v & 0x3FFFFFu;
        break;
    }
    default:
        return -1;
    }

    memcpy(out, &val, sizeof val); /* bit-exact copy (keeps signalling NaN payloads) */
    return (int)(hl + clen);
}
