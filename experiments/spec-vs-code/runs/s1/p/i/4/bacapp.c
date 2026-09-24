/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Freestanding C11: no heap, no stdio.  Only <string.h> (memcpy/memmove/memset)
 * and <limits.h> are used.
 *
 * Design rules
 *  - Encoders are strict: they only ever produce valid, canonical encodings
 *    (minimum-length integers, shortest tag/length forms, charset 0 = UTF-8,
 *    unused bit-string bits zero).  Invalid arguments (out-of-range dates,
 *    times, object ids, tag number 255, ill-formed UTF-8, ...) are rejected.
 *  - On any error an encoder returns -1 WITHOUT touching buf.  Nothing is
 *    ever written at or beyond buf[cap].
 *  - Decoders never read outside buf[0..len), never modify *out on error, and
 *    reject anything whose meaning is undefined or that does not fit the
 *    bac_value_t representation.  They tolerate non-canonical but unambiguous
 *    forms (e.g. an extended length octet holding a value < 5, or an unsigned
 *    with leading zero octets that still fits in 32 bits).
 */
#include "bacapp.h"

#include <limits.h>
#include <string.h>

_Static_assert(sizeof(float) == 4, "BACnet REAL requires a 32-bit IEEE-754 float");

#define TAG_NUM_MAX 254u   /* tag number 255 is reserved (20.2.1.2) */
#define TAG_EXTENDED 15u   /* tag number field value meaning "tag number in next octet" */
#define CLASS_CONTEXT 0x08u
#define LVT_EXTENDED 5u
#define LVT_OPENING 6u
#define LVT_CLOSING 7u

/* ======================================================================== */
/* Helpers                                                                  */
/* ======================================================================== */

/* true when n does not fit in 32 bits (written so it is warning-free when
 * size_t is itself 32 bits or narrower) */
static bool exceeds_u32(size_t n)
{
    return sizeof(size_t) > 4u && ((n >> 16) >> 16) != 0u;
}

/* Size of a tag header for tag number `tag` and content length `clen`. */
static size_t hdr_size(unsigned tag, size_t clen)
{
    size_t n = (tag >= TAG_EXTENDED) ? 2u : 1u;
    if (clen >= 5u) {
        n += 1u;                    /* extended length octet */
        if (clen >= 254u) {
            n += 2u;                /* 16-bit length */
            if (clen >= 65536u) {
                n += 2u;            /* 32-bit length */
            }
        }
    }
    return n;
}

/* Write a tag header carrying a length.  Caller has checked capacity. */
static size_t put_hdr(uint8_t *b, unsigned tag, bool context, size_t clen)
{
    size_t i = 1u;
    uint8_t first = context ? (uint8_t)CLASS_CONTEXT : 0u;

    if (tag >= TAG_EXTENDED) {
        first |= (uint8_t)(TAG_EXTENDED << 4);
        b[i++] = (uint8_t)tag;
    } else {
        first |= (uint8_t)(tag << 4);
    }

    if (clen < 5u) {
        first |= (uint8_t)clen;
    } else {
        uint32_t l = (uint32_t)clen;
        first |= (uint8_t)LVT_EXTENDED;
        if (l < 254u) {
            b[i++] = (uint8_t)l;
        } else if (l < 65536u) {
            b[i++] = 254u;
            b[i++] = (uint8_t)(l >> 8);
            b[i++] = (uint8_t)l;
        } else {
            b[i++] = 255u;
            b[i++] = (uint8_t)(l >> 24);
            b[i++] = (uint8_t)(l >> 16);
            b[i++] = (uint8_t)(l >> 8);
            b[i++] = (uint8_t)l;
        }
    }
    b[0] = first;
    return i;
}

/* Validate arguments common to every encoder and compute the header size.
 * Returns the total encoded size, or -1 if the value cannot be encoded into
 * buf[0..cap). */
static int reserve(const uint8_t *buf, size_t cap, unsigned tag, size_t clen, size_t *hl)
{
    size_t h;

    if (buf == NULL || tag > TAG_NUM_MAX || exceeds_u32(clen)) {
        return -1;
    }
    h = hdr_size(tag, clen);
    if (clen > cap || h > cap - clen) {
        return -1;
    }
    if (h + clen > (size_t)INT_MAX) {
        return -1;
    }
    *hl = h;
    return (int)(h + clen);
}

static void put_be(uint8_t *b, uint32_t v, size_t n)
{
    while (n > 0u) {
        n--;
        b[n] = (uint8_t)v;
        v >>= 8;
    }
}

static uint32_t get_be(const uint8_t *b, size_t n)
{
    uint32_t v = 0u;
    size_t i;
    for (i = 0u; i < n; i++) {
        v = (v << 8) | b[i];
    }
    return v;
}

/* Minimum number of octets for an unsigned value (20.2.4). */
static size_t unsigned_len(uint32_t v)
{
    if (v <= 0xFFu) {
        return 1u;
    }
    if (v <= 0xFFFFu) {
        return 2u;
    }
    if (v <= 0xFFFFFFu) {
        return 3u;
    }
    return 4u;
}

/* Minimum number of octets for a two's-complement signed value (20.2.5). */
static size_t signed_len(int32_t v)
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

static int enc_unsigned_tagged(uint8_t *buf, size_t cap, unsigned tag, bool context, uint32_t v)
{
    size_t hl;
    size_t n = unsigned_len(v);
    int total = reserve(buf, cap, tag, n, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, tag, context, n);
    put_be(buf + hl, v, n);
    return total;
}

/* Encode a fixed 4-octet application value (REAL, DATE, TIME, OBJECT_ID). */
static int enc_app_4(uint8_t *buf, size_t cap, unsigned tag, uint32_t v)
{
    size_t hl;
    int total = reserve(buf, cap, tag, 4u, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, tag, false, 4u);
    put_be(buf + hl, v, 4u);
    return total;
}

/* Well-formed UTF-8 per RFC 3629 / Unicode: no overlongs, no surrogates,
 * nothing above U+10FFFF, no truncated sequences. */
static bool utf8_valid(const uint8_t *s, size_t len)
{
    size_t i = 0u;
    while (i < len) {
        uint8_t c = s[i];
        size_t need;
        uint8_t lo = 0x80u;
        uint8_t hi = 0xBFu;

        if (c <= 0x7Fu) {
            i++;
            continue;
        }
        if (c >= 0xC2u && c <= 0xDFu) {
            need = 1u;
        } else if (c >= 0xE0u && c <= 0xEFu) {
            need = 2u;
            if (c == 0xE0u) {
                lo = 0xA0u;        /* no overlongs */
            } else if (c == 0xEDu) {
                hi = 0x9Fu;        /* no UTF-16 surrogates */
            }
        } else if (c >= 0xF0u && c <= 0xF4u) {
            need = 3u;
            if (c == 0xF0u) {
                lo = 0x90u;        /* no overlongs */
            } else if (c == 0xF4u) {
                hi = 0x8Fu;        /* <= U+10FFFF */
            }
        } else {
            return false;          /* 0x80-0xC1, 0xF5-0xFF */
        }
        if (len - i - 1u < need) {
            return false;
        }
        i++;
        if (s[i] < lo || s[i] > hi) {
            return false;
        }
        i++;
        need--;
        while (need > 0u) {
            if (s[i] < 0x80u || s[i] > 0xBFu) {
                return false;
            }
            i++;
            need--;
        }
    }
    return true;
}

/* ======================================================================== */
/* Application-tagged encoders                                              */
/* ======================================================================== */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    size_t hl;
    int total = reserve(buf, cap, BAC_TAG_NULL, 0u, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, BAC_TAG_NULL, false, 0u);
    return total;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* 20.2.3: the value lives in the L/V/T field, there are no content octets */
    size_t hl;
    int total = reserve(buf, cap, BAC_TAG_BOOLEAN, 0u, &hl);
    if (total < 0) {
        return -1;
    }
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return total;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_unsigned_tagged(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    size_t hl;
    size_t n = signed_len(v);
    int total = reserve(buf, cap, BAC_TAG_SIGNED, n, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, BAC_TAG_SIGNED, false, n);
    put_be(buf + hl, (uint32_t)v, n);   /* conversion is modulo 2^32: two's complement */
    return total;
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    memcpy(&bits, &v, sizeof bits);     /* bit-exact, keeps NaN payloads and -0 */
    return enc_app_4(buf, cap, BAC_TAG_REAL, bits);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    size_t hl;
    int total;

    if (data == NULL && len != 0u) {
        return -1;
    }
    total = reserve(buf, cap, BAC_TAG_OCTET_STRING, len, &hl);
    if (total < 0) {
        return -1;
    }
    /* Content first, header last: correct even if data overlaps buf. */
    if (len != 0u) {
        memmove(buf + hl, data, len);
    }
    (void)put_hdr(buf, BAC_TAG_OCTET_STRING, false, len);
    return total;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    const uint8_t *s = (const uint8_t *)utf8;
    size_t hl;
    size_t clen;
    int total;

    if ((s == NULL && len != 0u) || len == SIZE_MAX) {
        return -1;
    }
    if (len != 0u && !utf8_valid(s, len)) {
        return -1;
    }
    clen = len + 1u;                                  /* + character-set octet */
    total = reserve(buf, cap, BAC_TAG_CHARACTER_STRING, clen, &hl);
    if (total < 0) {
        return -1;
    }
    if (len != 0u) {
        memmove(buf + hl + 1u, s, len);
    }
    buf[hl] = 0u;                                     /* ISO 10646 (UTF-8) */
    (void)put_hdr(buf, BAC_TAG_CHARACTER_STRING, false, clen);
    return total;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t hl;
    size_t nbytes;
    size_t k;
    int total;

    if (bits == NULL && nbits != 0u) {
        return -1;
    }
    nbytes = nbits / 8u + ((nbits % 8u) != 0u ? 1u : 0u);
    if (nbytes == SIZE_MAX) {
        return -1;
    }
    total = reserve(buf, cap, BAC_TAG_BIT_STRING, nbytes + 1u, &hl);
    if (total < 0) {
        return -1;
    }
    /* Pack MSB-first; any non-zero bits[i] means 1.  Unused trailing bits are 0. */
    for (k = 0u; k < nbytes; k++) {
        uint8_t octet = 0u;
        size_t base = k * 8u;
        size_t j;
        for (j = 0u; j < 8u && base + j < nbits; j++) {
            if (bits[base + j] != 0u) {
                octet |= (uint8_t)(0x80u >> j);
            }
        }
        buf[hl + 1u + k] = octet;
    }
    buf[hl] = (uint8_t)((8u - (nbits % 8u)) % 8u);    /* unused bits in last octet */
    (void)put_hdr(buf, BAC_TAG_BIT_STRING, false, nbytes + 1u);
    return total;
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_unsigned_tagged(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t y;

    /* 20.2.12: year-1900 in one octet (1900..2154), X'FF' = unspecified */
    if (year == BAC_YEAR_UNSPECIFIED) {
        y = 0xFFu;
    } else if (year >= 1900u && year <= 2154u) {
        y = (uint8_t)(year - 1900u);
    } else {
        return -1;
    }
    /* month 1..12, 13 = odd months, 14 = even months, 255 = unspecified */
    if (!((month >= 1u && month <= 14u) || month == 0xFFu)) {
        return -1;
    }
    /* day 1..31, 32 = last day of month, 33 = odd days, 34 = even days, 255 */
    if (!((day >= 1u && day <= 34u) || day == 0xFFu)) {
        return -1;
    }
    /* day of week 1 (Monday) .. 7 (Sunday), 255 = unspecified */
    if (!((wday >= 1u && wday <= 7u) || wday == 0xFFu)) {
        return -1;
    }
    return enc_app_4(buf, cap, BAC_TAG_DATE,
                     ((uint32_t)y << 24) | ((uint32_t)month << 16) | ((uint32_t)day << 8) | wday);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    /* 20.2.13: each field may be X'FF' = unspecified */
    if (!(hour <= 23u || hour == 0xFFu) ||
        !(minute <= 59u || minute == 0xFFu) ||
        !(second <= 59u || second == 0xFFu) ||
        !(hundredths <= 99u || hundredths == 0xFFu)) {
        return -1;
    }
    return enc_app_4(buf, cap, BAC_TAG_TIME,
                     ((uint32_t)hour << 24) | ((uint32_t)minute << 16) | ((uint32_t)second << 8) | hundredths);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    /* 20.2.14: 10-bit object type, 22-bit instance number */
    if (type > 0x3FFu || instance > 0x3FFFFFu) {
        return -1;
    }
    return enc_app_4(buf, cap, BAC_TAG_OBJECT_ID, ((uint32_t)type << 22) | instance);
}

/* ======================================================================== */
/* Context-tagged encoders                                                  */
/* ======================================================================== */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_unsigned_tagged(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    /* 20.2.3: a context-tagged boolean has length 1 and one content octet */
    size_t hl;
    int total = reserve(buf, cap, tag, 1u, &hl);
    if (total < 0) {
        return -1;
    }
    (void)put_hdr(buf, tag, true, 1u);
    buf[hl] = v ? 1u : 0u;
    return total;
}

static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, unsigned lvt)
{
    size_t hl;
    int total = reserve(buf, cap, tag, 0u, &hl);
    if (total < 0) {
        return -1;
    }
    if (tag >= TAG_EXTENDED) {
        buf[0] = (uint8_t)((TAG_EXTENDED << 4) | CLASS_CONTEXT | lvt);
        buf[1] = tag;
    } else {
        buf[0] = (uint8_t)(((unsigned)tag << 4) | CLASS_CONTEXT | lvt);
    }
    return total;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_OPENING);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_CLOSING);
}

/* ======================================================================== */
/* Decoders                                                                 */
/* ======================================================================== */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t i = 1u;
    unsigned tag;
    unsigned lvt;

    if (buf == NULL || out == NULL || len == 0u) {
        return -1;
    }
    memset(&t, 0, sizeof t);
    t.context = (buf[0] & CLASS_CONTEXT) != 0u;
    tag = (unsigned)buf[0] >> 4;
    lvt = buf[0] & 0x07u;

    if (tag == TAG_EXTENDED) {
        if (len < 2u) {
            return -1;
        }
        tag = buf[1];
        if (tag > TAG_NUM_MAX) {
            return -1;                         /* 255 is reserved */
        }
        i = 2u;
    }
    t.tag = (uint8_t)tag;

    if (lvt == LVT_OPENING || lvt == LVT_CLOSING) {
        /* Opening/closing tags exist only in the context class (20.2.1.3.2) */
        if (!t.context) {
            return -1;
        }
        t.opening = (lvt == LVT_OPENING);
        t.closing = (lvt == LVT_CLOSING);
        t.lvt = 0u;
    } else if (!t.context && tag == BAC_TAG_BOOLEAN) {
        /* Application BOOLEAN: L/V/T is the value, not a length (20.2.3) */
        if (lvt > 1u) {
            return -1;
        }
        t.lvt = lvt;
    } else if (lvt == LVT_EXTENDED) {
        uint8_t e;
        if (len - i < 1u) {
            return -1;
        }
        e = buf[i++];
        if (e < 254u) {
            t.lvt = e;
        } else if (e == 254u) {
            if (len - i < 2u) {
                return -1;
            }
            t.lvt = get_be(buf + i, 2u);
            i += 2u;
        } else {
            if (len - i < 4u) {
                return -1;
            }
            t.lvt = get_be(buf + i, 4u);
            i += 4u;
        }
    } else {
        t.lvt = lvt;
    }

    *out = t;
    return (int)i;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    bac_tag_t t;
    bac_value_t v;
    const uint8_t *c;
    size_t hl;
    size_t clen;
    int h;

    if (out == NULL) {
        return -1;
    }
    h = bac_dec_tag(buf, len, &t);
    if (h < 0 || t.context) {                  /* opening/closing are context-only */
        return -1;
    }
    hl = (size_t)h;
    if (t.tag == BAC_TAG_BOOLEAN) {
        clen = 0u;                             /* value is in the L/V/T field */
    } else {
        if (t.lvt > len - hl) {
            return -1;                         /* truncated content */
        }
        clen = (size_t)t.lvt;
    }
    if (hl + clen > (size_t)INT_MAX) {
        return -1;
    }
    c = buf + hl;

    memset(&v, 0, sizeof v);
    v.tag = t.tag;

    switch (t.tag) {
    case BAC_TAG_NULL:
        if (clen != 0u) {
            return -1;
        }
        break;

    case BAC_TAG_BOOLEAN:
        v.v.boolean = (t.lvt != 0u);           /* bac_dec_tag guarantees 0 or 1 */
        break;

    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED: {
        size_t k = 0u;
        if (clen == 0u) {
            return -1;
        }
        while (clen - k > 4u) {                /* tolerate leading zero octets */
            if (c[k] != 0u) {
                return -1;                     /* does not fit in 32 bits */
            }
            k++;
        }
        v.v.u = get_be(c + k, clen - k);
        break;
    }

    case BAC_TAG_SIGNED: {
        size_t k = 0u;
        uint32_t u;
        if (clen == 0u) {
            return -1;
        }
        /* tolerate redundant sign-extension octets */
        while (clen - k > 4u) {
            if (!((c[k] == 0x00u && (c[k + 1u] & 0x80u) == 0u) ||
                  (c[k] == 0xFFu && (c[k + 1u] & 0x80u) != 0u))) {
                return -1;                     /* does not fit in 32 bits */
            }
            k++;
        }
        u = get_be(c + k, clen - k);
        if ((c[k] & 0x80u) != 0u && clen - k < 4u) {
            u |= 0xFFFFFFFFu << (8u * (clen - k));   /* sign-extend */
        }
        /* two's-complement reinterpretation without implementation-defined conversion */
        if (u <= (uint32_t)INT32_MAX) {
            v.v.i = (int32_t)u;
        } else {
            v.v.i = (int32_t)(u - 0x80000000u) + INT32_MIN;
        }
        break;
    }

    case BAC_TAG_REAL: {
        uint32_t bits;
        if (clen != 4u) {
            return -1;
        }
        bits = get_be(c, 4u);
        memcpy(&v.v.r, &bits, sizeof bits);
        break;
    }

    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = c;
        v.v.octets.len = clen;
        break;

    case BAC_TAG_CHARACTER_STRING:
        if (clen < 1u) {
            return -1;                         /* character-set octet is mandatory */
        }
        v.v.str.charset = c[0];
        v.v.str.data = c + 1u;
        v.v.str.len = clen - 1u;
        break;

    case BAC_TAG_BIT_STRING: {
        uint8_t unused;
        if (clen < 1u) {
            return -1;                         /* unused-bits octet is mandatory */
        }
        unused = c[0];
        if (unused > 7u || (clen == 1u && unused != 0u)) {
            return -1;
        }
        if (clen - 1u > SIZE_MAX / 8u) {
            return -1;
        }
        v.v.bits.data = c + 1u;
        v.v.bits.nbits = (clen - 1u) * 8u - unused;
        break;
    }

    case BAC_TAG_DATE:
        if (clen != 4u) {
            return -1;
        }
        v.v.date.year = (c[0] == 0xFFu) ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;

    case BAC_TAG_TIME:
        if (clen != 4u) {
            return -1;
        }
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;

    case BAC_TAG_OBJECT_ID: {
        uint32_t oid;
        if (clen != 4u) {
            return -1;
        }
        oid = get_be(c, 4u);
        v.v.oid.type = (uint16_t)(oid >> 22);
        v.v.oid.instance = oid & 0x3FFFFFu;
        break;
    }

    case BAC_TAG_DOUBLE:   /* not representable in bac_value_t */
    default:               /* 13, 14 reserved; >= 15 undefined */
        return -1;
    }

    *out = v;
    return (int)(hl + clen);
}
