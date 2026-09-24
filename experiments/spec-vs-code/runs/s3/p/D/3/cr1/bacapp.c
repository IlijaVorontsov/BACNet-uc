/* bacapp.c - BACnet application-layer primitive encoding */
#include "bacapp.h"

#include <math.h>
#include <string.h>

/* bac_enc_double / bac_dec_app (Double) copy the object representation of a
 * C double as an IEEE-754 binary64 value.  Refuse to build on targets where
 * double is not 64 bits (e.g. toolchains with a 32-bit double). */
_Static_assert(sizeof(double) == sizeof(uint64_t), "bacapp: double must be IEEE-754 binary64 (8 octets)");

/* ---- tag header helpers ---- */

static size_t hdr_len(uint8_t tag, uint32_t len)
{
    size_t n = 1;
    if (tag >= 15)
        n += 1;
    if (len >= 5) {
        if (len <= 253)
            n += 1;
        else if (len <= 65535)
            n += 3;
        else
            n += 5;
    }
    return n;
}

static size_t put_hdr(uint8_t *buf, uint8_t tag, bool ctx, uint32_t len)
{
    size_t i = 1;
    uint8_t b = ctx ? 0x08 : 0x00;
    if (tag < 15) {
        b |= (uint8_t)(tag << 4);
    } else {
        b |= 0xF0;
        buf[i++] = tag;
    }
    if (len < 5) {
        b |= (uint8_t)len;
    } else {
        b |= 5;
        if (len <= 253) {
            buf[i++] = (uint8_t)len;
        } else if (len <= 65535) {
            buf[i++] = 254;
            buf[i++] = (uint8_t)(len >> 8);
            buf[i++] = (uint8_t)len;
        } else {
            buf[i++] = 255;
            buf[i++] = (uint8_t)(len >> 24);
            buf[i++] = (uint8_t)(len >> 16);
            buf[i++] = (uint8_t)(len >> 8);
            buf[i++] = (uint8_t)len;
        }
    }
    buf[0] = b;
    return i;
}

static size_t uint_len(uint32_t v)
{
    if (v < 0x100u)
        return 1;
    if (v < 0x10000u)
        return 2;
    if (v < 0x1000000u)
        return 3;
    return 4;
}

static size_t sint_len(int32_t v)
{
    if (v >= -128 && v <= 127)
        return 1;
    if (v >= -32768 && v <= 32767)
        return 2;
    if (v >= -8388608 && v <= 8388607)
        return 3;
    return 4;
}

static void put_be(uint8_t *p, uint32_t v, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = (uint8_t)(v >> (8 * (n - 1 - i)));
}

static int enc_uint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, uint32_t v)
{
    size_t n = uint_len(v);
    size_t h = hdr_len(tag, (uint32_t)n);
    if (!buf || cap < h + n)
        return -1;
    put_hdr(buf, tag, ctx, (uint32_t)n);
    put_be(buf + h, v, n);
    return (int)(h + n);
}

static int enc_sint(uint8_t *buf, size_t cap, uint8_t tag, bool ctx, int32_t v)
{
    size_t n = sint_len(v);
    size_t h = hdr_len(tag, (uint32_t)n);
    if (!buf || cap < h + n)
        return -1;
    put_hdr(buf, tag, ctx, (uint32_t)n);
    put_be(buf + h, (uint32_t)v, n);
    return (int)(h + n);
}

/* ---- application encoders ---- */

int bac_enc_null(uint8_t *buf, size_t cap)
{
    if (!buf || cap < 1)
        return -1;
    buf[0] = 0x00;
    return 1;
}

int bac_enc_boolean(uint8_t *buf, size_t cap, bool v)
{
    if (!buf || cap < 1)
        return -1;
    buf[0] = (uint8_t)(0x10 | (v ? 1 : 0));
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
    return enc_sint(buf, cap, BAC_TAG_SIGNED, false, v);
}

int bac_enc_real(uint8_t *buf, size_t cap, float v)
{
    uint32_t bits;
    if (isnan(v))
        return -1;
    if (!buf || cap < 5)
        return -1;
    memcpy(&bits, &v, sizeof bits);
    buf[0] = 0x44;
    put_be(buf + 1, bits, 4);
    return 5;
}

/* E5b: Double, 8 octets IEEE-754 binary64 big-endian: 55 08 xx xx xx xx xx xx xx xx.
 * NaN is refused like for Real (E5a / decision D-7). */
int bac_enc_double(uint8_t *buf, size_t cap, double v)
{
    uint64_t bits;
    if (isnan(v))
        return -1;
    if (!buf || cap < 10)
        return -1;
    memcpy(&bits, &v, sizeof bits);
    buf[0] = 0x55; /* application tag 5, LVT = 5 (extended length) */
    buf[1] = 0x08; /* content length 8 */
    put_be(buf + 2, (uint32_t)(bits >> 32), 4);
    put_be(buf + 6, (uint32_t)bits, 4);
    return 10;
}

int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len)
{
    if (len > 0xFFFFFFFFu || (len && !data))
        return -1;
    size_t h = hdr_len(BAC_TAG_OCTET_STRING, (uint32_t)len);
    if (!buf || cap < h || cap - h < len)
        return -1;
    put_hdr(buf, BAC_TAG_OCTET_STRING, false, (uint32_t)len);
    if (len)
        memcpy(buf + h, data, len);
    return (int)(h + len);
}

int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len)
{
    if (len >= 0xFFFFFFFFu || (len && !utf8))
        return -1;
    uint32_t clen = (uint32_t)len + 1;
    size_t h = hdr_len(BAC_TAG_CHARACTER_STRING, clen);
    if (!buf || cap < h || cap - h < clen)
        return -1;
    put_hdr(buf, BAC_TAG_CHARACTER_STRING, false, clen);
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
    size_t h = hdr_len(BAC_TAG_BIT_STRING, clen);
    if (!buf || cap < h || cap - h < clen)
        return -1;
    put_hdr(buf, BAC_TAG_BIT_STRING, false, clen);
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
    if (!buf || cap < 5)
        return -1;
    buf[0] = 0xA4;
    buf[1] = y;
    buf[2] = month;
    buf[3] = day;
    buf[4] = wday;
    return 5;
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
    if (!buf || cap < 5)
        return -1;
    buf[0] = 0xB4;
    buf[1] = hour;
    buf[2] = minute;
    buf[3] = second;
    buf[4] = hundredths;
    return 5;
}

int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance)
{
    if (type > 1023 || instance > 0x3FFFFFu)
        return -1;
    if (!buf || cap < 5)
        return -1;
    buf[0] = 0xC4;
    put_be(buf + 1, ((uint32_t)type << 22) | instance, 4);
    return 5;
}

/* ---- context encoders ---- */

int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    if (tag == 255)
        return -1;
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v)
{
    if (tag == 255)
        return -1;
    return enc_uint(buf, cap, tag, true, v);
}

int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v)
{
    if (tag == 255)
        return -1;
    return enc_sint(buf, cap, tag, true, v);
}

int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v)
{
    if (tag == 255)
        return -1;
    size_t h = hdr_len(tag, 1);
    if (!buf || cap < h + 1)
        return -1;
    put_hdr(buf, tag, true, 1);
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);
}

static int enc_open_close(uint8_t *buf, size_t cap, uint8_t tag, uint8_t lvt)
{
    if (tag == 255)
        return -1;
    size_t n = tag < 15 ? 1 : 2;
    if (!buf || cap < n)
        return -1;
    if (tag < 15) {
        buf[0] = (uint8_t)((tag << 4) | 0x08 | lvt);
    } else {
        buf[0] = (uint8_t)(0xF0 | 0x08 | lvt);
        buf[1] = tag;
    }
    return (int)n;
}

int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, 6);
}

int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag)
{
    return enc_open_close(buf, cap, tag, 7);
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
