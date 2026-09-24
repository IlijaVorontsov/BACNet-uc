#!/usr/bin/env python3
"""Build injected-bug variants of the v1.0 references (all bugs + single-bug variants)."""
import os
LAB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def sub(s, old, new):
    assert s.count(old) >= 1, old
    return s.replace(old, new, 1)

P_BUGS = {
 "PB1_signed_minimal[STD]": [("    if (v >= -128 && v <= 127)\n        return 1;", "    if (v > -128 && v <= 127)\n        return 1;")],
 "PB2_extlen_253[STD]": [("        if (len <= 253)\n            n += 1;", "        if (len < 253)\n            n += 1;"),
                         ("        if (len <= 253) {\n            buf[i++] = (uint8_t)len;", "        if (len < 253) {\n            buf[i++] = (uint8_t)len;")],
 "PB3_ctx_boolean[STD]": [('''    size_t h = hdr_len(tag, 1);
    if (!buf || cap < h + 1)
        return -1;
    put_hdr(buf, tag, true, 1);
    buf[h] = v ? 1 : 0;
    return (int)(h + 1);''', '''    size_t h = hdr_len(tag, 0);
    if (!buf || cap < h)
        return -1;
    put_hdr(buf, tag, true, v ? 1 : 0);
    return (int)h;''')],
 "PB4_partial_write[POL]": [('''    size_t h = hdr_len(BAC_TAG_OCTET_STRING, (uint32_t)len);
    if (!buf || cap < h || cap - h < len)
        return -1;
    put_hdr(buf, BAC_TAG_OCTET_STRING, false, (uint32_t)len);''', '''    size_t h = hdr_len(BAC_TAG_OCTET_STRING, (uint32_t)len);
    if (!buf || cap < h)
        return -1;
    put_hdr(buf, BAC_TAG_OCTET_STRING, false, (uint32_t)len);
    if (cap - h < len)
        return -1;''')],
 "PB5_bits_validation[POL]": [('''    for (size_t i = 0; i < nbits; i++)
        if (bits[i] > 1)
            return -1;
''', '')],
}

def xb4(c):
    for fn in ("alarm_tick", "alarm_ack", "alarm_set_maintenance"):
        i = c.index(f"int {fn}(")
        old = "    if (!accept_time(p, now))\n        return 0;\n"
        j = c.index(old, i)
        c = c[:j] + "    if (now > p->last_now)\n        p->last_now = now;\n" + c[j + len(old):]
    return c

X_BUGS = {
 "XB1_return_not_strict[STD]": [("        if (v < c->high_limit - c->deadband)\n            return ALARM_NORMAL;", "        if (v <= c->high_limit - c->deadband)\n            return ALARM_NORMAL;")],
 "XB2_no_direct_high_low[STD]": [('''    case ALARM_HIGH:
        if (v < c->low_limit)
            return ALARM_LOW;
        if''', '''    case ALARM_HIGH:
        if''')],
 "XB3_catchup_no_episode[POL]": [('''        if (p->state != p->last_notified)
            notify_state(p, now, out, &n);''', '''        if (p->state != p->last_notified) {
            emit(out, &n, now, to_event[p->state], p->last_value);
            p->last_notified = p->state;
        }''')],
 "XB4_time_check_samples_only[POL]": xb4,
 "XB5_recovery_not_evaluated[POL]": [('''        if (p->state == ALARM_FAULT)
            transition(p, ALARM_NORMAL, now, out, &n);
        uint8_t t''', '''        if (p->state == ALARM_FAULT) {
            transition(p, ALARM_NORMAL, now, out, &n);
            return n;
        }
        uint8_t t''')],
}

def apply(c, patch):
    if callable(patch):
        return patch(c)
    for old, new in patch:
        c = sub(c, old, new)
    return c

for d, fn, bugs in (("p", "bacapp.c", P_BUGS), ("x", "alarm.c", X_BUGS)):
    base = open(f"{LAB}/{d}/ref/v1.0/{fn}").read()
    allc = base
    for name, patch in bugs.items():
        single = apply(base, patch)
        os.makedirs(f"{LAB}/{d}/bugs/single/{name}", exist_ok=True)
        open(f"{LAB}/{d}/bugs/single/{name}/{fn}", "w").write(single)
        allc = apply(allc, patch)
    open(f"{LAB}/{d}/bugs/{fn}", "w").write(allc)
print("ok")
