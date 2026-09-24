/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Implements the API in bacapp.h according to SPEC.md v1.0.
 * Bare-metal friendly: C11, no heap, no stdio.
 *
 * Encoder contract (SPEC G1): every encoder first validates all arguments and
 * computes the exact encoded length; only when the whole value fits into
 * buf[0..cap) does it write anything.  On any error it returns -1 without
 * touching buf.
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

_Static_assert(sizeof(float) == 4, "float must be IEEE-754 single precision");

#define TAG_EXTENDED 15u      /* tag-number field value announcing an extended tag */
#define TAG_RESERVED 255u     /* reserved tag number (G3) */
#define CLASS_CONTEXT 0x08u   /* class bit of the tag octet (G2) */
#define LVT_EXTENDED 5u       /* LVT value announcing an extended length (G4) */
#define LVT_OPENING 6u        /* opening tag (G5) */
#define LVT_CLOSING 7u        /* closing tag (G5) */

/* ------------------------------------------------------------------------ */
/* Helpers                                                                  */
/* ------------------------------------------------------------------------ */

/* Write the low n (1..4) octets of v big-endian into dst. */
static void put_be(uint8_t *dst, uint32_t v, size_t n)
{
    while (n > 0u) {
        dst[--n] = (uint8_t)(v & 0xFFu);
        v >>= 8;
    }
}

/* Read n (1..4) octets big-endian. */
static uint32_t get_be(const uint8_t *src, size_t n)
{
    uint32_t v = 0u;
    for (size_t i = 0u; i < n; i++) {
        v = (v << 8) | src[i];
    }
    return v;
}

/* Length in octets of a tag header for tag number `tag` and content length `clen` (G3, G4). */
static size_t header_len(uint8_t tag, uint32_t clen)
{
    size_t n = 1u;
    if (tag >= TAG_EXTENDED) {
        n += 1u;
    }
    if (clen > 65535u) {
        n += 5u;
    } else if (clen > 253u) {
        n += 3u;
    } else if (clen > 4u) {
        n += 1u;
    }
    return n;
}

/* Write a tag header (shortest form, G2-G4).  The caller has checked that it fits. */
static void put_header(uint8_t *buf, uint8_t tag, bool ctx, uint32_t clen)
{
    size_t i = 1u;
    uint8_t lvt;

    if (tag >= TAG_EXTENDED) {
        buf[i++] = tag;
    }
    if (clen <= 4u) {
        lvt = (uint8_t)clen;
    } else {
        lvt = LVT_EXTENDED;
        if (clen <= 253u) {
            buf[i++] = (uint8_t)clen;
        } else if (clen <= 65535u) {
            buf[i++] = 254u;
            put_be(&buf[i], clen, 2u);
        } else {
            buf[i++] = 255u;
            put_be(&buf[i], clen, 4u);
        }
    }
    buf[0] = (uint8_t)(((tag >= TAG_EXTENDED) ? 0xF0u : ((unsigned)tag << 4)) |
                       (ctx ? CLASS_CONTEXT : 0u) | lvt);
}

/* Compute the total encoded length of a value with the given tag number and
 * content length, and check that it can be encoded and fits into cap.
 * Returns the total length, or 0 when the value cannot be written. */
static size_t plan(size_t cap, uint8_t tag, size_t clen)
{
    if (tag == TAG_RESERVED) {
        return 0u;
    }
    if ((uint64_t)clen > UINT32_MAX) {
        return 0u; /* not representable in a 4-octet length */
    }
    size_t hl = header_len(tag, (uint32_t)clen);
    if (clen > (size_t)INT_MAX - hl) {
        return 0u; /* total length not representable in the int return value */
    }
    if (hl + clen > cap) {
        return 0u;
    }
    return hl + clen;
}

/* Encode a value whose content is: an optional one-octet prefix (prefix >= 0),
 * followed by len octets of data.  data may overlap buf. */
static int enc_content(uint8_t *buf, size_t cap, uint8_t tag, bool ctx,
                       int prefix, const uint8_t *data, size_t len)
{
    size_t plen = (prefix >= 0) ? 1u : 0u;

    if (buf == NULL || (data == NULL && len != 0u)) {
        return -1;
    }
    if (len > SIZE_MAX - plen) {
        return -1;
    }
    size_t clen = plen + len;
    size_t total = plan(cap, tag, clen);
    if (total == 0u) {
        return -1;
    }
    size_t hl = total - clen;
    /* Content first, header last, so that data overlapping buf still works. */
    if (len != 0u) {
        memmove(&buf[hl + plen], data, len);
    }
    if (plen != 0u) {
        buf[hl] = (uint8_t)prefix;
    }
    put_header(buf, tag, ctx, (uint32_t)clen);
    return (int)total;
}

/* Minimum number of octets for an unsigned value (at least one, E3). */
static size_t unsigned_octets(uint32_t v)
{
    if (v > 0xFFFFFFu) {
        return 4u;
    }
    if (v > 0xFFFFu) {
        return 3u;
    }
    if (v > 0xFFu) {
        return 2u;
    }
    return 1u;
}

/* Minimum number of octets for a two's complement signed value (at least one, E4). */
static size_t signed_octets(int32_t v)
{
    if (v >= -128 && v <= 127) {
        return 1u;
    }
    if (v >= -32768 && v <= 32767) {
        return 2u;
    }
    if (v >= -8388608 && v <= 8388607) {
        return 3u;
    }
    return 4u;
}

static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v)
{
    uint8_t tmp[4];
    size_t n = unsigned_octets(v);
    put_be(tmp, v, n);
    return enc_content(buf, cap, tag, ctx, -1, tmp, n);
}

static int enc_bracket_tag(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    if (buf == NULL || tag == TAG_RESERVED) {
        return -1;
    }
    size_t hl = (tag >= TAG_EXTENDED) ? 2u : 1u;
    if (cap < hl) {
        return -1;
    }
    if (tag >= TAG_EXTENDED) {
        buf[0] = (uint8_t)(0xF0u | CLASS_CONTEXT | lvt);
        buf[1] = tag;
    } else {
        buf[0] = (uint8_t)(((unsigned)tag << 4) | CLASS_CONTEXT | lvt);
    }
    return (int)hl;
}

/* ------------------------------------------------------------------------ */
/* Application-tagged encoders                                              */
/* ------------------------------------------------------------------------ */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return enc_content(buf, cap, BAC_TAG_NULL, false, -1, NULL, 0u);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* E2: value in the LVT field, no content. */
    if (buf == NULL || cap < 1u) {
        return -1;
    }
    buf[0] = (uint8_t)(((unsigned)BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    uint8_t tmp[4];
    size_t n = signed_octets(v);
    put_be(tmp, (uint32_t)v, n); /* conversion to uint32_t yields two's complement */
    return enc_content(buf, cap, BAC_TAG_SIGNED, false, -1, tmp, n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    uint8_t tmp[4];

    memcpy(&bits, &v, sizeof bits);
    /* E5a: refuse every NaN (exponent all ones, non-zero fraction). */
    if ((bits & 0x7F800000u) == 0x7F800000u && (bits & 0x007FFFFFu) != 0u) {
        return -1;
    }
    put_be(tmp, bits, 4u);
    return enc_content(buf, cap, BAC_TAG_REAL, false, -1, tmp, 4u);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    return enc_content(buf, cap, BAC_TAG_OCTET_STRING, false, -1, data, len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    /* E7: character set 0 (UTF-8); E7a: bytes are not validated. */
    return enc_content(buf, cap, BAC_TAG_CHARACTER_STRING, false, 0,
                       (const uint8_t *)utf8, len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    if (buf == NULL || (bits == NULL && nbits != 0u)) {
        return -1;
    }
    /* E8a: every element must be 0 or 1. */
    for (size_t i = 0u; i < nbits; i++) {
        if (bits[i] > 1u) {
            return -1;
        }
    }
    size_t nbytes = nbits / 8u + ((nbits % 8u) != 0u ? 1u : 0u);
    size_t clen = 1u + nbytes;
    size_t total = plan(cap, BAC_TAG_BIT_STRING, clen);
    if (total == 0u) {
        return -1;
    }
    size_t hl = total - clen;
    uint8_t *p = &buf[hl + 1u];
    for (size_t j = 0u; j < nbytes; j++) {
        uint8_t octet = 0u;
        for (size_t k = 0u; k < 8u; k++) {
            size_t i = j * 8u + k;
            if (i < nbits && bits[i] != 0u) {
                octet |= (uint8_t)(0x80u >> k);
            }
        }
        p[j] = octet;
    }
    buf[hl] = (uint8_t)((8u - (nbits % 8u)) % 8u); /* unused bits in the last octet */
    put_header(buf, BAC_TAG_BIT_STRING, false, (uint32_t)clen);
    return (int)total;
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t tmp[4];

    /* E9a */
    if (year == BAC_YEAR_UNSPECIFIED) {
        tmp[0] = 255u;
    } else if (year >= 1900u && year <= 2154u) {
        tmp[0] = (uint8_t)(year - 1900u);
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
    tmp[1] = month;
    tmp[2] = day;
    tmp[3] = wday;
    return enc_content(buf, cap, BAC_TAG_DATE, false, -1, tmp, 4u);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    /* E10a */
    if ((hour > 23u && hour != 255u) || (minute > 59u && minute != 255u) ||
        (second > 59u && second != 255u) || (hundredths > 99u && hundredths != 255u)) {
        return -1;
    }
    uint8_t tmp[4] = { hour, minute, second, hundredths };
    return enc_content(buf, cap, BAC_TAG_TIME, false, -1, tmp, 4u);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t tmp[4];

    /* E11a: never truncate. */
    if (type > 1023u || instance > 4194303u) {
        return -1;
    }
    put_be(tmp, ((uint32_t)type << 22) | instance, 4u);
    return enc_content(buf, cap, BAC_TAG_OBJECT_ID, false, -1, tmp, 4u);
}

/* ------------------------------------------------------------------------ */
/* Context-tagged encoders                                                  */
/* ------------------------------------------------------------------------ */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v); /* tag 255 rejected by plan() */
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* E13: length 1, content 00/01. */
    uint8_t octet = v ? 1u : 0u;
    return enc_content(buf, cap, tag, true, -1, &octet, 1u);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_bracket_tag(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_bracket_tag(buf, cap, tag, LVT_CLOSING);
}

/* ------------------------------------------------------------------------ */
/* Decoders                                                                 */
/* ------------------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1u;

    if (buf == NULL || out == NULL || len == 0u) {
        return -1;
    }
    uint8_t first = buf[0];
    uint8_t raw_lvt = (uint8_t)(first & 0x07u);

    t.tag = (uint8_t)(first >> 4);
    t.context = (first & CLASS_CONTEXT) != 0u;
    t.opening = false;
    t.closing = false;
    t.lvt = raw_lvt;

    if (t.tag == TAG_EXTENDED) {
        /* D1a: truncated extended tag, or reserved tag 255.
         * D1b: values below 15 are accepted as-is. */
        if (len < 2u || buf[1] == TAG_RESERVED) {
            return -1;
        }
        t.tag = buf[1];
        pos = 2u;
    }

    if (raw_lvt == LVT_EXTENDED) {
        /* D1: always an extended length, whatever tag number and class. */
        if (len - pos < 1u) {
            return -1;
        }
        uint8_t ext = buf[pos++];
        if (ext < 254u) {
            t.lvt = ext;
        } else {
            size_t n = (ext == 254u) ? 2u : 4u;
            if (len - pos < n) {
                return -1;
            }
            t.lvt = get_be(&buf[pos], n); /* D1b: non-shortest forms accepted */
            pos += n;
        }
    } else if (raw_lvt == LVT_OPENING || raw_lvt == LVT_CLOSING) {
        if (!t.context) {
            return -1; /* D1a */
        }
        t.opening = (raw_lvt == LVT_OPENING);
        t.closing = (raw_lvt == LVT_CLOSING);
        t.lvt = 0u;
    }

    *out = t;
    return (int)pos;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v;

    if (out == NULL) {
        return -1;
    }
    int r = bac_dec_tag(buf, len, &t);
    if (r < 0 || t.context) {
        return -1; /* D2: header error, context class, opening/closing */
    }
    size_t hl = (size_t)r;
    uint8_t raw_lvt = (uint8_t)(buf[0] & 0x07u);
    size_t clen = 0u;

    memset(&v, 0, sizeof v);
    v.tag = t.tag;

    if (t.tag == BAC_TAG_NULL) {
        if (raw_lvt != 0u) {
            return -1; /* D3 */
        }
    } else if (t.tag == BAC_TAG_BOOLEAN) {
        if (raw_lvt > 1u) {
            return -1; /* D3 */
        }
        v.v.boolean = (raw_lvt == 1u);
    } else {
        if (t.lvt > len - hl) {
            return -1; /* D2: content beyond len */
        }
        clen = t.lvt;
        const uint8_t *c = &buf[hl];

        switch (t.tag) {
        case BAC_TAG_UNSIGNED:
        case BAC_TAG_ENUMERATED:
            if (clen < 1u || clen > 4u) {
                return -1; /* D4 */
            }
            v.v.u = get_be(c, clen);
            break;

        case BAC_TAG_SIGNED: {
            if (clen < 1u || clen > 4u) {
                return -1; /* D4 */
            }
            uint32_t u = get_be(c, clen);
            if (clen < 4u && (c[0] & 0x80u) != 0u) {
                u |= ~((1u << (8u * clen)) - 1u); /* sign-extend */
            }
            /* Portable two's complement reinterpretation. */
            v.v.i = (u <= (uint32_t)INT32_MAX) ? (int32_t)u
                                               : (int32_t)(-(int32_t)(~u) - 1);
            break;
        }

        case BAC_TAG_REAL: {
            if (clen != 4u) {
                return -1; /* D5 */
            }
            uint32_t bits = get_be(c, 4u);
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

        case BAC_TAG_BIT_STRING: /* D8 */
            if (clen < 1u || c[0] > 7u || (clen == 1u && c[0] != 0u)) {
                return -1;
            }
            if ((clen - 1u) > (SIZE_MAX - 7u) / 8u) {
                return -1;
            }
            v.v.bits.data = c + 1;
            v.v.bits.nbits = (clen - 1u) * 8u - c[0];
            break;

        case BAC_TAG_DATE: /* D9 */
            if (clen != 4u) {
                return -1;
            }
            v.v.date.year = (c[0] == 255u) ? (uint16_t)BAC_YEAR_UNSPECIFIED
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
            if (clen != 4u) {
                return -1;
            }
            uint32_t oid = get_be(c, 4u);
            v.v.oid.type = (uint16_t)(oid >> 22);
            v.v.oid.instance = oid & 0x3FFFFFu;
            break;
        }

        default:
            return -1; /* D2: Double (5), reserved 13, 14 and >= 15 */
        }
    }

    if (clen > (size_t)INT_MAX - hl) {
        return -1; /* consumed length not representable in the return value */
    }
    *out = v;
    return (int)(hl + clen);
}
