#!/usr/bin/env python3
"""Generate oracle scenario blocks for domain X (alarm).

usage: gen_x.py VERSION(10|11|13) OUT.inp
"""
import random
import sys

RULES = {
    "A1": "STD", "A2": "STD", "A3": "STD", "A4": "STD", "A5": "STD",
    "A5a": "POL", "A6": "POL", "A6a": "POL", "A7": "POL", "A8": "POL", "A9": "POL", "A10": "POL",
    "A11": "POL", "A12": "POL", "A13": "POL",
    "RAND": "MIX",
    "CR1": "NEW", "CR3acked": "NEW",
    "CR3clear": "CONFLICT", "CR3incl": "CONFLICT",
}

C = "cfg high=30 low=10 deadband=2 delay=30 escalate=600"
C0 = "cfg high=30 low=10 deadband=2 delay=0 escalate=600"


def targeted(ver):
    S = []

    def sc(tags, *lines):
        S.append((tags, list(lines)))

    # A1/A2 strict limits
    sc(["A2", "CR3incl"], C, "init 0", "s 0 30", "s 10 30", "s 40 30", "s 100 30")
    sc(["A2", "CR3incl"], C, "init 0", "s 0 10", "s 40 10", "s 100 10")
    sc(["A2", "CR3incl"], C0, "init 0", "s 5 30", "s 6 10", "s 7 30.25")
    sc(["A2"], C, "init 0", "s 0 30.25", "s 30 30.25", "s 40 29.75")
    sc(["A2"], C, "init 0", "s 0 9.75", "s 29 9.75", "s 30 9.75")
    # A3 deadband
    sc(["A3"], C0, "init 0", "s 0 35", "s 10 28", "s 20 28.25", "s 30 27.75", "s 40 35")
    sc(["A3"], C0, "init 0", "s 0 5", "s 10 12", "s 20 11.75", "s 30 12.25")
    sc(["A3"], "cfg high=30 low=10 deadband=0 delay=0 escalate=0", "init 0", "s 0 31", "s 1 30", "s 2 29.75", "s 3 10", "s 4 9.75", "s 5 10", "s 6 10.25")
    sc(["A3", "A5"], C, "init 0", "s 0 35", "s 30 35", "s 40 27", "s 60 27", "s 70 27", "s 80 29", "s 90 27", "s 120 27")
    sc(["A3"], "cfg high=30 low=10 deadband=5 delay=0 escalate=0", "init 0", "s 0 31", "s 1 26", "s 2 25", "s 3 24.75")
    # A4 direct transitions and priority
    sc(["A4"], C0, "init 0", "s 0 35", "s 10 5", "s 20 35", "s 30 20")
    sc(["A4", "A5"], C, "init 0", "s 0 35", "s 30 35", "s 40 5", "s 50 5", "s 70 5")
    sc(["A4", "A5a"], C, "init 0", "s 0 35", "s 30 35", "s 40 25", "s 50 5", "s 70 5", "s 80 5")
    sc(["A4"], C, "init 0", "s 0 5", "s 30 5", "s 40 35", "s 60 35", "s 70 35")
    # A5 continuous delay
    sc(["A5"], C, "init 0", "s 10 35", "s 20 35", "s 30 25", "s 40 35", "s 50 35", "s 60 35", "s 70 35")
    sc(["A5", "A5a"], C, "init 0", "s 10 35", "s 20 40", "s 30 31", "s 40 36")
    sc(["A5"], "cfg high=30 low=10 deadband=2 delay=1 escalate=0", "init 0", "s 5 35", "s 5 35", "s 6 35")
    # A5a ticks / cancellation / evaluation order
    sc(["A5a"], C, "init 0", "s 10 35", "tick 39", "tick 40", "tick 100")
    sc(["A5a"], C, "init 0", "s 10 35", "s 50 20", "tick 100")
    sc(["A5a"], C, "init 0", "s 10 35", "tick 30", "s 45 20", "tick 100")
    sc(["A5a", "A8"], "cfg high=30 low=10 deadband=2 delay=30 escalate=50", "init 0", "s 10 35", "ack 40", "tick 100", "tick 200")
    sc(["A5a", "A10"], C, "init 0", "s 10 35", "maint 40 on", "maint 50 off")
    sc(["A5a", "A10"], C, "init 0", "s 10 35", "maint 20 on", "tick 40", "maint 50 off")
    sc(["A5a", "A5"], C0, "init 0", "s 10 35", "s 20 35", "s 30 5", "s 40 20")
    # A6 faults
    sc(["A6"], C, "init 0", "sf 20 25", "s 30 nan", "sf 40 50", "tick 100")
    sc(["A6"], C, "init 0", "s 0 nan", "s 10 25")
    sc(["A6"], C0, "init 0", "s 0 35", "sf 10 35", "s 20 35")
    sc(["A6"], C0, "init 0", "s 0 inf", "s 10 -inf", "s 20 20")
    sc(["A6"], C, "init 0", "s 0 35", "s 20 nan", "s 40 35", "s 60 35", "s 70 35")
    sc(["A6", "A6a"], C0, "init 0", "sf 5 0", "s 10 35", "s 20 20")
    sc(["A6", "A6a"], C, "init 0", "sf 5 0", "s 10 35", "s 30 35", "s 40 35")
    sc(["A6", "A6a", "A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "sf 50 35", "tick 99", "tick 100", "s 120 20")
    sc(["A6", "A6a", "A8"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "sf 10 0", "ack 20", "s 30 35", "tick 200")
    # A7 notification values
    sc(["A7"], C, "init 0", "s 10 35", "s 20 36", "tick 40", "s 50 37")
    sc(["A7"], "cfg high=30 low=10 deadband=2 delay=0 escalate=50", "init 0", "s 0 35", "s 20 29", "s 30 28.5", "tick 60")
    # A8 acknowledgement
    sc(["A8"], C0, "init 0", "ack 5", "s 10 35", "ack 20", "ack 30", "s 40 20", "ack 50")
    sc(["A8", "A9", "CR3clear"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 10 20", "tick 99", "tick 100", "tick 300")
    sc(["A8", "A9", "CR3clear"], "cfg high=30 low=10 deadband=2 delay=10 escalate=100", "init 0", "s 0 5", "s 10 5", "s 20 20", "s 30 20", "s 110 20", "s 120 20")
    sc(["A8", "A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "ack 10", "s 20 20", "s 30 35", "tick 129", "tick 130")
    sc(["A8", "A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 50 5", "tick 100", "tick 149", "tick 150")
    sc(["A8", "A9", "CR3clear"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 10 20", "ack 50", "tick 200")
    # A9 escalation
    sc(["A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=0", "init 0", "s 0 35", "tick 1000", "tick 100000")
    sc(["A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "ack 500", "tick 600")
    sc(["A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 100 36", "s 200 37", "tick 300")
    sc(["A9", "A11"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 150 20")
    sc(["A9", "A11"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 10 20", "s 150 35")
    sc(["A9", "A11"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "s 5 5", "tick 104", "tick 105")
    # A10 maintenance
    sc(["A10"], C0, "init 0", "maint 0 on", "s 10 35", "s 20 20", "maint 30 off", "s 40 35")
    sc(["A10"], C0, "init 0", "maint 0 on", "s 10 35", "maint 30 off", "tick 40")
    sc(["A10", "A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "maint 0 on", "s 10 35", "maint 30 off", "tick 129", "tick 130")
    sc(["A10"], C0, "init 0", "s 0 35", "maint 10 on", "s 20 20", "maint 30 off", "s 40 35")
    sc(["A10", "A9"], "cfg high=30 low=10 deadband=2 delay=0 escalate=100", "init 0", "s 0 35", "maint 10 on", "tick 150", "maint 160 off", "tick 170")
    sc(["A10"], C0, "init 0", "maint 0 on", "maint 5 on", "s 10 35", "maint 20 off", "maint 30 off", "s 40 20")
    sc(["A10"], C0, "init 0", "maint 0 off", "s 10 35", "maint 20 off")
    sc(["A10", "A6"], C0, "init 0", "maint 0 on", "sf 10 1", "maint 20 off", "s 30 20")
    sc(["A10", "A8"], C0, "init 0", "s 0 35", "maint 10 on", "ack 20", "maint 30 off", "tick 700")
    sc(["A10", "A5a"], C, "init 0", "maint 0 on", "s 10 35", "maint 30 off", "tick 40", "tick 50")
    sc(["A10"], C0, "init 0", "s 0 35", "maint 10 on", "s 20 5", "maint 30 off")
    # A12 time monotonicity
    sc(["A12"], C0, "init 100", "s 50 35", "s 100 20", "s 120 35", "s 110 20", "tick 110", "s 120 20")
    sc(["A12"], C, "init 0", "s 10 35", "s 20 35", "s 15 35", "s 40 35", "ack 30", "ack 40")
    sc(["A12"], C0, "init 0", "s 10 35", "s 10 20", "s 10 35")
    # A13 before first sample
    sc(["A13"], C0, "init 0", "tick 10", "ack 20", "maint 30 on", "maint 40 off", "tick 50")
    # combined long scenario
    sc(["A5a", "A8", "A9", "A10"], "cfg high=25 low=5 deadband=1 delay=20 escalate=120", "init 0",
       "s 0 20", "s 10 26", "s 20 26", "s 30 26", "tick 40", "ack 60", "s 70 23.5", "s 80 23.5", "s 100 23.5",
       "s 110 3", "tick 130", "maint 140 on", "s 150 20", "tick 200", "maint 210 off", "tick 400")

    if ver >= 11:
        D = "cfg high=30 low=10 deadband=2 delay=30 escalate=600 delay_normal=60"
        sc(["CR1"], D, "init 0", "s 0 35", "s 30 35", "s 40 20", "s 70 20", "s 99 20", "s 100 20")
        sc(["CR1"], D, "init 0", "s 0 5", "s 30 5", "s 40 20", "tick 99", "tick 100")
        sc(["CR1"], D, "init 0", "s 0 35", "s 30 35", "s 40 5", "s 70 5")
        sc(["CR1"], "cfg high=30 low=10 deadband=2 delay=30 escalate=600 delay_normal=0", "init 0", "s 0 35", "s 30 35", "s 40 20")
        sc(["CR1"], "cfg high=30 low=10 deadband=2 delay=30 escalate=600 delay_normal=4294967295", "init 0", "s 0 35", "s 30 35", "s 40 20", "s 69 20", "s 70 20")
        sc(["CR1"], "cfg high=30 low=10 deadband=2 delay=0 escalate=600 delay_normal=50", "init 0", "s 0 35", "s 10 20", "s 30 5", "s 40 20", "s 89 20", "s 90 20")
        sc(["CR1"], "cfg high=30 low=10 deadband=2 delay=100 escalate=600 delay_normal=10", "init 0", "s 0 35", "s 100 35", "s 110 20", "s 120 20", "s 130 35")
        sc(["CR1", "A6a"], "cfg high=30 low=10 deadband=2 delay=10 escalate=600 delay_normal=100", "init 0", "s 0 35", "s 10 35", "sf 20 0", "s 30 20")
        sc(["CR1", "A10"], "cfg high=30 low=10 deadband=2 delay=0 escalate=600 delay_normal=40", "init 0", "s 0 35", "maint 5 on", "s 10 20", "tick 49", "tick 50", "maint 60 off")
    if ver >= 13:
        E = "cfg high=30 low=10 deadband=2 delay=0 escalate=100"
        sc(["CR3acked"], E, "init 0", "s 0 35", "ack 10", "ack 20", "s 30 20", "ack 40")
        sc(["CR3acked", "A10"], E, "init 0", "s 0 35", "maint 10 on", "ack 20", "maint 30 off", "tick 200")
        sc(["CR3acked", "A5a"], "cfg high=30 low=10 deadband=2 delay=30 escalate=100", "init 0", "s 10 35", "ack 40", "ack 50")
        sc(["CR3acked", "A9"], E, "init 0", "s 0 5", "tick 100", "ack 150", "tick 300")
        sc(["CR3acked"], E, "init 0", "sf 0 1", "ack 10", "s 20 35", "s 30 5", "ack 40")
    return S


VALS = ["5", "9.75", "10", "10.25", "11", "12", "12.25", "20", "27.5", "27.75", "28", "28.25", "29.75",
        "30", "30.25", "31", "35", "inf", "-inf", "nan"]


def random_scenario(rnd, ver):
    db = rnd.choice(["0", "1", "2", "5"])
    delay = rnd.choice([0, 0, 10, 30])
    esc = rnd.choice([0, 50, 200])
    cfg = f"cfg high=30 low=10 deadband={db} delay={delay} escalate={esc}"
    if ver >= 11 and rnd.random() < 0.5:
        cfg += f" delay_normal={rnd.choice([0, 10, 60, 4294967295])}"
    lines = [cfg]
    t = rnd.choice([0, 0, 100])
    lines.append(f"init {t}")
    cur = rnd.choice(VALS[:17])
    for _ in range(rnd.randrange(20, 60)):
        dt = rnd.choice([0, 1, 5, 10, 10, 20, 30, 60])
        if rnd.random() < 0.03:
            dt = -rnd.choice([1, 5, 20])
        t2 = max(0, t + dt)
        r = rnd.random()
        if r < 0.55:
            if rnd.random() < 0.6:
                cur = rnd.choice(VALS[:17])
            else:
                cur = rnd.choice(VALS)
            lines.append(f"s {t2} {cur}")
        elif r < 0.60:
            lines.append(f"sf {t2} {rnd.choice(VALS[:17])}")
        elif r < 0.78:
            lines.append(f"tick {t2}")
        elif r < 0.90:
            lines.append(f"ack {t2}")
        else:
            lines.append(f"maint {t2} {rnd.choice(['on', 'off'])}")
        if dt >= 0:
            t = t2
    return lines


def blocks(ver, nrand=300):
    out = []
    for i, (tags, lines) in enumerate(targeted(ver)):
        out.append((f"x{i+1:03d}", tags, lines))
    # ids of targeted scenarios for v1.1/v1.3 are appended after the v1.0 ones, so ids stay stable
    rnd = random.Random(4321)
    for i in range(nrand):
        out.append((f"r{i+1:03d}", ["RAND"], random_scenario(rnd, 10)))
    if ver >= 11:
        rnd = random.Random(8765)
        for i in range(100):
            out.append((f"q{i+1:03d}", ["RAND", "CR1"], random_scenario(rnd, 11)))
    return out


def main():
    ver = int(sys.argv[1])
    out = sys.argv[2]
    with open(out, "w") as f:
        for bid, tags, lines in blocks(ver):
            f.write(f"### {bid} {' '.join(tags)}\n")
            for ln in lines:
                f.write(ln + "\n")
            f.write("---\n")


if __name__ == "__main__":
    main()
