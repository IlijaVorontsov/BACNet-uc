# Differential fuzzer: independent Python model of SPEC.md vs. the driver.
# Usage: python3 fuzz_model.py [seed] [nblocks] [./drv]
import random, subprocess, struct, math, sys
def f32(x): return struct.unpack('f', struct.pack('f', x))[0]
N,H,L,F=0,1,2,3
SN=['NORMAL','HIGH','LOW','FAULT']; EN=['TO_NORMAL','TO_HIGH','TO_LOW','TO_FAULT','ESCALATE']
def fmt(v):
    if math.isnan(v): return 'nan'
    if math.isinf(v): return 'inf' if v>0 else '-inf'
    s='%.3f'%v
    return '0.000' if s=='-0.000' else s
class M:
    def __init__(s,cfg,now):
        s.c=cfg; s.st=N; s.pend=None; s.ps=0; s.un=False; s.esc=False; s.maint=False
        s.at=0; s.last=now; s.lv=0.0; s.ln=N
    def tgt(s,v):
        hi,lo,db=s.c['high'],s.c['low'],s.c['db']
        hc=v>hi; lc=v<lo
        if s.st==N: return H if hc else (L if lc else None)
        if s.st==H: return L if lc else (N if v<f32(hi-db) else None)
        if s.st==L: return H if hc else (N if v>f32(lo+db) else None)
        return None
    def note(s,out,now,ev): out.append((EN[ev],now,s.lv))
    def notif(s,now,out):
        to=s.st; s.note(out,now,to); s.ln=to
        if to in (H,L): s.un=True; s.at=now; s.esc=False
    def trans(s,to,now,out):
        s.st=to; s.pend=None
        if not s.maint: s.notif(now,out)
    def timer(s,now,out):
        if s.st==F or s.pend is None: return
        if now-s.ps>=s.c['delay']: s.trans(s.pend,now,out)
    def escal(s,now,out):
        e=s.c['esc']
        if s.un and not s.esc and not s.maint and e>0 and now-s.at>=e:
            s.note(out,now,4); s.esc=True
    def ok(s,now):
        if now<s.last: return False
        s.last=now; return True
    def sample(s,now,v,fault):
        out=[]
        if not s.ok(now): return out
        s.lv=v
        if fault or math.isnan(v):
            if s.st!=F: s.trans(F,now,out)
            s.pend=None
        else:
            if s.st==F: s.trans(N,now,out)
            t=s.tgt(v)
            if t is None: s.pend=None
            elif t!=s.pend: s.pend=t; s.ps=now
            s.timer(now,out)
        s.escal(now,out); return out
    def tick(s,now):
        out=[]
        if not s.ok(now): return out
        s.timer(now,out); s.escal(now,out); return out
    def ack(s,now):
        out=[]
        if not s.ok(now): return out
        s.timer(now,out); s.un=False; s.escal(now,out); return out
    def mnt(s,now,on):
        out=[]
        if not s.ok(now): return out
        s.timer(now,out)
        if on: s.maint=True
        elif s.maint:
            s.maint=False
            if s.st!=s.ln: s.notif(now,out)
        s.escal(now,out); return out
def line(m,out):
    return ' '.join([SN[m.st]]+['%s@%d:%s'%(e,t,fmt(v)) for e,t,v in out])
def gen(r):
    hi=r.choice([30,20,5,0.5]); lo=r.choice([10,-5,0,0.25]); db=r.choice([0,2,1.5,0.1])
    delay=r.choice([0,0,5,10,30]); esc=r.choice([0,0,20,50,100])
    cfg={'high':f32(hi),'low':f32(lo),'db':f32(db),'delay':delay,'esc':esc}
    t0=r.randint(0,50); inp=['cfg high=%s low=%s deadband=%s delay=%d escalate=%d'%(hi,lo,db,delay,esc),'init %d'%t0]
    exp=['cfg']; m=M(cfg,t0); exp.append(SN[m.st]); t=t0
    vals=[hi,lo,hi+1,lo-1,hi-db,lo+db,hi-db-0.01,lo+db+0.01,(hi+lo)/2,float('inf'),float('-inf'),float('nan'),hi+0.001,lo-0.001]
    for _ in range(r.randint(1,40)):
        dt=r.choice([0,0,1,2,3,5,10,20,40,-1,-5])
        tt=max(0,t+dt)
        if dt>=0: t=tt
        k=r.random()
        if k<0.55:
            v=f32(r.choice(vals)); fault=r.random()<0.08
            vs='nan' if math.isnan(v) else ('inf' if v==float('inf') else ('-inf' if v==float('-inf') else repr(v)))
            inp.append('%s %d %s'%('sf' if fault else 's',tt,vs)); out=m.sample(tt,v,fault)
        elif k<0.75: inp.append('tick %d'%tt); out=m.tick(tt)
        elif k<0.87: inp.append('ack %d'%tt); out=m.ack(tt)
        else:
            on=r.random()<0.5; inp.append('maint %d %s'%(tt,'on' if on else 'off')); out=m.mnt(tt,on)
        exp.append(line(m,out))
    return inp,exp
r=random.Random(int(sys.argv[1]) if len(sys.argv)>1 else 1)
blocks=[gen(r) for _ in range(int(sys.argv[2]) if len(sys.argv)>2 else 3000)]
data=''.join('@block %d\n'%i+'\n'.join(b[0])+'\n' for i,b in enumerate(blocks))
o=subprocess.run([sys.argv[3] if len(sys.argv)>3 else "./drv"],input=data.encode(),stdout=subprocess.PIPE).stdout.decode().split('\n')
got={};cur=None
for ln in o:
    if ln.startswith('@block '): cur=int(ln.split()[1]); got[cur]=[]
    elif cur is not None and ln: got[cur].append(ln.rstrip())
bad=0
for i,(inp,exp) in enumerate(blocks):
    if got.get(i)!=exp:
        bad+=1
        if bad<=3:
            print('MISMATCH block',i)
            for a,e,g in zip(inp,exp,got.get(i,[])+['']*99):
                print(('   ' if e==g else '!! ')+a.ljust(34),'exp:',e.ljust(40),'got:',g)
print('%d/%d match'%(len(blocks)-bad,len(blocks)))
