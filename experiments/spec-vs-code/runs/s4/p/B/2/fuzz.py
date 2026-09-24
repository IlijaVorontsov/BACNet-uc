import random, struct, subprocess
R=random.Random(1)
def hdr(tag,ctx,L):
    b=0x08 if ctx else 0
    ext=b''
    if tag<15: b|=tag<<4
    else: b|=0xF0; ext=bytes([tag])
    if L<5: return bytes([b|L])+ext
    b|=5
    if L<=253: return bytes([b])+ext+bytes([L])
    if L<=65535: return bytes([b])+ext+bytes([254])+L.to_bytes(2,'big')
    return bytes([b])+ext+bytes([255])+L.to_bytes(4,'big')
def fit(out,cap): return 'OK '+out.hex() if len(out)<=cap else 'ERR'
def ulen(v): return 1 if v<256 else 2 if v<65536 else 3 if v<1<<24 else 4
def slen(v):
    for n in (1,2,3):
        if -(1<<(8*n-1))<=v<(1<<(8*n-1)): return n
    return 4
def ref_dec_tag(b):
    if not b: return None
    t=b[0]>>4; ctx=bool(b[0]&8); lvt=b[0]&7; i=1
    if t==15:
        if len(b)<2 or b[1]==255: return None
        t=b[1]; i=2
    if lvt in (6,7):
        if not ctx: return None
        return (i,t,ctx,'open' if lvt==6 else 'close',0)
    if lvt<5: return (i,t,ctx,None,lvt)
    if len(b)<i+1: return None
    e=b[i]; i+=1
    if e<254: return (i,t,ctx,None,e)
    n=2 if e==254 else 4
    if len(b)<i+n: return None
    return (i+n,t,ctx,None,int.from_bytes(b[i:i+n],'big'))
def fmt_tag(r):
    if r is None: return 'ERR'
    i,t,ctx,oc,l=r
    s=f"OK hl={i} tag={t} {'ctx' if ctx else 'app'}"
    return s+(' '+oc if oc else f' lvt={l}')
def pb(b): return '-' if not b else b.hex() if len(b)<=64 else None
def ref_dec_app(b):
    r=ref_dec_tag(b)
    if r is None: return 'ERR'
    h,t,ctx,oc,L=r
    if ctx: return 'ERR'
    raw=b[0]&7
    if t==0: return f'OK n={h} null' if raw==0 else 'ERR'
    if t==1: return f'OK n={h} bool {raw}' if raw<=1 else 'ERR'
    if t==5 or t>12: return 'ERR'
    if L>len(b)-h: return 'ERR'
    c=b[h:h+L]
    if t in (2,9,3):
        if not 1<=L<=4: return 'ERR'
        v=int.from_bytes(c,'big')
        if t==3:
            if c[0]&0x80: v-=1<<(8*L)
            return f'OK n={h+L} i {v}'
        return f'OK n={h+L} {"u" if t==2 else "enum"} {v}'
    if t==4: return f'OK n={h+L} r {c.hex()}' if L==4 else 'ERR'
    if t==6: return f'OK n={h+L} oct {pb(c)}'
    if t==7: return f'OK n={h+L} str cs={c[0]} {pb(c[1:])}' if L>=1 else 'ERR'
    if t==8:
        if L<1 or c[0]>7 or (L==1 and c[0]!=0): return 'ERR'
        nb=(L-1)*8-c[0]
        bits=''.join(str((c[1+i//8]>>(7-i%8))&1) for i in range(nb))
        return f'OK n={h+L} bits {bits or "-"}'
    if L!=4: return 'ERR'
    if t==10: return f'OK n={h+L} date {65535 if c[0]==255 else 1900+c[0]} {c[1]} {c[2]} {c[3]}'
    if t==11: return f'OK n={h+L} time {c[0]} {c[1]} {c[2]} {c[3]}'
    v=int.from_bytes(c,'big'); return f'OK n={h+L} oid {v>>22} {v&0x3fffff}'
cmds=[];exp=[]
def add(c,e): cmds.append(c); exp.append(e)
for _ in range(20000):
    cap=R.choice([0,1,2,3,4,5,6,7,8,1024,R.randint(0,300)])
    k=R.randrange(14)
    if k==0:
        v=R.choice([R.randint(0,2**32-1),R.randint(0,300),R.choice([0,255,256,65535,65536,2**24-1,2**24,2**32-1])])
        tg=R.choice([2,9]); c='enc_unsigned' if tg==2 else 'enc_enum'
        add(f'{c} {v} @{cap}',fit(hdr(tg,0,ulen(v))+v.to_bytes(ulen(v),'big'),cap))
    elif k==1:
        v=R.choice([R.randint(-2**31,2**31-1),R.randint(-300,300),R.choice([-128,-129,127,128,-32768,-32769,32767,32768,-2**23,-2**23-1,2**23-1,2**23,-2**31,2**31-1])])
        n=slen(v); add(f'enc_signed {v} @{cap}',fit(hdr(3,0,n)+(v&((1<<(8*n))-1)).to_bytes(n,'big'),cap))
    elif k==2:
        bits=R.choice([R.getrandbits(32),0x7fc00000|R.getrandbits(22),0x7f800000|R.randint(1,0x7fffff),0xff800000,0x7f800000,0x80000000,0xffffffff])
        nan=(bits&0x7f800000)==0x7f800000 and bits&0x7fffff
        add(f'enc_real {bits:08x} @{cap}','ERR' if nan else fit(b'\x44'+bits.to_bytes(4,'big'),cap))
    elif k==3:
        n=R.choice([0,1,4,5,252,253,254,255,65535,65536,R.randint(0,70000)]); cap=R.choice([cap,n+1,n+2,n+3,n+5,n+6,n+4,80000])
        d=bytes([0x41])*n
        out=hdr(6,0,n)+d
        e=fit(out,cap)
        if e!='ERR' and len(out)>64: e=None
        add(f'enc_octets {"rep:41:"+str(n) if n else "-"} @{cap}',e)
    elif k==4:
        n=R.choice([0,1,3,4,5,251,252,253,254,65534,65535,R.randint(0,300)]); cap=R.choice([cap,n+2,n+3,n+4,n+6,n+7,80000])
        out=hdr(7,0,n+1)+b'\x00'+b'A'*n
        e=fit(out,cap)
        if e!='ERR' and len(out)>64: e=None
        add(f'enc_str {"rep:41:"+str(n) if n else "-"} @{cap}',e)
    elif k==5:
        n=R.choice([0,1,7,8,9,16,R.randint(0,64),R.randint(0,3000)])
        bl=[R.randint(0,1) for _ in range(n)]
        bad=False
        if n and R.random()<0.2: bl[R.randrange(n)]=R.randint(2,9); bad=True
        nb=(n+7)//8; by=bytearray(nb)
        for i,x in enumerate(bl):
            if x: by[i//8]|=0x80>>(i%8)
        out=hdr(8,0,nb+1)+bytes([nb*8-n])+bytes(by)
        e='ERR' if bad else fit(out,cap)
        if e!='ERR' and len(out)>64: e=None
        add(f'enc_bits {"".join(map(str,bl)) or "-"} @{cap}',e)
    elif k==6:
        y=R.choice([1899,1900,2024,2154,2155,65535,R.randint(0,65535)]);m=R.choice([0,1,12,13,14,15,254,255,R.randint(0,255)]);d=R.choice([0,1,31,32,33,34,35,255,R.randint(0,255)]);w=R.choice([0,1,7,8,255,R.randint(0,255)])
        ok=(y==65535 or 1900<=y<=2154) and (1<=m<=14 or m==255) and (1<=d<=34 or d==255) and (1<=w<=7 or w==255)
        add(f'enc_date {y} {m} {d} {w} @{cap}',fit(bytes([0xa4,255 if y==65535 else (y-1900)&255,m,d,w]),cap) if ok else 'ERR')
    elif k==7:
        f=[R.choice([0,23,24,59,60,99,100,255,R.randint(0,255)]) for _ in range(4)]
        ok=all(x<=lim or x==255 for x,lim in zip(f,[23,59,59,99]))
        add(f'enc_time {f[0]} {f[1]} {f[2]} {f[3]} @{cap}',fit(bytes([0xb4]+f),cap) if ok else 'ERR')
    elif k==8:
        t=R.choice([0,1023,1024,R.randint(0,65535),R.randint(0,1023)]);i=R.choice([0,0x3fffff,0x400000,R.randint(0,2**32-1),R.randint(0,0x3fffff)])
        ok=t<=1023 and i<=0x3fffff
        add(f'enc_oid {t} {i} @{cap}',fit(b'\xc4'+((t<<22)|i).to_bytes(4,'big'),cap) if ok else 'ERR')
    elif k==9:
        t=R.choice([0,14,15,254,255,R.randint(0,255)]);v=R.choice([0,255,256,2**32-1,R.randint(0,2**32-1)])
        add(f'enc_ctx_unsigned {t} {v} @{cap}','ERR' if t==255 else fit(hdr(t,1,ulen(v))+v.to_bytes(ulen(v),'big'),cap))
    elif k==10:
        t=R.choice([0,14,15,254,255,R.randint(0,255)]);v=R.randint(0,1)
        add(f'enc_ctx_bool {t} {v} @{cap}','ERR' if t==255 else fit(hdr(t,1,1)+bytes([v]),cap))
    elif k==11:
        t=R.choice([0,14,15,254,255,R.randint(0,255)]);oc=R.choice(['open','close'])
        l=6 if oc=='open' else 7
        out=bytes([(t<<4)|8|l]) if t<15 else bytes([0xf8|l,t])
        add(f'enc_{oc} {t} @{cap}','ERR' if t==255 else fit(out,cap))
    elif k==12:
        n=R.randint(0,8); b=bytes(R.getrandbits(8) for _ in range(n))
        if R.random()<0.5 and n: b=bytes([R.choice([0xf0,0xf5,0xf8,0xfd,0xfe,0xff,0x05,0x0d,0x65])|R.choice([0,0])])+b[1:]
        add(f'dec_tag {b.hex() or "-"}',fmt_tag(ref_dec_tag(b)))
    else:
        t=R.randint(0,15); ln=R.choice([0,1,2,3,4,5,R.randint(0,20)])
        lvt=R.randint(0,7) if R.random()<0.3 else min(ln,5)
        first=(t<<4)|(8 if R.random()<0.1 else 0)|lvt
        b=bytearray([first])
        if t==15: b.append(R.choice([0,4,12,13,15,255]))
        if lvt==5:
            b.append(ln) if ln>=5 or R.random()<0.3 else b.extend([254,0,ln])
        body=bytes(R.getrandbits(8) for _ in range(ln))
        if t in (8,) and ln and R.random()<0.7: body=bytes([R.randint(0,8)])+body[1:]
        b+=body
        if R.random()<0.2: b=b[:R.randint(0,len(b))]
        if R.random()<0.2: b+=b'\x99\x98'
        b=bytes(b)
        e=ref_dec_app(b)
        if 'None' in e: e=None
        add(f'dec_app {b.hex() or "-"}',e)
out=subprocess.run(['./drv'],input='\n'.join(cmds)+'\n',capture_output=True,text=True).stdout.split('\n')
bad=0
for c,e,g in zip(cmds,exp,out):
    if e is None:
        if 'DIRTY' in g or 'OVERRUN' in g or 'NONDET' in g or 'BAD' in g: print('X',c[:100],g); bad+=1
        continue
    if e!=g:
        bad+=1
        if bad<20: print('MISMATCH',c[:120],'\n  exp',e[:100],'\n  got',g[:100])
print('checked',len(cmds),'bad',bad)
