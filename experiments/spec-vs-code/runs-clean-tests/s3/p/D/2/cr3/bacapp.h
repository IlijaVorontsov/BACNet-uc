/* bacapp.h - BACnet application-layer primitive encoding (ASHRAE 135 clause 20.2) */
#ifndef BACAPP_H
#define BACAPP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Application tag numbers */
enum {
    BAC_TAG_NULL = 0,
    BAC_TAG_BOOLEAN = 1,
    BAC_TAG_UNSIGNED = 2,
    BAC_TAG_SIGNED = 3,
    BAC_TAG_REAL = 4,
    BAC_TAG_DOUBLE = 5,
    BAC_TAG_OCTET_STRING = 6,
    BAC_TAG_CHARACTER_STRING = 7,
    BAC_TAG_BIT_STRING = 8,
    BAC_TAG_ENUMERATED = 9,
    BAC_TAG_DATE = 10,
    BAC_TAG_TIME = 11,
    BAC_TAG_OBJECT_ID = 12
};

#define BAC_YEAR_UNSPECIFIED 0xFFFFu

/* ---- Encoders ------------------------------------------------------------
 * Each encoder writes one complete tagged value into buf (capacity cap bytes)
 * and returns the number of bytes written, or -1 on error.
 */
int bac_enc_null(uint8_t *buf, size_t cap);
int bac_enc_boolean(uint8_t *buf, size_t cap, bool v);
int bac_enc_unsigned(uint8_t *buf, size_t cap, uint32_t v);
int bac_enc_signed(uint8_t *buf, size_t cap, int32_t v);
int bac_enc_real(uint8_t *buf, size_t cap, float v);
int bac_enc_double(uint8_t *buf, size_t cap, double v);
int bac_enc_octet_string(uint8_t *buf, size_t cap, const uint8_t *data, size_t len);
/* utf8: len bytes of UTF-8 text (not NUL-terminated); returns -1 if the text is
 * not well-formed UTF-8 (RFC 3629: no overlong forms, surrogates or code points
 * above U+10FFFF) */
int bac_enc_char_string(uint8_t *buf, size_t cap, const char *utf8, size_t len);
/* bits[i] is the value of bit i (bit 0 is the first bit of the string) */
int bac_enc_bit_string(uint8_t *buf, size_t cap, const uint8_t *bits, size_t nbits);
int bac_enc_enumerated(uint8_t *buf, size_t cap, uint32_t v);
/* year is the calendar year (e.g. 2024) or BAC_YEAR_UNSPECIFIED; 255 = unspecified for the others */
int bac_enc_date(uint8_t *buf, size_t cap, uint16_t year, uint8_t month, uint8_t day, uint8_t wday);
int bac_enc_time(uint8_t *buf, size_t cap, uint8_t hour, uint8_t minute, uint8_t second, uint8_t hundredths);
int bac_enc_object_id(uint8_t *buf, size_t cap, uint16_t type, uint32_t instance);

/* Context-tagged encoders (tag = context tag number) */
int bac_enc_ctx_unsigned(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v);
int bac_enc_ctx_enumerated(uint8_t *buf, size_t cap, uint8_t tag, uint32_t v);
int bac_enc_ctx_signed(uint8_t *buf, size_t cap, uint8_t tag, int32_t v);
int bac_enc_ctx_boolean(uint8_t *buf, size_t cap, uint8_t tag, bool v);
int bac_enc_opening_tag(uint8_t *buf, size_t cap, uint8_t tag);
int bac_enc_closing_tag(uint8_t *buf, size_t cap, uint8_t tag);

/* ---- Decoders ------------------------------------------------------------ */
typedef struct {
    uint8_t tag;      /* tag number */
    bool context;     /* true = context class, false = application class */
    bool opening;     /* opening tag */
    bool closing;     /* closing tag */
    uint32_t lvt;     /* length/value/type after extended-length processing
                         (not meaningful for opening/closing tags) */
} bac_tag_t;

/* Decode one tag header from buf[0..len). Returns the header length in bytes, or -1. */
int bac_dec_tag(const uint8_t *buf, size_t len, bac_tag_t *out);

typedef struct {
    uint8_t tag; /* application tag number (BAC_TAG_...) */
    union {
        bool boolean;
        uint32_t u;       /* UNSIGNED, ENUMERATED */
        int32_t i;        /* SIGNED */
        float r;          /* REAL */
        double d;         /* DOUBLE */
        struct { const uint8_t *data; size_t len; } octets;                 /* OCTET_STRING */
        struct { uint8_t charset; const uint8_t *data; size_t len; } str;   /* CHARACTER_STRING */
        struct { const uint8_t *data; size_t nbits; } bits;  /* BIT_STRING: packed bytes, bit 0 = MSB of data[0] */
        struct { uint16_t year; uint8_t month, day, wday; } date;           /* year as in bac_enc_date */
        struct { uint8_t hour, minute, second, hundredths; } time;
        struct { uint16_t type; uint32_t instance; } oid;
    } v;
} bac_value_t;

/* Decode one complete application-tagged value from buf[0..len).
 * Returns the number of bytes consumed, or -1. Pointers in *out point into buf. */
int bac_dec_app(const uint8_t *buf, size_t len, bac_value_t *out);

#endif
