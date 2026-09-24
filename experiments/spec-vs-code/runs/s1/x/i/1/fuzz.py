import random, subprocess, sys
random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
vals = ["nan", "inf", "-inf", "5", "9.99", "10", "12", "12.01", "20", "27.999", "28", "30", "30.001", "35", "-1e30", "1e30"]
lines = []
for blk in range(3000):
    lines.append(f"cfg high={random.choice([30,30,30,10,'nan'])} low={random.choice([10,10,10,30])} deadband={random.choice([0,2,2,15,-1])} delay={random.choice([0,0,10,30,4294967295])} escalate={random.choice([0,50,300,1])}")
    t = random.choice([0, 4294967000])
    lines.append(f"init {t}")
    for _ in range(random.randint(1, 60)):
        t = (t + random.choice([0, 0, 1, 5, 10, 30, 100, 1000, 4294967200])) % 2**32
        c = random.choice(["s", "s", "s", "sf", "tick", "ack", "maint"])
        if c in ("s", "sf"): lines.append(f"{c} {t} {random.choice(vals)}")
        elif c == "maint": lines.append(f"maint {t} {random.choice(['on','off'])}")
        else: lines.append(f"{c} {t}")
p = subprocess.run(["./drv_san"], input="\n".join(lines).encode() + b"\n", capture_output=True)
out = p.stdout.decode().splitlines()
bad = [l for l in out if "OVERRUN" in l or "BADCOUNT" in l or "BADCMD" in l or "STATE?" in l]
print("rc", p.returncode, "lines", len(out), "/", len(lines), "bad", len(bad), bad[:3], p.stderr.decode()[:500])
