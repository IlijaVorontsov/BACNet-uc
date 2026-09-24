/* bacapp.c - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2)
 *
 * Implements SPEC.md v1.0.  Freestanding C11: no heap, no stdio, no libc calls
 * (only <stdint.h>, <stdbool.h>, <stddef.h>, <limits.h>).
 *
 * Encoder strategy (G1): every encoder first validates all arguments and
 * computes the exact encoded length; only when the whole value fits into cap
 * bytes (and the length is representable as an int) is anything written.
 * On any error buf is not touched at all.
 */
#include "bacapp.h"

#include <limits.h>

_Static_assert(CHAR_BIT == 8, "8-bit bytes required");
_Static_assert(sizeof(float) == 4, "float must be IEEE-754 binary32");

#define CLASS_CTX 0x08u
#define LVT_EXT 5u
#define LVT_OPEN 6u
#define LVT_CLOSE 7u
#define TAG_EXT 15u      /* tag-number nibble meaning "extended tag number follows" */
#define TAG_MAX 254u     /* tag number 255 is reserved (G3) */
#define RESULT_MAX ((size_t)INT_MAX) /* return values are int */

/* ------------------------------------------------------------------------ */
/* Helpers                                                                  */
/* ------------------------------------------------------------------------ */

/* Write the low n octets of v big-endian. */
static void put_be(uint8_t *p, uint32_t v, unsigned n)
{
    while (n > 0) {
        n--;
        p[n] = (uint8_t)(v & 0xFFu);
        v >>= 8;
    }
}

static uint32_t get_be(const uint8_t *p, size_t n)
{
    uint32_t v = 0;
    for (size_t i = 0; i < n; i++)
        v = (v << 8) | p[i];
    return v;
}

/* memmove without libc (source may overlap destination). */
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

/* Length of a data (non opening/closing) tag header, shortest form (G3, G4). */
static size_t hdr_len(unsigned tag, size_t clen)
{
    size_t n = 1;
    if (tag >= TAG_EXT)
        n += 1;
    if (clen <= 4)
        ;
    else if (clen <= 253)
        n += 1;
    else if (clen <= 65535)
        n += 3;
    else
        n += 5;
    return n;
}

/* Write a data tag header (caller has checked tag <= TAG_MAX,
 * clen <= 0xFFFFFFFF and the capacity). Returns the header length. */
static size_t hdr_write(uint8_t *buf, unsigned tag, bool ctx, size_t clen)
{
    uint8_t *p = buf;
    unsigned lvt = clen <= 4 ? (unsigned)clen : LVT_EXT;
    *p++ = (uint8_t)(((tag >= TAG_EXT ? TAG_EXT : tag) << 4) | (ctx ? CLASS_CTX : 0u) | lvt);
    if (tag >= TAG_EXT)
        *p++ = (uint8_t)tag;
    if (clen > 4) {
        if (clen <= 253) {
            *p++ = (uint8_t)clen;
        } else if (clen <= 65535) {
            *p++ = 254;
            put_be(p, (uint32_t)clen, 2);
            p += 2;
        } else {
            *p++ = 255;
            put_be(p, (uint32_t)clen, 4);
            p += 4;
        }
    }
    return (size_t)(p - buf);
}

/* Emit one tagged value whose content is [prefix octet] + data[0..dlen).
 * prefix < 0 means no prefix octet.  All-or-nothing (G1). */
static int emit(uint8_t *buf, size_t cap, unsigned tag, bool ctx, int prefix,
                const uint8_t *data, size_t dlen)
{
    size_t pre = prefix >= 0 ? 1u : 0u;
    size_t clen, hl, total;

    if (buf == NULL || tag > TAG_MAX || (data == NULL && dlen != 0))
        return -1;
    /* header <= 7 octets, prefix <= 1: keeps total <= INT_MAX (< 2^32-1, G4) */
    if (dlen > RESULT_MAX - 8)
        return -1;
    clen = dlen + pre;
    hl = hdr_len(tag, clen);
    total = hl + clen;
    if (total > cap)
        return -1;

    /* content first, so a source overlapping the header area is read intact */
    move_bytes(buf + hl + pre, data, dlen);
    (void)hdr_write(buf, tag, ctx, clen);
    if (pre)
        buf[hl] = (uint8_t)prefix;
    return (int)total;
}

/* Minimum number of octets for an unsigned value (at least one). */
static unsigned u_len(uint32_t v)
{
    if (v > 0xFFFFFFu) return 4;
    if (v > 0xFFFFu) return 3;
    if (v > 0xFFu) return 2;
    return 1;
}

/* Minimum number of octets for a two's-complement value (at least one). */
static unsigned s_len(int32_t v)
{
    if (v >= -128 && v <= 127) return 1;
    if (v >= -32768 && v <= 32767) return 2;
    if (v >= -8388608 && v <= 8388607) return 3;
    return 4;
}

static int enc_uint(uint8_t *buf, size_t cap, unsigned tag, bool ctx, uint32_t v)
{
    uint8_t c[4];
    unsigned n = u_len(v);
    put_be(c, v, n);
    return emit(buf, cap, tag, ctx, -1, c, n);
}

static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, unsigned lvt)
{
    size_t n = tag >= TAG_EXT ? 2u : 1u;
    if (buf == NULL || tag > TAG_MAX || n > cap)
        return -1;
    buf[0] = (uint8_t)(((tag >= TAG_EXT ? TAG_EXT : tag) << 4) | CLASS_CTX | lvt);
    if (n == 2)
        buf[1] = tag;
    return (int)n;
}

/* ------------------------------------------------------------------------ */
/* Application-tagged encoders                                              */
/* ------------------------------------------------------------------------ */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    return emit(buf, cap, BAC_TAG_NULL, false, -1, NULL, 0);
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    /* E2: value lives in the LVT field, no content octets */
    if (buf == NULL || cap < 1)
        return -1;
    buf[0] = (uint8_t)((BAC_TAG_BOOLEAN << 4) | (v ? 1u : 0u));
    return 1;
}

int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_UNSIGNED, false, v);
}

int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v)
{
    return enc_uint(buf, cap, BAC_TAG_ENUMERATED, false, v);
}

int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v)
{
    uint8_t c[4];
    unsigned n = s_len(v);
    put_be(c, (uint32_t)v, n); /* conversion to uint32_t is modulo 2^32 */
    return emit(buf, cap, BAC_TAG_SIGNED, false, -1, c, n);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    union { float f; uint32_t u; } pun;
    uint8_t c[4];
    pun.f = v;
    /* E5a: refuse every NaN (exponent all ones, non-zero mantissa) */
    if ((pun.u & 0x7F800000u) == 0x7F800000u && (pun.u & 0x007FFFFFu) != 0)
        return -1;
    put_be(c, pun.u, 4);
    return emit(buf, cap, BAC_TAG_REAL, false, -1, c, 4);
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    return emit(buf, cap, BAC_TAG_OCTET_STRING, false, -1, data, len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    /* E7: charset 0 (UTF-8); E7a: bytes are not validated */
    return emit(buf, cap, BAC_TAG_CHARACTER_STRING, false, 0,
                (const uint8_t *)utf8, len);
}

int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits)
{
    size_t nbytes, clen, hl, total;
    uint8_t *p;

    if (buf == NULL || (bits == NULL && nbits != 0))
        return -1;
    nbytes = nbits / 8 + (nbits % 8 != 0 ? 1u : 0u);
    if (nbytes > RESULT_MAX - 8)
        return -1;
    clen = nbytes + 1;
    hl = hdr_len(BAC_TAG_BIT_STRING, clen);
    total = hl + clen;
    if (total > cap)
        return -1;
    for (size_t i = 0; i < nbits; i++) /* E8a */
        if (bits[i] > 1)
            return -1;

    p = buf + hdr_write(buf, BAC_TAG_BIT_STRING, false, clen);
    *p++ = (uint8_t)((8 - nbits % 8) % 8); /* unused bits in the last octet */
    for (size_t j = 0; j < nbytes; j++) {
        uint8_t o = 0;
        for (unsigned k = 0; k < 8; k++) {
            size_t i = j * 8 + k;
            if (i < nbits && bits[i])
                o |= (uint8_t)(0x80u >> k);
        }
        p[j] = o; /* unused trailing bits are 0 */
    }
    return (int)total;
}

int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday)
{
    uint8_t c[4];
    if (year == BAC_YEAR_UNSPECIFIED)
        c[0] = 255;
    else if (year >= 1900 && year <= 2154)
        c[0] = (uint8_t)(year - 1900);
    else
        return -1;
    if (!((month >= 1 && month <= 14) || month == 255))
        return -1;
    if (!((day >= 1 && day <= 34) || day == 255))
        return -1;
    if (!((wday >= 1 && wday <= 7) || wday == 255))
        return -1;
    c[1] = month;
    c[2] = day;
    c[3] = wday;
    return emit(buf, cap, BAC_TAG_DATE, false, -1, c, 4);
}

int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths)
{
    uint8_t c[4];
    if ((hour > 23 && hour != 255) || (minute > 59 && minute != 255) ||
        (second > 59 && second != 255) || (hundredths > 99 && hundredths != 255))
        return -1;
    c[0] = hour;
    c[1] = minute;
    c[2] = second;
    c[3] = hundredths;
    return emit(buf, cap, BAC_TAG_TIME, false, -1, c, 4);
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    uint8_t c[4];
    if (type > 1023u || instance > 4194303u) /* E11a */
        return -1;
    put_be(c, ((uint32_t)type << 22) | instance, 4);
    return emit(buf, cap, BAC_TAG_OBJECT_ID, false, -1, c, 4);
}

/* ------------------------------------------------------------------------ */
/* Context-tagged encoders                                                  */
/* ------------------------------------------------------------------------ */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    uint8_t c = v ? 1u : 0u;
    return emit(buf, cap, tag, true, -1, &c, 1);
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_OPEN);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, LVT_CLOSE);
}

/* ------------------------------------------------------------------------ */
/* Decoders                                                                 */
/* ------------------------------------------------------------------------ */

int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out)
{
    bac_tag_t t;
    size_t pos = 1;
    unsigned lvt;

    if (buf == NULL || out == NULL || len == 0)
        return -1;

    t.tag = (uint8_t)(buf[0] >> 4);
    t.context = (buf[0] & CLASS_CTX) != 0;
    t.opening = false;
    t.closing = false;
    t.lvt = 0;
    lvt = buf[0] & 0x07u;

    if (t.tag == TAG_EXT) {
        if (pos >= len)
            return -1;              /* truncated extended tag */
        if (buf[pos] == 255)
            return -1;              /* reserved */
        t.tag = buf[pos++];         /* D1b: values < 15 accepted */
    }

    if (lvt == LVT_OPEN || lvt == LVT_CLOSE) {
        if (!t.context)
            return -1;              /* D1a */
        t.opening = lvt == LVT_OPEN;
        t.closing = lvt == LVT_CLOSE;
    } else if (lvt == LVT_EXT) {
        uint8_t x;
        if (pos >= len)
            return -1;
        x = buf[pos++];
        if (x <= 253) {
            t.lvt = x;              /* D1b: values < 5 accepted */
        } else {
            size_t n = x == 254 ? 2u : 4u;
            if (len - pos < n)
                return -1;
            t.lvt = get_be(buf + pos, n);
            pos += n;
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
    size_t clen, total;
    int r;
    unsigned raw;

    if (out == NULL)
        return -1;
    r = bac_dec_tag(buf, len, &t);
    if (r < 0 || t.context)
        return -1;
    raw = buf[0] & 0x07u;
    v.tag = t.tag;

    /* D3: Null / Boolean use the raw LVT bits and have no content */
    if (t.tag == BAC_TAG_NULL || t.tag == BAC_TAG_BOOLEAN) {
        if (t.tag == BAC_TAG_NULL) {
            if (raw != 0)
                return -1;
        } else {
            if (raw > 1)
                return -1;
            v.v.boolean = raw == 1;
        }
        *out = v;
        return r;
    }

    clen = t.lvt;
    if (clen > len - (size_t)r)
        return -1;                  /* content beyond len */
    if (clen > RESULT_MAX - (size_t)r)
        return -1;                  /* not representable as int */
    total = (size_t)r + clen;
    c = buf + r;

    switch (t.tag) {
    case BAC_TAG_UNSIGNED:
    case BAC_TAG_ENUMERATED:
        if (clen < 1 || clen > 4)
            return -1;
        v.v.u = get_be(c, clen);
        break;
    case BAC_TAG_SIGNED: {
        uint32_t u;
        if (clen < 1 || clen > 4)
            return -1;
        u = get_be(c, clen);
        if (clen < 4 && (c[0] & 0x80u))
            u |= 0xFFFFFFFFu << (8 * clen); /* sign-extend */
        v.v.i = u <= 0x7FFFFFFFu ? (int32_t)u : -(int32_t)(~u) - 1;
        break;
    }
    case BAC_TAG_REAL: {
        union { float f; uint32_t u; } pun;
        if (clen != 4)
            return -1;
        pun.u = get_be(c, 4);
        v.v.r = pun.f;
        break;
    }
    case BAC_TAG_OCTET_STRING:
        v.v.octets.data = c;
        v.v.octets.len = clen;
        break;
    case BAC_TAG_CHARACTER_STRING:
        if (clen < 1)
            return -1;
        v.v.str.charset = c[0];
        v.v.str.data = c + 1;
        v.v.str.len = clen - 1;
        break;
    case BAC_TAG_BIT_STRING:
        if (clen < 1 || c[0] > 7 || (clen == 1 && c[0] != 0))
            return -1;
        if (clen - 1 > SIZE_MAX / 8)
            return -1;
        v.v.bits.data = c + 1;
        v.v.bits.nbits = (clen - 1) * 8 - c[0];
        break;
    case BAC_TAG_DATE:
        if (clen != 4)
            return -1;
        v.v.date.year = c[0] == 255 ? (uint16_t)BAC_YEAR_UNSPECIFIED : (uint16_t)(1900u + c[0]);
        v.v.date.month = c[1];
        v.v.date.day = c[2];
        v.v.date.wday = c[3];
        break;
    case BAC_TAG_TIME:
        if (clen != 4)
            return -1;
        v.v.time.hour = c[0];
        v.v.time.minute = c[1];
        v.v.time.second = c[2];
        v.v.time.hundredths = c[3];
        break;
    case BAC_TAG_OBJECT_ID: {
        uint32_t u;
        if (clen != 4)
            return -1;
        u = get_be(c, 4);
        v.v.oid.type = (uint16_t)(u >> 22);
        v.v.oid.instance = u & 0x3FFFFFu;
        break;
    }
    default:
        /* D2: 5 (Double, unsupported in v1.0), 13, 14, >= 15 */
        return -1;
    }

    *out = v;
    return (int)total;
}
