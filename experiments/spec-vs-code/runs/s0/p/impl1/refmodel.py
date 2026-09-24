#!/usr/bin/env python3
"""Independent reference model of SPEC.md + random differential test generator.
Writes fuzz.vec (inputs + expected outputs computed by the model)."""
import random, struct, sys
from fmt import fmt, hdr   # noqa  (fmt.py prints at import; silence below)

def ok(b): return "OK " + fmt(b) if b else "OK -"

def uns(v):
    n = 1 if v < 1<<8 else 2 if v < 1<<16 else 3 if v < 1<<24 else 4
    return v.to_bytes(n, 'big')
def sig(v):
    for n in (1,2,3,4):
        if -(1<<(8*n-1)) <= v < (1<<(8*n-1)): return (v & ((1<<(8*n))-1)).to_bytes(n,'big')

def m_enc(cmd, args, cap):
    def fit(b): return ok(b) if len(b) <= cap else "ERR"
    if cmd == "enc_null": return fit(b'\x00')
    if cmd == "enc_bool": return fit(bytes([0x10|args[0]]))
    if cmd == "enc_unsigned": v=uns(args[0]); return fit(hdr(2,len(v))+v)
    if cmd == "enc_enum": v=uns(args[0]); return fit(hdr(9,len(v))+v)
    if cmd == "enc_signed": v=sig(args[0]); return fit(hdr(3,len(v))+v)
    if cmd == "enc_real":
        u=args[0]
        if (u>>23)&0xff==0xff and u&0x7fffff: return "ERR"
        return fit(b'\x44'+u.to_bytes(4,'big'))
    if cmd == "enc_octets": d=args[0]; return fit(hdr(6,len(d))+d)
    if cmd == "enc_str": d=args[0]; return fit(hdr(7,len(d)+1)+b'\x00'+d)
    if cmd == "enc_bits":
        bits=args[0]
        if any(b>1 for b in bits): return "ERR"
        nb=(len(bits)+7)//8; out=bytearray(nb)
        for i,b in enumerate(bits):
            if b: out[i//8] |= 0x80>>(i%8)
        c=bytes([(8-len(bits)%8)%8])+bytes(out)
        return fit(hdr(8,len(c))+c)
    if cmd == "enc_date":
        y,m,d,w=args
        if y==0xffff: yo=255
        elif 1900<=y<=2154: yo=y-1900
        else: return "ERR"
        if not (1<=m<=14 or m==255): return "ERR"
        if not (1<=d<=34 or d==255): return "ERR"
        if not (1<=w<=7 or w==255): return "ERR"
        return fit(b'\xa4'+bytes([yo,m,d,w]))
    if cmd == "enc_time":
        h,mi,s,hu=args
        if not (h<=23 or h==255) or not (mi<=59 or mi==255) or not (s<=59 or s==255) or not (hu<=99 or hu==255): return "ERR"
        return fit(b'\xb4'+bytes(args))
    if cmd == "enc_oid":
        t,i=args
        if t>1023 or i>4194303: return "ERR"
        return fit(b'\xc4'+((t<<22)|i).to_bytes(4,'big'))
    if cmd == "enc_ctx_unsigned":
        t,v=args
        if t==255: return "ERR"
        v=uns(v); return fit(hdr(t,len(v),True)+v)
    if cmd == "enc_ctx_bool":
        t,v=args
        if t==255: return "ERR"
        return fit(hdr(t,1,True)+bytes([v]))
    if cmd in ("enc_open","enc_close"):
        t=args[0]
        if t==255: return "ERR"
        l=6 if cmd=="enc_open" else 7
        b=bytes([0xF8|l,t]) if t>=15 else bytes([t<<4|8|l])
        return fit(b)
    raise ValueError(cmd)

def m_tag(b):
    if not b: return None
    i=1; tag=b[0]>>4; ctx=bool(b[0]&8); lvt=b[0]&7
    if tag==15:
        if len(b)<2 or b[1]==255: return None
        tag=b[1]; i=2
    if lvt>=6:
        if not ctx: return None
        return (i,tag,ctx,lvt==6,lvt==7,0)
    if lvt==5:
        if len(b)<i+1: return None
        e=b[i]; i+=1
        if e<254: lvt=e
        else:
            n=2 if e==254 else 4
            if len(b)<i+n: return None
            lvt=int.from_bytes(b[i:i+n],'big'); i+=n
    return (i,tag,ctx,False,False,lvt)

def m_dec_tag(b):
    r=m_tag(b)
    if r is None: return "ERR"
    hl,tag,ctx,op,cl,lvt=r
    s=f"OK hl={hl} tag={tag} {'ctx' if ctx else 'app'}"
    if op: s+=" open"
    if cl: s+=" close"
    if not op and not cl: s+=f" lvt={lvt}"
    return s

def bitdigits(data, nbits):
    if nbits==0: return "-"
    assert nbits<=256
    return "".join('1' if (data[i//8]>>(7-i%8))&1 else '0' for i in range(nbits))

def m_dec_app(b):
    r=m_tag(b)
    if r is None: return "ERR"
    hl,tag,ctx,op,cl,lvt=r
    if ctx: return "ERR"
    if tag in (5,13,14) or tag>=15: return "ERR"
    raw=b[0]&7
    if tag==0:
        return "ERR" if raw!=0 else f"OK n={hl} null"
    if tag==1:
        return "ERR" if raw>1 else f"OK n={hl} bool {raw}"
    if hl+lvt>len(b): return "ERR"
    c=b[hl:hl+lvt]; n=hl+lvt
    if tag in (2,3,9):
        if not 1<=lvt<=4: return "ERR"
        u=int.from_bytes(c,'big')
        if tag==2: return f"OK n={n} u {u}"
        if tag==9: return f"OK n={n} enum {u}"
        if u>>(8*lvt-1): u-=1<<(8*lvt)
        return f"OK n={n} i {u}"
    if tag in (4,10,11,12):
        if lvt!=4: return "ERR"
        if tag==4: return f"OK n={n} r {c.hex()}"
        if tag==10: return f"OK n={n} date {65535 if c[0]==255 else 1900+c[0]} {c[1]} {c[2]} {c[3]}"
        if tag==11: return f"OK n={n} time {c[0]} {c[1]} {c[2]} {c[3]}"
        u=int.from_bytes(c,'big'); return f"OK n={n} oid {u>>22} {u&0x3fffff}"
    if tag==6: return f"OK n={n} oct {fmt(c) if c else '-'}"
    if tag==7:
        if lvt<1: return "ERR"
        return f"OK n={n} str cs={c[0]} {fmt(c[1:]) if len(c)>1 else '-'}"
    if tag==8:
        if lvt<1 or c[0]>7 or (lvt==1 and c[0]!=0): return "ERR"
        nb=(lvt-1)*8-c[0]
        if nb>256: return None  # skip (hash format)
        return f"OK n={n} bits {bitdigits(c[1:],nb)}"
    raise AssertionError

def hexs(b): return b.hex() if b else "-"

def gen(seed, count):
    R=random.Random(seed)
    lines=[]
    def interesting_u32():
        return R.choice([0,1,127,128,255,256,65535,65536,0xffffff,0x1000000,0xffffffff,R.getrandbits(32),R.getrandbits(R.randint(1,32))])
    def cap(need_hint):
        return R.choice([None, 0, 1, 2, 3, 4, 5, 6, R.randint(0, need_hint+3)])
    for _ in range(count):
        k=R.randrange(18)
        if k<3:
            # decoder fuzz: structured-random bytes
            n=R.randint(0,12)
            b=bytearray(R.getrandbits(8) for _ in range(n))
            if b and R.random()<0.5: b[0]=(R.randrange(16)<<4)|R.randrange(16)
            b=bytes(b)
            if R.random()<0.5:
                lines.append((f"dec_tag {hexs(b)}", m_dec_tag(b)))
            else:
                e=m_dec_app(b)
                if e is not None: lines.append((f"dec_app {hexs(b)}", e))
            continue
        if k<5:
            # well-formed-ish app values with random header tweaks
            tag=R.choice([0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15])
            clen=R.choice([0,1,2,3,4,5,6,R.randint(0,300)])
            content=bytes(R.getrandbits(8) for _ in range(clen))
            if tag==8 and clen and R.random()<0.7: content=bytes([R.randint(0,8)])+content[1:]
            form=R.randrange(4)
            if form==0 and clen<=4: h=bytes([tag<<4|clen]) if tag<15 else bytes([0xf0|clen, tag])
            elif form==1 and clen<=253: h=bytes([(min(tag,15))<<4|5]) + (bytes([tag]) if tag>=15 else b'') + bytes([clen])
            elif form==2: h=bytes([(min(tag,15))<<4|5]) + (bytes([tag]) if tag>=15 else b'') + b'\xfe'+clen.to_bytes(2,'big')
            else: h=hdr(tag, clen)
            if tag==15 and R.random()<0.5: h=bytes([0xf0|(h[0]&7), R.randrange(16)])+h[2:]
            b=h+content
            if R.random()<0.2: b=b[:R.randint(0,len(b))]
            if R.random()<0.2: b=b+bytes([R.getrandbits(8)])
            e=m_dec_app(b)
            if e is not None: lines.append((f"dec_app {hexs(b)}", e))
            continue
        cmds=["enc_null","enc_bool","enc_unsigned","enc_enum","enc_signed","enc_real","enc_octets","enc_str","enc_bits","enc_date","enc_time","enc_oid","enc_ctx_unsigned","enc_ctx_bool","enc_open","enc_close"]
        c=R.choice(cmds)
        if c=="enc_null": args=[]; s=[]
        elif c=="enc_bool": args=[R.randint(0,1)]; s=[str(args[0])]
        elif c in ("enc_unsigned","enc_enum"): args=[interesting_u32()]; s=[str(args[0])]
        elif c=="enc_signed":
            v=R.choice([0,-1,127,128,-128,-129,32767,32768,-32768,-32769,8388607,8388608,-8388608,-8388609,2**31-1,-2**31,R.randint(-2**31,2**31-1),R.randint(-300,300)])
            args=[v]; s=[str(v)]
        elif c=="enc_real":
            u=R.choice([R.getrandbits(32), 0x7f800000|R.getrandbits(23), 0xff800000|R.getrandbits(23), 0x7f800000,0xff800000,0,0x80000000,R.getrandbits(23)])
            args=[u]; s=["%08x"%u]
        elif c in ("enc_octets","enc_str"):
            n=R.choice([0,1,4,5,253,254,255,R.randint(0,600)])
            d=bytes(R.getrandbits(8) for _ in range(n)); args=[d]; s=[hexs(d)]
        elif c=="enc_bits":
            n=R.choice([0,1,7,8,9,15,16,17,R.randint(0,300)])
            bits=[R.randint(0,1) for _ in range(n)]
            if n and R.random()<0.2: bits[R.randrange(n)]=R.randint(2,9)
            args=[bits]; s=["".join(map(str,bits)) or "-"]
        elif c=="enc_date":
            args=[R.choice([1899,1900,2024,2154,2155,65535,65534,0,R.randint(0,65535)])]+[R.choice([0,1,7,12,13,14,15,31,32,33,34,35,254,255,R.randint(0,255)]) for _ in range(3)]
            s=list(map(str,args))
        elif c=="enc_time":
            args=[R.choice([0,23,24,59,60,99,100,254,255,R.randint(0,255)]) for _ in range(4)]; s=list(map(str,args))
        elif c=="enc_oid":
            args=[R.choice([0,1023,1024,65535,R.randint(0,1100)]), R.choice([0,4194303,4194304,0xffffffff,R.getrandbits(22),R.getrandbits(32)])]; s=list(map(str,args))
        elif c=="enc_ctx_unsigned":
            args=[R.choice([0,14,15,16,254,255,R.randint(0,255)]), interesting_u32()]; s=list(map(str,args))
        elif c=="enc_ctx_bool":
            args=[R.choice([0,14,15,254,255,R.randint(0,255)]), R.randint(0,1)]; s=list(map(str,args))
        else:
            args=[R.choice([0,1,14,15,16,254,255,R.randint(0,255)])]; s=list(map(str,args))
        cp=cap(700)
        capv=1024 if cp is None else cp
        line=" ".join([c]+s+([] if cp is None else [f"@{cp}"]))
        if len(line)>3000: continue
        lines.append((line, m_enc(c,args,capv)))
    return lines

if __name__=="__main__":
    seed=int(sys.argv[1]) if len(sys.argv)>1 else 1
    count=int(sys.argv[2]) if len(sys.argv)>2 else 20000
    lines=gen(seed,count)
    with open("fuzz.vec","w") as f:
        for i in range(0,len(lines),50):
            chunk=lines[i:i+50]
            f.write(f"### fuzz-{seed}-{i}\n")
            for l,_ in chunk: f.write(l+"\n")
            f.write("---\n")
            for _,e in chunk: f.write(e+"\n")
    print(len(lines),"vectors")
