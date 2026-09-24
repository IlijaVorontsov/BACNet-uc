#!/usr/bin/env python3
"""Merge REPORT_DRAFT.md + results/tables.md + METHOD.md into the final README.md.

usage: assemble_report.py LAB OUT.md TESTS_NOTE.md COST_NOTE.md
"""
import re
import sys

LAB, OUT, TESTS_NOTE, COST_NOTE = sys.argv[1:5]
draft = open(f"{LAB}/REPORT_DRAFT.md").read()
tables = open(f"{LAB}/results/tables.md").read()
method = open(f"{LAB}/METHOD.md").read()


def section(title_prefix):
    parts = re.split(r"(?m)^## ", tables)
    for p in parts:
        if p.startswith(title_prefix):
            body = p.split("\n", 1)[1]
            return body.strip() + "\n"
    raise KeyError(title_prefix)


s3 = section("Stage 3").replace("### P · BACnet codec", "**P · BACnet codec**").replace("### X · alarm policy", "**X · alarm policy**")
s4 = section("Stage 4")
cost = section("Cost per agent run")
s1 = section("Stage 1")
s2 = section("Stage 2")

out = draft
out = out.replace("__S3_TABLE__", s3)
out = out.replace("__S4_TABLE__", s4)
out = out.replace("__S3_TESTS_NOTE__", open(TESTS_NOTE).read().strip())
out = out.replace("__COST__", open(COST_NOTE).read().strip())
out = out.replace("__METHOD__", method.strip() + "\n\n## Appendix — full result tables\n\n### Stage 1\n\n" + s1 +
                  "\n### Stage 2\n\n" + s2 + "\n### Time per agent run\n\n" + cost)
assert "__" not in re.sub(r"`[^`]*`", "", out).replace("__pycache__", ""), "unfilled placeholder"
open(OUT, "w").write(out)
print(f"wrote {OUT}: {len(out.splitlines())} lines")
