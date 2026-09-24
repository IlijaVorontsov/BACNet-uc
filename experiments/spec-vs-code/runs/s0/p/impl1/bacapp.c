/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Implements bacapp.h according to SPEC.md v1.0.
 * C11, no heap, no stdio, no libc dependency (freestanding-friendly).
 *
 * Encoder contract (G1): every encoder first validates all arguments and computes
 * the exact encoded size; only when everything is valid and fits into cap does it
 * write, and then it writes exactly the returned number of bytes (never beyond).
 * On -1 no byte of buf is touched.
 *
 * Decoders never modify *out when they return -1.
 */
#include "bacapp.h"

#include <limits.h>

_Static_assert(CHAR_BIT == 8, "bacapp requires 8-bit bytes");
_Static_assert(sizeof(float) == 4, "bacapp requires 32-bit IEEE-754 float");

#define MAX_TAG_NUMBER 254u   /* G3: 255 is reserved */
#define CLASS_CONTEXT 0x08u

/* ------------------------------------------------------------------------ */
/* Helpers                                                                  */
/* ------------------------------------------------------------------------ */

/* Bit pattern of a float, exact (read through the object representation). */
static uint32_t float_to_bits(float f)
{
    uint32_t u = 0;
    const unsigned char *s = (const unsigned char *)&f;
    unsigned char *d = (unsigned char *)&u;
    for (size_t i = 0; i < sizeof u; i++)
        d[i] = s[i];
    return u;
}

/* Store a bit pattern into a float object without passing it through an FPU
 * register (keeps signalling NaN payloads bit-exact on every platform). */
static void bits_to_float(float *dst, uint32_t u)
{
    const unsigned char *s = (const unsigned char *)&u;
    unsigned char *d = (unsigned char *)dst;
    for (size_t i = 0; i < sizeof u; i++)
        d[i] = s[i];
}

/* memmove replacement: correct for overlapping ranges. */
static void move_bytes(uint8_t *dst, const uint8_t *src, size_t n)
{
    if (n == 0 || dst == src)
        return;
    if ((uintptr_t)dst < (uintptr_t)src) {
        for (size_t i = 0; i < n; i++)
            dst[i] = src[i];
    } else {
        for (size_t i = n; i > 0; i--)
            dst[i - 1] = src[i - 1];
    }
}

/* Header size (tag octet + extended tag octet + extended length octets) for a
 * tag with number tag (0..254) and content length clen. Returns 0 when clen
 * cannot be represented (more than 2^32-1 octets). G3/G4, shortest form. */
static size_t header_size(unsigned tag, size_t clen)
{
    size_t n = (tag >= 15u) ? 2u : 1u;
    if (clen <= 4u)
        return n;
    if (clen <= 253u)
        return n + 1u;
    if (clen <= 65535u)
        return n + 3u;
    if ((uint64_t)clen <= 0xFFFFFFFFu)
        return n + 5u;
    return 0;
}

/* Validate buffer/tag and compute the total size of a value whose header uses
 * the length clen and whose content is clen octets long.
 * Returns the total size, or -1 (nothing is written). */
static int plan(const uint8_t *buf, size_t cap, unsigned tag, size_t clen, size_t *hl)
{
    if (buf == NULL || tag > MAX_TAG_NUMBER)
        return -1;
    size_t h = header_size(tag, clen);
    if (h == 0)
        return -1;
    if (clen > cap || h > cap - clen)
        return -1; /* does not fit */
    if (h + clen > (size_t)INT_MAX)
        return -1; /* size not representable in the return type */
    *hl = h;
    return (int)(h + clen);
}

/* Tag octet(s) plus extended length; the caller has sized buf via plan(). */
static size_t put_header(uint8_t *buf, unsigned tag, bool context, size_t clen)
{
    size_t i = 0;
    uint8_t cls = context ? (uint8_t)CLASS_CONTEXT : 0u;
    uint8_t lvt = (clen <= 4u) ? (uint8_t)clen : 5u;
    if (tag >= 15u) {
        buf[i++] = (uint8_t)(0xF0u | cls | lvt);
        buf[i++] = (uint8_t)tag;
    } else {
        buf[i++] = (uint8_t)((tag << 4) | cls | lvt);
    }
    if (clen > 4u) {
        uint32_t l = (uint32_t)clen;
        if (clen <= 253u) {
            buf[i++] = (uint8_t)l;
        } else if (clen <= 65535u) {
            buf[i++] = 254u;
            buf[i++] = (uint8_t)(l >> 8);
            buf[i++] = (uint8_t)l;
        } else {
            buf[i++] = 255u;
            buf[i++] = (uint8_t)(l >> 24);
            buf[i++] = (uint8_t)(l >> 16);
            buf[i++] = (uint8_t)(l >> 8);
            buf[i++] = (uint8_t)l;
        }
    }
    return i;
}

/* Encode a value with a small content (at most 4 octets, built by the caller). */
static int enc_small(uint8_t *buf, size_t cap, unsigned tag, bool context,
                     const uint8_t *content, size_t clen)
{
    size_t hl;
    int total = plan(buf, cap, tag, clen, &hl);
    if (total < 0)
        return -1;
    put_header(buf, tag, context, clen);
    for (size_t i = 0; i < clen; i++)
        buf[hl + i] = content[i];
    return total;
}

/* Big-endian, minimum number of octets, at least one (E3). */
static size_t unsigned_content(uint8_t c[4], uint32_t v)
{
    size_t n = (v <= 0xFFu) ? 1u : (v <= 0xFFFFu) ? 2u : (v <= 0xFFFFFFu) ? 3u : 4u;
    for (size_t i = 0; i < n; i++)
        c[i] = (uint8_t)(v >> (8u * (n - 1u - i)));
    return n;
}

/* Tag octet(s) for opening (lvt 6) / closing (lvt 7) tags (G5). */
static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    if (buf == NULL || tag > MAX_TAG_NUMBER)
        return -1;
    size_t n = (tag >= 15u) ? 2u : 1u;
    if (cap < n)
        return -1;
    if (tag >= 15u) {
        buf[0] = (uint8_t)(0xF0u | CLASS_CONTEXT | lvt);
        buf[1] = tag;
    } else {
        buf[0] = (uint8_t)((unsigned)tag << 4 | CLASS_CONTEXT | lvt);
    }
    return (int)n;
}

/* ------------------------------------------------------------------------ */
/* Application encoders                                                     */
/* ------------------------------------------------------------------------ */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return enc_small(buf, cap, BAC_TAG_NULL, false, NULL, 0); /* E1: 00 */
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* E2: value in the LVT field, no content */
    if (buf == NULL || cap < 1u)
        return -1;
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    uint8_t c[4];
    size_t n = unsigned_content(c, v);
    return enc_small(buf, cap, BAC_TAG_UNSIGNED, false, c, n);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    uint8_t c[4];
    size_t n = unsigned_content(c, v);
    return enc_small(buf, cap, BAC_TAG_ENUMERATED, false, c, n);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    /* E4: two's complement, minimum number of octets */
    size_t n;
    if (v >= -128 && v <= 127)
        n = 1;
    else if (v >= -32768 && v <= 32767)
        n = 2;
    else if (v >= -8388608 && v <= 8388607)
        n = 3;
    else
        n = 4;
    uint32_t u = (uint32_t)v; /* modular conversion: two's complement pattern */
    uint8_t c[4];
    for (size_t i = 0; i < n; i++)
        c[i] = (uint8_t)(u >> (8u * (n - 1u - i)));
    return enc_small(buf, cap, BAC_TAG_SIGNED, false, c, n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t u = float_to_bits(v);
    /* E5a: refuse any NaN (exponent all ones, non-zero mantissa) */
    if ((u & 0x7F800000u) == 0x7F800000u && (u & 0x007FFFFFu) != 0u)
        return -1;
    uint8_t c[4] = { (uint8_t)(u >> 24), (uint8_t)(u >> 16), (uint8_t)(u >> 8), (uint8_t)u };
    return enc_small(buf, cap, BAC_TAG_REAL, false, c, 4);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (data == NULL && len != 0u)
        return -1;
    size_t hl;
    int total = plan(buf, cap, BAC_TAG_OCTET_STRING, len, &hl);
    if (total < 0)
        return -1;
    /* content first, then header: correct even if data lies inside buf */
    if (len != 0u)
        move_bytes(buf + hl, data, len);
    put_header(buf, BAC_TAG_OCTET_STRING, false, len);
    return total;
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (utf8 == NULL && len != 0u)
        return -1;
    if (len == SIZE_MAX)
        return -1; /* len + 1 not representable */
    size_t clen = len + 1u; /* charset octet + text (E7) */
    size_t hl;
    int total = plan(buf, cap, BAC_TAG_CHARACTER_STRING, clen, &hl);
    if (total < 0)
        return -1;
    /* E7a: bytes are not validated. Content first (alias-safe), then header. */
    if (len != 0u)
        move_bytes(buf + hl + 1u, (const uint8_t *)utf8, len);
    buf[hl] = 0u; /* character set 0 = UTF-8 */
    put_header(buf, BAC_TAG_CHARACTER_STRING, false, clen);
    return total;
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    /* Note: bits must not overlap buf. */
    if (bits == NULL && nbits != 0u)
        return -1;
    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1u)
            return -1; /* E8a */
    size_t nbytes = nbits / 8u + (nbits % 8u != 0u ? 1u : 0u);
    size_t clen = 1u + nbytes; /* cannot overflow: nbytes <= SIZE_MAX / 8 + 1 */
    size_t hl;
    int total = plan(buf, cap, BAC_TAG_BIT_STRING, clen, &hl);
    if (total < 0)
        return -1;
    put_header(buf, BAC_TAG_BIT_STRING, false, clen);
    buf[hl] = (uint8_t)((8u - nbits % 8u) % 8u); /* unused bits in last octet */
    uint8_t *p = buf + hl + 1u;
    for (size_t k = 0; k < nbytes; k++) {
        uint8_t octet = 0;
        for (size_t j = 0; j < 8u; j++) {
            size_t i = k * 8u + j;
            if (i < nbits && bits[i])
                octet = (uint8_t)(octet | (0x80u >> j)); /* MSB first */
        }
        p[k] = octet; /* unused trailing bits are 0 */
    }
    return total;
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    /* E9a */
    uint8_t y;
    if (year == BAC_YEAR_UNSPECIFIED)
        y = 255u;
    else if (year >= 1900u && year <= 2154u)
        y = (uint8_t)(year - 1900u);
    else
        return -1;
    if (!((month >= 1u && month <= 14u) || month == 255u))
        return -1;
    if (!((day >= 1u && day <= 34u) || day == 255u))
        return -1;
    if (!((wday >= 1u && wday <= 7u) || wday == 255u))
        return -1;
    uint8_t c[4] = { y, month, day, wday };
    return enc_small(buf, cap, BAC_TAG_DATE, false, c, 4);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    /* E10a */
    if (!(hour <= 23u || hour == 255u))
        return -1;
    if (!(minute <= 59u || minute == 255u))
        return -1;
    if (!(second <= 59u || second == 255u))
        return -1;
    if (!(hundredths <= 99u || hundredths == 255u))
        return -1;
    uint8_t c[4] = { hour, minute, second, hundredths };
    return enc_small(buf, cap, BAC_TAG_TIME, false, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    /* E11a: never truncate */
    if (type > 1023u || instance > 4194303u)
        return -1;
    uint32_t u = ((uint32_t)type << 22) | instance;
    uint8_t c[4] = { (uint8_t)(u >> 24), (uint8_t)(u >> 16), (uint8_t)(u >> 8), (uint8_t)u };
    return enc_small(buf, cap, BAC_TAG_OBJECT_ID, false, c, 4);
}

/* ------------------------------------------------------------------------ */
/* Context encoders                                                         */
/* ------------------------------------------------------------------------ */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    uint8_t c[4];
    size_t n = unsigned_content(c, v);
    return enc_small(buf, cap, tag, true, c, n); /* tag 255 rejected in plan() */
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    uint8_t c[1] = { v ? 1u : 0u }; /* E13: length 1, content 00/01 */
    return enc_small(buf, cap, tag, true, c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, 6u);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, 7u);
}

/* ------------------------------------------------------------------------ */
/* Decoders                                                                 */
/* ------------------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    if (buf == NULL || out == NULL || len == 0u)
        return -1;
    bac_tag_t t = { 0 };
    size_t i = 0;
    uint8_t b0 = buf[i++];
    unsigned tag = b0 >> 4;
    unsigned lvt = b0 & 0x07u;
    t.context = (b0 & CLASS_CONTEXT) != 0u;

    if (tag == 15u) { /* G3: extended tag number */
        if (i >= len)
            return -1;
        if (buf[i] == 255u)
            return -1;
        tag = buf[i++]; /* D1b: values below 15 accepted */
    }
    t.tag = (uint8_t)tag;

    if (lvt >= 6u) { /* G5: opening / closing */
        if (!t.context)
            return -1; /* D1a */
        t.opening = (lvt == 6u);
        t.closing = (lvt == 7u);
        t.lvt = 0;
    } else if (lvt == 5u) { /* G4: extended length (D1: always, any tag/class) */
        if (i >= len)
            return -1;
        uint8_t e = buf[i++];
        if (e <= 253u) {
            t.lvt = e;
        } else {
            size_t n = (e == 254u) ? 2u : 4u;
            if (len - i < n)
                return -1;
            uint32_t l = 0;
            for (size_t k = 0; k < n; k++)
                l = (l << 8) | buf[i++];
            t.lvt = l; /* D1b: non-shortest forms accepted */
        }
    } else {
        t.lvt = lvt;
    }
    *out = t;
    return (int)i;
}

int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out)
{
    if (out == NULL)
        return -1;
    bac_tag_t t;
    int h = bac_dec_tag(buf, len, &t);
    if (h < 0)
        return -1; /* D2: header error */
    if (t.context)
        return -1; /* D2: context class, opening/closing tags */

    size_t hl = (size_t)h;
    unsigned raw = buf[0] & 0x07u;
    bac_value_t v = { 0 };
    v.tag = t.tag;

    /* Content length and content-independent checks */
    size_t clen;
    switch (t.tag) {
    case BAC_TAG_NULL:
        if (raw != 0u)
            return -1; /* D3 */
        clen = 0;
        break;
    case BAC_TAG_BOOLEAN:
        if (raw > 1u)
            return -1; /* D3 */
        v.v.boolean = (raw == 1u);
        clen = 0;
        break;
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_SIGNED:
    case BAC_TAG_ENUMERATED:
        if (t.lvt < 1u || t.lvt > 4u)
            return -1; /* D4 */
        clen = (size_t)t.lvt;
        break;
    case BAC_TAG_REAL:
    case BAC_TAG_DATE:
    case BAC_TAG_TIME:
    case BAC_TAG_OBJECT_ID:
        if (t.lvt != 4u)
            return -1; /* D5, D9 */
        clen = 4;
        break;
    case BAC_TAG_OCTET_STRING:
    case BAC_TAG_CHARACTER_STRING:
    case BAC_TAG_BIT_STRING:
        if ((uint64_t)t.lvt > (uint64_t)(len - hl))
            return -1; /* D2: content beyond len */
        clen = (size_t)t.lvt;
        if (t.tag != BAC_TAG_OCTET_STRING && clen < 1u)
            return -1; /* D7, D8 */
        break;
    default:
        return -1; /* D2: 5 (Double), 13, 14, >= 15 */
    }
    if (clen > len - hl)
        return -1; /* D2: content beyond len */
    if (hl + clen > (size_t)INT_MAX)
        return -1; /* consumed size not representable in the return type */

    const uint8_t *c = buf + hl;
    uint32_t u = 0;
    for (size_t k = 0; k < clen && k < 4u; k++)
        u = (u << 8) | c[k];

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        v.v.u = u;
        break;
    case BAC_TAG_SIGNED:
        if (clen < 4u && (u & ((uint32_t)1 << (8u * clen - 1u))) != 0u)
            u |= ~(((uint32_t)1 << (8u * clen)) - 1u); /* sign-extend */
        v.v.i = (u & 0x80000000u) ? (int32_t)(-(int32_t)(~u) - 1) : (int32_t)u;
        break;
    case BAC_TAG_REAL:
        bits_to_float(&v.v.r, u); /* D5: NaN accepted, bit-exact */
        break;
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = c;
        v.v.octets.len = clen;
        break;
    case BAC_TAG_CHARACTER_STRING:
        v.v.str.charset = c[0]; /* D7: reported as-is */
        v.v.str.data = c + 1;
        v.v.str.len = clen - 1u;
        break;
    case BAC_TAG_BIT_STRING: {
        uint8_t unused = c[0];
        if (unused > 7u)
            return -1; /* D8 */
        if (clen == 1u && unused != 0u)
            return -1; /* D8 */
        if (clen - 1u > SIZE_MAX / 8u)
            return -1; /* nbits not representable */
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (clen - 1u) * 8u - unused;
        break;
    }
    case BAC_TAG_DATE:
        v.v.date.year = (c[0] == 255u) ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID:
        v.v.oid.type = (uint16_t)(u >> 22);
        v.v.oid.instance = u & 0x3FFFFFu;
        break;
    default: /* NULL, BOOLEAN: nothing more */
        break;
    }
    *out = v;
    return (int)(hl + clen);
}
