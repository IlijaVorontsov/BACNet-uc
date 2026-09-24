import sys
def fnv(b):
    h=1469598103934665603
    for x in b: h^=x; h=(h*1099511628211)&0xFFFFFFFFFFFFFFFF
    return h
def fmt(b):
    if len(b)<=64: return b.hex()
    return b[:16].hex()+"..%016x/%d"%(fnv(b),len(b))
def hdr(tag,n,ctx=False):
    c=8 if ctx else 0
    out=bytearray()
    lv=n if n<=4 else 5
    if tag>=15: out+=bytes([0xF0|c|lv,tag])
    else: out.append(tag<<4|c|lv)
    if n>4:
        if n<=253: out.append(n)
        elif n<=65535: out+=bytes([254])+n.to_bytes(2,'big')
        else: out+=bytes([255])+n.to_bytes(4,'big')
    return bytes(out)
if __name__=="__main__":
  for n in (253,254,65535,65536):
      print("oct",n,"OK "+fmt(hdr(6,n)+b'\xab'*n))
  for n in (252,253,65534):
      print("str",n,"OK "+fmt(hdr(7,n+1)+b'\x00'+b'A'*n))
