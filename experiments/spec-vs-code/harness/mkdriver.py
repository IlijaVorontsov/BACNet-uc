#!/usr/bin/env python3
"""mkdriver.py TEMPLATE VERSION OUT : keep '/*V1N*/' lines only when VERSION >= 1N."""
import re, sys
tpl, ver, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
res = []
for line in open(tpl):
    m = re.match(r'^/\*V(\d+)\*/(.*)$', line, re.S)
    if m:
        if ver >= int(m.group(1)):
            res.append(m.group(2))
    else:
        res.append(line)
open(out, "w").write("".join(res))
