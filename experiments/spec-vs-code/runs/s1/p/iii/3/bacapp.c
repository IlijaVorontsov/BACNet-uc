/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Bare-metal friendly: C11, no heap, no stdio.  See SPEC.md for the rules
 * referenced below (G*, E*, D*).
 *
 * Every encoder first validates its arguments and computes the complete
 * encoded length; only when everything is valid and fits into cap does it
 * write to buf (rule G1: no partial writes).
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

_Static_assert(sizeof(float) == 4, "bacapp requires a 32-bit IEEE-754 float");

#define TAG_RESERVED 255u   /* G3: tag number 255 is reserved */
#define CLASS_CONTEXT 0x08u /* G2: bit 3 */
#define LVT_EXTENDED 5u     /* G4 */
#define LVT_OPENING 6u      /* G5 */
#define LVT_CLOSING 7u      /* G5 */

/* Largest content length we accept; keeps header + content within int. */
#define MAX_CONTENT ((size_t)INT_MAX - 8u)

/* ---- Header helpers ------------------------------------------------------ */

/* Length of a tag header (tag octet, extended tag octet, extended length). */
static size_t hdr_len(uint8_t tag, size_t content_len)
{
    size_t n = 1u;
    if (tag >= 15u) {
        n += 1u; /* G3 */
    }
    if (content_len > 4u) { /* G4, shortest form */
        if (content_len <= 253u) {
            n += 1u;
        } else if (content_len <= 65535u) {
            n += 3u;
        } else {
            n += 5u;
        }
    }
    return n;
}

/* Total encoded length for a value with the given header and content length,
 * or 0 when it is not representable or does not fit into cap. */
static size_t fit_len(uint8_t tag, size_t content_len, size_t cap)
{
    size_t total;
    if (content_len > MAX_CONTENT) {
        return 0u;
    }
    total = hdr_len(tag, content_len) + content_len;
    if (total > cap) {
        return 0u;
    }
    return total;
}

/* Write a data (non opening/closing) tag header. Caller has checked space. */
static size_t put_hdr(uint8_t *buf, uint8_t tag, bool context, size_t content_len)
{
    size_t n = 1u;
    uint8_t b0 = context ? (uint8_t)CLASS_CONTEXT : (uint8_t)0u;

    if (tag >= 15u) {
        b0 |= 0xF0u;
        buf[n++] = tag;
    } else {
        b0 |= (uint8_t)((unsigned)tag << 4);
    }

    if (content_len <= 4u) {
        b0 |= (uint8_t)content_len;
    } else {
        b0 |= (uint8_t)LVT_EXTENDED;
        if (content_len <= 253u) {
            buf[n++] = (uint8_t)content_len;
        } else if (content_len <= 65535u) {
            buf[n++] = 254u;
            buf[n++] = (uint8_t)(content_len >> 8);
            buf[n++] = (uint8_t)content_len;
        } else {
            uint32_t l = (uint32_t)content_len; /* <= MAX_CONTENT */
            buf[n++] = 255u;
            buf[n++] = (uint8_t)(l >> 24);
            buf[n++] = (uint8_t)(l >> 16);
            buf[n++] = (uint8_t)(l >> 8);
            buf[n++] = (uint8_t)l;
        }
    }
    buf[0] = b0;
    return n;
}

/* Minimum number of octets for an unsigned value (at least one). E3 */
static size_t unsigned_octets(uint32_t v)
{
    if (v < 0x100u) {
        return 1u;
    }
    if (v < 0x10000u) {
        return 2u;
    }
    if (v < 0x1000000u) {
        return 3u;
    }
    return 4u;
}

/* Minimum number of two's complement octets for a signed value. E4 */
static size_t signed_octets(int32_t v)
{
    if (v >= -128 && v <= 127) {
        return 1u;
    }
    if (v >= -32768 && v <= 32767) {
        return 2u;
    }
    if (v >= -8388608L && v <= 8388607L) {
        return 3u;
    }
    return 4u;
}

/* Big-endian store of the low n octets of v. */
static void put_be(uint8_t *dst, uint32_t v, size_t n)
{
    size_t i;
    for (i = 0u; i < n; i++) {
        dst[i] = (uint8_t)(v >> (8u * (n - 1u - i)));
    }
}

static uint32_t get_be(const uint8_t *src, size_t n)
{
    uint32_t v = 0u;
    size_t i;
    for (i = 0u; i < n; i++) {
        v = (v << 8) | src[i];
    }
    return v;
}

/* Encode a value whose content is the minimal big-endian form of 'raw' with
 * 'nbytes' octets (unsigned, enumerated, signed, context unsigned). */
static int enc_integer(uint8_t *buf, size_t cap, uint8_t tag, bool context,
                       uint32_t raw, size_t nbytes)
{
    size_t total, h;
    if (buf == NULL) {
        return -1;
    }
    total = fit_len(tag, nbytes, cap);
    if (total == 0u) {
        return -1;
    }
    h = put_hdr(buf, tag, context, nbytes);
    put_be(buf + h, raw, nbytes);
    return (int)total;
}

/* Encode a fixed 4-octet application value (real, date, time, object id). */
static int enc_fixed4(uint8_t *buf, size_t cap, uint8_t tag, const uint8_t content[4])
{
    if (buf == NULL || cap < 5u) {
        return -1;
    }
    buf[0] = (uint8_t)(((unsigned)tag << 4) | 4u);
    buf[1] = content[0];
    buf[2] = content[1];
    buf[3] = content[2];
    buf[4] = content[3];
    return 5;
}

/* ---- Application encoders ------------------------------------------------ */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = 0x00u; /* E1 */
    return 1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u)); /* E2 */
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_integer(buf, cap, BAC_TAG_UNSIGNED, false, v, unsigned_octets(v));
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_integer(buf, cap, BAC_TAG_ENUMERATED, false, v, unsigned_octets(v));
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    return enc_integer(buf, cap, BAC_TAG_SIGNED, false, (uint32_t)v, signed_octets(v));
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t c[4];

    memcpy(&bits, &v, sizeof bits);
    /* E5a: refuse every NaN (exponent all ones, non-zero mantissa). */
    if ((bits & 0x7F800000u) == 0x7F800000u && (bits & 0x007FFFFFu) != 0u) {
        return -1;
    }
    put_be(c, bits, 4u); /* E5: bit-exact, big-endian */
    return enc_fixed4(buf, cap, BAC_TAG_REAL, c);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t total, h;
    if (buf == NULL || (data == NULL && len != 0u)) {
        return -1;
    }
    total = fit_len(BAC_TAG_OCTET_STRING, len, cap);
    if (total == 0u) {
        return -1;
    }
    h = hdr_len(BAC_TAG_OCTET_STRING, len);
    if (len != 0u) {
        memmove(buf + h, data, len); /* content first: tolerates data inside buf */
    }
    (void)put_hdr(buf, BAC_TAG_OCTET_STRING, false, len);
    return (int)total;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    size_t total, h, content;
    if (buf == NULL || (utf8 == NULL && len != 0u) || len > MAX_CONTENT) {
        return -1;
    }
    content = len + 1u; /* E7: character set octet + bytes */
    total = fit_len(BAC_TAG_CHARACTER_STRING, content, cap);
    if (total == 0u) {
        return -1;
    }
    h = hdr_len(BAC_TAG_CHARACTER_STRING, content);
    if (len != 0u) {
        memmove(buf + h + 1u, utf8, len); /* E7a: sent as given */
    }
    buf[h] = 0u; /* character set 0 = UTF-8 */
    (void)put_hdr(buf, BAC_TAG_CHARACTER_STRING, false, content);
    return (int)total;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes, content, total, h, i, j;
    uint8_t *p;

    if (buf == NULL || (bits == NULL && nbits != 0u)) {
        return -1;
    }
    /* E8a: every element must be 0 or 1 */
    for (i = 0u; i < nbits; i++) {
        if (bits[i] > 1u) {
            return -1;
        }
    }
    nbytes = nbits / 8u + ((nbits % 8u) != 0u ? 1u : 0u);
    if (nbytes > MAX_CONTENT) {
        return -1;
    }
    content = nbytes + 1u;
    total = fit_len(BAC_TAG_BIT_STRING, content, cap);
    if (total == 0u) {
        return -1;
    }
    h = put_hdr(buf, BAC_TAG_BIT_STRING, false, content);
    buf[h] = (uint8_t)((8u - nbits % 8u) % 8u); /* unused bits in last octet */
    p = buf + h + 1u;
    for (j = 0u; j < nbytes; j++) {
        uint8_t b = 0u;
        size_t base = j * 8u;
        for (i = 0u; i < 8u && base + i < nbits; i++) {
            b |= (uint8_t)(bits[base + i] << (7u - i)); /* MSB first */
        }
        p[j] = b;
    }
    return (int)total;
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t c[4];

    /* E9a */
    if (year == BAC_YEAR_UNSPECIFIED) {
        c[0] = 0xFFu;
    } else if (year >= 1900u && year <= 2154u) {
        c[0] = (uint8_t)(year - 1900u);
    } else {
        return -1;
    }
    if (!((month >= 1u && month <= 14u) || month == 255u)) {
        return -1;
    }
    if (!((day >= 1u && day <= 34u) || day == 255u)) {
        return -1;
    }
    if (!((wday >= 1u && wday <= 7u) || wday == 255u)) {
        return -1;
    }
    c[1] = month;
    c[2] = day;
    c[3] = wday;
    return enc_fixed4(buf, cap, BAC_TAG_DATE, c);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    uint8_t c[4];

    /* E10a */
    if (hour > 23u && hour != 255u) {
        return -1;
    }
    if (minute > 59u && minute != 255u) {
        return -1;
    }
    if (second > 59u && second != 255u) {
        return -1;
    }
    if (hundredths > 99u && hundredths != 255u) {
        return -1;
    }
    c[0] = hour;
    c[1] = minute;
    c[2] = second;
    c[3] = hundredths;
    return enc_fixed4(buf, cap, BAC_TAG_TIME, c);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];

    if (type > 1023u || instance > 4194303u) { /* E11a */
        return -1;
    }
    put_be(c, ((uint32_t)type << 22) | instance, 4u); /* E11 */
    return enc_fixed4(buf, cap, BAC_TAG_OBJECT_ID, c);
}

/* ---- Context encoders ---------------------------------------------------- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    if (tag == TAG_RESERVED) {
        return -1;
    }
    return enc_integer(buf, cap, tag, true, v, unsigned_octets(v)); /* E12 */
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* E13: context class, length 1, content 00/01 */
    if (tag == TAG_RESERVED) {
        return -1;
    }
    return enc_integer(buf, cap, tag, true, v ? 1u : 0u, 1u);
}

static int enc_pairing_tag(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    size_t total = (tag >= 15u) ? 2u : 1u;
    if (buf == NULL || tag == TAG_RESERVED || cap < total) {
        return -1;
    }
    if (tag >= 15u) {
        buf[0] = (uint8_t)(0xF0u | CLASS_CONTEXT | lvt);
        buf[1] = tag;
    } else {
        buf[0] = (uint8_t)(((unsigned)tag << 4) | CLASS_CONTEXT | lvt);
    }
    return (int)total;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_pairing_tag(buf, cap, tag, LVT_OPENING); /* E14 / G5 */
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_pairing_tag(buf, cap, tag, LVT_CLOSING); /* E14 / G5 */
}

/* ---- Decoders ------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1u;
    uint8_t b0, lvt;

    if (buf == NULL || out == NULL || len == 0u) { /* D1a */
        return -1;
    }
    b0 = buf[0];
    t.tag = (uint8_t)(b0 >> 4);
    t.context = (b0 & CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    t.lvt = 0u;
    lvt = (uint8_t)(b0 & 0x07u);

    if (t.tag == 15u) { /* G3; D1b: values below 15 accepted */
        if (len < 2u || buf[1] == TAG_RESERVED) {
            return -1;
        }
        t.tag = buf[1];
        pos = 2u;
    }

    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) { /* G5 */
        if (!t.context) {
            return -1; /* D1a: application class with LVT 6/7 */
        }
        t.opening = (lvt == LVT_OPENING);
        t.closing = (lvt == LVT_CLOSING);
    } else if (lvt == LVT_EXTENDED) { /* G4; D1: regardless of tag/class */
        uint8_t e;
        if (len < pos + 1u) {
            return -1;
        }
        e = buf[pos++];
        if (e == 254u) {
            if (len < pos + 2u) {
                return -1;
            }
            t.lvt = get_be(buf + pos, 2u);
            pos += 2u;
        } else if (e == 255u) {
            if (len < pos + 4u) {
                return -1;
            }
            t.lvt = get_be(buf + pos, 4u);
            pos += 4u;
        } else {
            t.lvt = e; /* D1b: non-canonical short lengths accepted */
        }
    } else {
        t.lvt = lvt;
    }

    *out = t;
    return (int)pos;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v;
    const uint8_t *c;
    size_t hl, clen;
    uint8_t raw_lvt;
    int r;

    if (out == NULL) {
        return -1;
    }
    r = bac_dec_tag(buf, len, &t);
    if (r < 0 || t.context) { /* D2: header error, context class / open / close */
        return -1;
    }
    hl = (size_t)r;
    raw_lvt = (uint8_t)(buf[0] & 0x07u);
    memset(&v, 0, sizeof v);
    v.tag = t.tag;

    /* D3: Null and Boolean carry no content; use the raw LVT bits. */
    if (t.tag == BAC_TAG_NULL) {
        if (raw_lvt != 0u) {
            return -1;
        }
        *out = v;
        return (int)hl;
    }
    if (t.tag == BAC_TAG_BOOLEAN) {
        if (raw_lvt > 1u) {
            return -1;
        }
        v.v.boolean = (raw_lvt == 1u);
        *out = v;
        return (int)hl;
    }

    /* D2: unsupported / reserved application tags */
    if (t.tag == BAC_TAG_DOUBLE || t.tag > BAC_TAG_OBJECT_ID) {
        return -1;
    }

    /* D2: content must lie within buf[0..len) */
    if ((size_t)t.lvt > len - hl || (size_t)t.lvt > (size_t)INT_MAX - hl) {
        return -1;
    }
    clen = (size_t)t.lvt;
    c = buf + hl;

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED: /* D4 */
        if (clen < 1u || clen > 4u) {
            return -1;
        }
        v.v.u = get_be(c, clen);
        break;
    case BAC_TAG_SIGNED: { /* D4: sign-extended */
        uint32_t u;
        if (clen < 1u || clen > 4u) {
            return -1;
        }
        u = get_be(c, clen);
        if (clen < 4u && (c[0] & 0x80u) != 0u) {
            u |= 0xFFFFFFFFu << (8u * clen);
        }
        /* two's complement reinterpretation without implementation-defined conversion */
        if (u > (uint32_t)INT32_MAX) {
            v.v.i = (int32_t)(-(int32_t)(~u) - 1);
        } else {
            v.v.i = (int32_t)u;
        }
        break;
    }
    case BAC_TAG_REAL: { /* D5: bit-exact, NaN passed through */
        uint32_t bits;
        if (clen != 4u) {
            return -1;
        }
        bits = get_be(c, 4u);
        memcpy(&v.v.r, &bits, sizeof bits);
        break;
    }
    case BAC_TAG_OCTET_STRING: /* D6 */
        v.v.octets.data = c;
        v.v.octets.len = clen;
        break;
    case BAC_TAG_CHARACTER_STRING: /* D7 */
        if (clen < 1u) {
            return -1;
        }
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = clen - 1u;
        break;
    case BAC_TAG_BIT_STRING: { /* D8 */
        uint8_t unused;
        if (clen < 1u) {
            return -1;
        }
        unused = c[0];
        if (unused > 7u || (clen == 1u && unused != 0u)) {
            return -1;
        }
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (clen - 1u) * 8u - unused;
        break;
    }
    case BAC_TAG_DATE: /* D9: not validated */
        if (clen != 4u) {
            return -1;
        }
        v.v.date.year = (c[0] == 0xFFu) ? (uint16_t)BAC_YEAR_UNSPECIFIED
                                        : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME: /* D9 */
        if (clen != 4u) {
            return -1;
        }
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: { /* D9 */
        uint32_t raw;
        if (clen != 4u) {
            return -1;
        }
        raw = get_be(c, 4u);
        v.v.oid.type = (uint16_t)(raw >> 22);
        v.v.oid.instance = raw & 0x3FFFFFu;
        break;
    }
    default: /* 13, 14 (reserved) and >= 15 */
        return -1;
    }

    *out = v;
    return (int)(hl + clen);
}
