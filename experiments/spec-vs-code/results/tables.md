## Stage 1 — one-shot creation

Oracle = hidden intent (all spec rules). STD = rules fixed by the public BACnet standard; POL = private product decisions; MIX = random/fuzz vectors that exercise many rules at once. Values: mean (min–max) over 4 runs.

| Domain | Artifacts given to the agent | Acceptance test passed | Oracle match % | STD % | POL % | MIX % |
|---|---|---|---|---|---|---|
| P · BACnet codec | prompt + acceptance test | 4/4 | 96.7 (94.3–98.3) | 99.6 (99.1–100.0) | 89.1 (81.6–94.2) | 99.6 (99.0–100.0) |
| P · BACnet codec | prompt + acceptance test, asked to write unit tests | 4/4 | 96.5 (94.8–98.0) | 99.6 (99.1–100.0) | 88.5 (83.6–92.8) | 99.4 (99.0–100.0) |
| P · BACnet codec | prompt + acceptance test + SPEC.md | 4/4 | 100.0 (100.0–100.0) | 100.0 (100.0–100.0) | 100.0 (100.0–100.0) | 100.0 (100.0–100.0) |
| X · alarm policy | prompt + acceptance test | 4/4 | 35.0 (29.5–46.0) | 95.6 (94.1–100.0) | 75.5 (73.9–76.1) | 25.3 (18.3–38.7) |
| X · alarm policy | prompt + acceptance test, asked to write unit tests | 4/4 | 25.3 (18.5–32.5) | 88.2 (76.5–100.0) | 71.7 (71.7–71.7) | 14.6 (7.0–23.0) |
| X · alarm policy | prompt + acceptance test + SPEC.md | 4/4 | 100.0 (100.0–100.0) | 100.0 (100.0–100.0) | 100.0 (100.0–100.0) | 100.0 (100.0–100.0) |

**Divergence between independent one-shot implementations** (share of oracle inputs on which two implementations produce different output):

| Domain | Pairs within prompt-only arms (i+ii) | Pairs within spec arm (iii) |
|---|---|---|
| P · BACnet codec | 2.7 (0.0–5.6) over 28 pairs | 0.0 (0.0–0.0) over 6 pairs |
| X · alarm policy | 50.3 (7.7–77.4) over 28 pairs | 0.0 (0.0–0.0) over 6 pairs |

**Unit tests the agent wrote for its own code (arm ii)** — checked against the hidden intent:

| Domain | Run | Test blocks | Pass on own code | Agree with the intended behaviour |
|---|---|---|---|---|
| P · BACnet codec | ii-1 | 76 | 76 | 65 (86 %) |
| P · BACnet codec | ii-2 | 31 | 31 | 27 (87 %) |
| P · BACnet codec | ii-3 | 68 | 68 | 64 (94 %) |
| P · BACnet codec | ii-4 | 67 | 67 | 59 (88 %) |
| X · alarm policy | ii-1 | 41 | 41 | 33 (80 %) |
| X · alarm policy | ii-2 | 52 | 52 | 39 (75 %) |
| X · alarm policy | ii-3 | 59 | 59 | 43 (73 %) |
| X · alarm policy | ii-4 | 41 | 41 | 26 (63 %) |
| X · alarm policy | iii-4 (spec arm, wrote tests unprompted) | 29 | 29 | 29 (100 %) |

## Stage 2 — how many injected faults does each kind of test catch? (mutation analysis)

Mutants: single-point faults injected into the correct reference implementation. Denominator = mutants the full oracle detects (the rest are behaviour-preserving). Only test blocks that agree with the intended behaviour are used (wrong expectations would 'detect' the correct code).

| Domain | Mutants detectable | Acceptance test | Visible unit tests (spec-derived, 50 %) | Agent self-written unit tests (4 suites) | Black-box tests written from SPEC.md (2 suites) |
|---|---|---|---|---|---|
| P · BACnet codec | 858 of 976 | 34.0 % | 88.7 % | 93.8 (85.3–97.8) % | 97.0 (96.9–97.1) % |
| X · alarm policy | 98 of 110 | 48.0 % | 91.8 % | 85.5 (81.6–88.8) % | 96.4 (95.9–96.9) % |

## Stage 3 — evolution: three change requests applied by fresh agents (no memory)

Base = the correct reference code. Each chain: CR1 feature → CR2 refactor → CR3 field requests (one legitimate item + items that conflict with earlier decisions). 3 chains per arm. 'Kept' = previously intended behaviour still intact (hidden oracle). Values are means over 3 chains.

### P · BACnet codec

| Arm | Old behaviour kept after CR1 / CR2 / CR3 (%) | Held-out old behaviour after CR3 (%) | New features correct CR1 / CR3 (%) | NaN refusal kept (private decision) | Unsigned 0 = 21 00 kept (standard) | NaN policy carried to new Double (CR1) | CR3 item 1 (UTF-8 check; conflicts with POL rule E7a) |
|---|---|---|---|---|---|---|---|
| code + acceptance test | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 0/3 | 3/3 | 3/3 | 3/3 implemented |
| code with spec as comments + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 2/3 implemented |
| code + SPEC.md + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 0/3 implemented |
| code + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 3/3 implemented |
| code + SPEC.md + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 1/3 implemented |
| (C with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 1/3 | 3/3 | 3/3 | 3/3 implemented |
| (D with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 0/3 implemented |

### X · alarm policy

| Arm | Old behaviour kept after CR1 / CR2 / CR3 (%) | Held-out old behaviour after CR3 (%) | New features correct CR1 / CR3 (%) | ack survives return-to-normal kept | strict limits kept (standard) |
|---|---|---|---|---|---|
| code + acceptance test | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code with spec as comments + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code + SPEC.md + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code + SPEC.md + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| (C with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| (D with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |

## Stage 4 — review: 'the module has some bugs, fix them' (5 injected bugs, deliberate quirks present)

| Domain | Artifacts | Bugs that violate the standard, fixed | Bugs that violate only private policy, fixed | Intended behaviour broken (oracle vectors per run) |
|---|---|---|---|---|
| P · BACnet codec | code + acceptance | 9/9 | 3/6 | 0.0 (0.0–0.0) |
| P · BACnet codec | code with spec as comments | 9/9 | 6/6 | 0.0 (0.0–0.0) |
| P · BACnet codec | code + SPEC.md | 9/9 | 6/6 | 0.0 (0.0–0.0) |
| P · BACnet codec | code + unit tests | 9/9 | 4/6 | 0.0 (0.0–0.0) |
| P · BACnet codec | (code + unit tests with leaked labels — not used) | 9/9 | 6/6 | 0.0 (0.0–0.0) |
| X · alarm policy | code + acceptance | 5/6 | 5/9 | 0.7 (0.0–2.0) |
| X · alarm policy | code with spec as comments | 6/6 | 9/9 | 0.0 (0.0–0.0) |
| X · alarm policy | code + SPEC.md | 6/6 | 9/9 | 0.0 (0.0–0.0) |
| X · alarm policy | code + unit tests | 4/6 | 4/9 | 22.3 (0.0–67.0) |
| X · alarm policy | (code + unit tests with leaked labels — not used) | 0/6 | 5/9 | 4.0 (4.0–4.0) |

## Cost per agent run (from transcripts)

| Stage / arm | Runs | Wall time per run, min (mean) | Tool calls per run (mean) |
|---|---|---|---|
| s0 p bb | 2 | 9.3 | 7.5 |
| s0 p comment | 1 | 3.8 | 8.0 |
| s0 p impl | 2 | 8.5 | 18.0 |
| s0 x bb | 2 | 9.2 | 6.0 |
| s0 x comment | 1 | 1.9 | 7.0 |
| s0 x impl | 2 | 4.5 | 9.0 |
| s1 p arm i | 4 | 8.1 | 15.2 |
| s1 p arm ii | 4 | 9.3 | 15.0 |
| s1 p arm iii | 4 | 4.6 | 10.2 |
| s1 x arm i | 4 | 9.0 | 7.2 |
| s1 x arm ii | 4 | 9.8 | 8.0 |
| s1 x arm iii | 4 | 2.7 | 5.5 |
| s3 p arm A | 9 | 4.2 | 14.6 |
| s3 p arm Ac | 9 | 5.3 | 21.7 |
| s3 p arm B | 9 | 3.6 | 13.1 |
| s3 p arm C | 9 | 5.7 | 21.4 |
| s3 p arm C (clean tests) | 9 | 6.5 | 20.2 |
| s3 p arm D | 9 | 4.0 | 15.1 |
| s3 p arm D (clean tests) | 9 | 5.6 | 18.1 |
| s3 x arm A | 9 | 2.2 | 10.2 |
| s3 x arm Ac | 9 | 2.3 | 12.3 |
| s3 x arm B | 9 | 2.4 | 11.2 |
| s3 x arm C | 9 | 2.6 | 11.2 |
| s3 x arm C (clean tests) | 9 | 2.4 | 10.7 |
| s3 x arm D | 9 | 2.6 | 12.3 |
| s3 x arm D (clean tests) | 9 | 2.4 | 12.3 |
| s4 p arm A | 3 | 3.1 | 11.0 |
| s4 p arm Ac | 3 | 2.4 | 11.3 |
| s4 p arm B | 3 | 3.0 | 12.3 |
| s4 p arm C | 3 | 5.6 | 12.0 |
| s4 p arm C (clean tests) | 3 | 6.6 | 13.7 |
| s4 x arm A | 3 | 5.8 | 12.7 |
| s4 x arm Ac | 3 | 2.5 | 12.3 |
| s4 x arm B | 3 | 3.3 | 13.7 |
| s4 x arm C | 3 | 6.9 | 10.0 |
| s4 x arm C (clean tests) | 3 | 6.6 | 10.0 |

Blinding audit: 0 of 190 agent transcripts contain a tool call touching the hidden lab directory.

