# If the AI one-shots the code, do we still need unit tests, specs and docs?

*A controlled experiment on BACnet firmware modules, run with the same model that writes your code.
190 agent runs, every outcome scored against a hidden, independently verified oracle.*

---

## The short answer

**Code plus a passing acceptance test is not "everything in one", but the answer is not "keep
doing what we did before" either.** The experiment separates three jobs that tests and docs used to
do together, and the AI changes the value of each very differently:

| Job | Does the code + acceptance test do it? | Evidence | Verdict for the AI era |
|---|---|---|---|
| **Catch faults** (regression net) | Poorly: the acceptance test caught **34 % / 48 %** of injected faults | Stage 2 | Still essential, but now **cheap**: tests the AI wrote itself caught **82–98 %** |
| **Record intent** (which behaviour is *wanted*) | **No.** Code cannot say which of its behaviours are deliberate | Stages 1, 3, 4 | **The part that becomes more important.** This is the spec's real job |
| **Explain what the code does** | Yes: the model reads code fine | Stage 3 (zero regressions in every arm) | Documentation that only restates the code is losing its value |

The things that still have to be written down, by you, are **your decisions and the reasons for
them**. That is especially true of anything that is *not* dictated by a public standard. Where
they live (a SPEC.md, structured comments in the code, or test vectors with requirement IDs)
matters much less than that they exist and were written independently of the code.

---

## What the experiment showed

### 1 · "Code that does what I intend" is one random sample. You only intended what you checked.

I gave the one-shot prompt to fresh agents: 4 times prompt + acceptance test, 4 times the same but
also asked to write unit tests, and 4 times the same plus the written spec. **All 24 runs passed the
acceptance test.**

| Module | Prompt + acceptance test | … + agent writes its own unit tests | … + SPEC.md |
|---|---|---|---|
| **P · BACnet codec** (mostly a public standard) | 96.7 % of hidden intent (94.3–98.3) | 96.5 % (94.8–98.0) | **100 %** (4/4 runs) |
| **X · alarm policy** (mostly private product decisions) | 35.0 % (29.5–46.0) | 25.3 % (18.5–32.5) | **100 %** (4/4 runs) |

Two independent prompt-only runs of the *same prompt* disagreed, on average, on **50 %** of the X
oracle scenarios (range 8–77 % over 28 pairs; P: 2.7 %, range 0–5.6 %). With the spec, every pair
agreed on 100 %. Each prompt-only run is a valid-looking answer to your prompt, but they are
different programs.

Where they diverge is predictable. Probing the 8 prompt-only implementations on specific questions:

| Decision | Source | Prompt-only runs that matched your intent | With SPEC.md |
|---|---|---|---|
| Value exactly at the limit is **not** an alarm | BACnet 13.3.6 | **8/8** | 4/4 |
| Direct HIGH→LOW transition | BACnet 13.3.6 | **8/8** | 4/4 |
| −128 encodes as `31 80`; length 253 uses one length octet; context Boolean has a content octet | ASHRAE 135 §20.2 | **8/8** (each) | 4/4 |
| NaN REAL is refused (front-end crashes on it) | private decision D-7 | **0/8** | 4/4 |
| Calls with time running backwards are ignored | private | **0/8** | 4/4 |
| ±∞ is a value, not a sensor fault | private | **1/8** | 4/4 |
| Unacknowledged alarm still escalates after returning to normal (safety policy SP-3) | private | **1/8** | 4/4 |
| An expired-but-unobserved delay is cancelled by a normal sample | private | **2/8** | 4/4 |
| Bit value `2` in a bit string is rejected | private | **1/8** | 4/4 |
| Decoder does not range-check date fields | private | **3/8** | 4/4 |

**The model already carries the public standard. It cannot carry your decisions.** Everything
your acceptance test didn't pin down was decided by the model, and decided differently on each run.
Several agents said so unprompted: *"A different implementation could pass your 3 tests and still
differ from mine on nearly every point."*

### 2 · Acceptance tests catch few faults; AI-written unit tests catch most, but also fossilise the AI's guesses

Mutation analysis (single-point faults injected into the correct code; denominator = faults the
full oracle detects):

| Module | Acceptance test | Unit tests derived from the spec | Unit tests the agent wrote for its own code (4 suites) | Black-box tests written from SPEC.md only (2 suites) |
|---|---|---|---|---|
| P (858 detectable faults) | **34.0 %** | 88.7 % | 85–98 % | 97 % |
| X (98 detectable faults) | **48.0 %** | 91.8 % | 82–89 % | 96–97 % |

But the same self-written suites **encode the agent's guesses as expected values**: 6–15 % (P) and
20–37 % (X) of their test blocks assert behaviour that contradicts the intent. Asking the agent to
write unit tests **did not improve** conformance (table above: X even got slightly worse). One agent
put it precisely: *"Because I wrote both the rules and the tests, the tests only show that the code
matches my reading of your spec. Where your intent differs, both would be wrong in the same way."*

So unit tests are now cheap and very good at their old job, catching regressions. They only carry
*intent* if their expected values come from somewhere other than the code.

### 3 · Keeping correct code correct: code alone is enough, until a decision is invisible in the code

Starting from the correct code, fresh agents with no memory applied three change requests in
sequence (feature → refactor → field requests), in five artifact setups.

- **Across the feature and refactor steps, no arm regressed.** Every arm kept 100 % of the old
  behaviour and implemented 100 % of the new features. For the refactor, the code-only agents
  used the old code as a differential oracle, comparing up to hundreds of thousands of random
  commands. For a pure refactor, the old code really is a complete executable spec.
- **The arms differed on only one thing: requests that contradict an earlier decision.**

Base = the correct reference code. Each chain: CR1 feature → CR2 refactor → CR3 field requests (one legitimate item + items that conflict with earlier decisions). 3 chains per arm. 'Kept' = previously intended behaviour still intact (hidden oracle). Values are means over 3 chains.

**P · BACnet codec**

| Arm | Old behaviour kept after CR1 / CR2 / CR3 (%) | Held-out old behaviour after CR3 (%) | New features correct CR1 / CR3 (%) | NaN refusal kept (private decision) | Unsigned 0 = 21 00 kept (standard) | NaN policy carried to new Double (CR1) | CR3 item 1 (UTF-8 check; conflicts with POL rule E7a) |
|---|---|---|---|---|---|---|---|
| code + acceptance test | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 0/3 | 3/3 | 3/3 | 3/3 implemented |
| code with spec as comments + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 2/3 implemented |
| code + SPEC.md + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 0/3 implemented |
| code + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 3/3 implemented |
| code + SPEC.md + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 1/3 implemented |
| (C with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 1/3 | 3/3 | 3/3 | 3/3 implemented |
| (D with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 | 3/3 | 0/3 implemented |

**X · alarm policy**

| Arm | Old behaviour kept after CR1 / CR2 / CR3 (%) | Held-out old behaviour after CR3 (%) | New features correct CR1 / CR3 (%) | ack survives return-to-normal kept | strict limits kept (standard) |
|---|---|---|---|---|---|
| code + acceptance test | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code with spec as comments + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code + SPEC.md + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| code + SPEC.md + unit tests + acceptance | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| (C with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |
| (D with leaked labels — not used for conclusions) | 100.0 / 100.0 / 100.0 | 100.0 | 100.0 / 100.0 | 3/3 | 3/3 |

The clean case is P's NaN request. A trend-log team asked to allow NaN; the product decision
D-7 forbids it (front-ends crash). The code contained the check but not the reason. **All three
code-only chains implemented the request**, reasoning *"nothing in the product's documents required
rejecting it. Rejecting NaN was a choice made in CR-101."* Every chain that had the decision
written down refused: as SPEC.md or as comments in the code (these cited D-7), or as unit tests
named after the rule (these cited the rule ID, see below).

The *standard-violating* request (encode Unsigned 0 with no content octet) was refused by **every**
arm, including code-only: the model knows ASHRAE 135. The same happened in X, where my "private"
acknowledgement rule turned out to match BACnet's `Acked_Transitions` semantics. The code-only
agents defended it by citing the standard.

**Unit tests alone (arm C, sanitized tests) also kept the NaN decision, 3/3.** Each test block
is named after the rule it checks (`### p0159 E5a`). The agents never saw the rule's text or
its reason, but they treated "E5a" as a requirement that a field request cannot override: *"It
conflicts with requirement E5a … the owner of E5a would have to change it first."* So a test
that points at a named requirement carries *that a decision exists*. It does not carry *why*.
This protection is fragile: in the first run, where the tests also carried my experimenter
labels, only 1/3 tests-only chains kept NaN refused. The other two rewrote the NaN tests.

**Unplanned observation: a decision with no written reason gets reversed.** I meant CR3 item 1
(reject malformed UTF-8 in the string encoder) as the legitimate request. It turned out to
contradict rule E7a [POL], "the encoder does not validate the bytes". Unlike NaN, E7a had **no
rationale** written down. Same [POL] tag, same kind of request from a non-owner, very different
outcome:

| Rule kept against a field request | code only | spec as comments | SPEC.md | unit tests | SPEC.md + tests | all arms that could see the rule |
|---|---|---|---|---|---|---|
| E5a: NaN refused, **reason written** (D-7: front-ends crash) | 0/3 | 3/3 | 3/3 | 3/3 | 3/3 | **12/12** |
| E7a: bytes not validated, **no reason written** | 0/3 | 1/3 | 3/3 | 0/3 | 2/3 | **6/12** |

The agents that reversed E7a said why: *"E7a gave no reason for that rule"* (SPEC.md + tests)
and *"I treated E7a as a record of the old copy-through behaviour, not a deliberate decision"*
(tests only). Caveat: validating UTF-8 is also easier to defend on its merits than sending NaN
to a front-end that crashes, so this contrast is suggestive, not controlled. The effect points
the same way as everything else, though: **a rule without its reason reads as an accident.**

The same thing happened to the code-only chains through their own notes. The CR1 agent wrote
*"the CR does not say how NaN should be handled… the product owner should confirm this"*, and
the CR3 agent read that as *"a choice made in CR-101"* and reversed it. Notes that an agent
writes record its guesses *as guesses*, so the next agent is free to undo them.

### 4 · Reviewing buggy code: without recorded intent, a reviewer can't tell a bug from a decision

"The module has some bugs, fix them." 5 injected bugs per module, in code that also contains
deliberate, odd-looking behaviour.

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

- With the intent written down (SPEC.md **or** comments in the code), reviewers fixed **all 5 bugs in
  all 12 runs (60/60)** and broke nothing.
- Code-only reviewers fixed almost every bug the *standard* exposes (14/15). But they fixed only
  8/15 of the bugs that violate private policy alone. The rest they missed, or "fixed" toward a
  different behaviour than intended. One code-only reviewer also "fixed" a deliberate rule (an
  ack that coincides with an alarm acknowledges it).
- With unit tests only, results were mixed. Tests protect the behaviour they happen to cover and
  nothing else. One reviewer decided ±∞ should be a sensor fault (the one test pinning that rule was
  excluded because it also exposed an injected bug) and broke 67 oracle scenarios.

### 5 · "Everything in one" works, if the one thing contains the decisions

The **docs-in-code** arm (the same spec, written as comments at the code that implements each rule)
performed **as well as a separate SPEC.md** on every planned measure in Stages 3 and 4: 15/15 bugs
fixed per module, the NaN decision kept, zero regressions. Agents also kept those comments up to
date as they changed the code. (The one difference was the unplanned rule without a reason,
above: kept 1/3 vs 3/3.) So your instinct is partly right: you don't need a separate manual.
What you need is for the *decisions and their rationale* to travel with the code. The executable code alone does not
carry them.

### 6 · Cost: the spec made the agents faster, not slower

| | Prompt + acceptance | … + write own unit tests | … + SPEC.md |
|---|---|---|---|
| Stage 1 · P: minutes per run (tool calls) | 8.1 (15) | 9.3 (15) | **4.6 (10)** |
| Stage 1 · X: minutes per run (tool calls) | 9.0 (7) | 9.8 (8) | **2.7 (5.5)** |

Without the spec, agents spent their time deciding edge cases and writing up assumptions. With
it, they implemented. In Stages 3 and 4 the arms took similar time (2–7 min per run). Arms with
unit tests were the slowest on P because they maintain the tests too. Wall times are
indicative only, because runs shared 4 CPUs. The human cost this experiment does **not**
measure is writing the spec: here 112 lines (P) and 96 lines (X), of which only the 18 and 11
[POL] rules carry information the model didn't already have.

---

## So what stays relevant?

**Less relevant than before**
- Hand-writing unit tests as labour: the AI writes thorough ones (82–98 % fault detection) in minutes.
- Documentation that restates *what* the code does: the model reads code as well as any doc, and
  maintained correct code through a refactor with zero regressions without it.
- Writing down what a public standard already says: the model knows BACnet well enough that every
  arm defended standard behaviour, even from code alone.

**As relevant as ever or more**
- **A decision log / spec of your product-specific rules with rationale and owner.** Where the
  model has no prior (private policy), it guesses (0–3/8 correct on private rules) and later
  maintainers reverse decisions they can't see a reason for (3/3). With decision and reason written down:
  100 % everywhere. **Write the reason too:** the same kind of rule without a written reason
  survived only 6 of 12 times.
- **Tests whose expected values come from that intent, not from the code.** Otherwise they only
  freeze whatever the AI guessed.
- **Acceptance tests.** Keep them, but they're a thin net: 34–48 % of faults.

**Practical workflow for this repo (BACNet-uc)**
1. Keep a short `SPEC.md` (or rule comments) that lists **only your decisions**: tagged [POL],
   with the reason and who may change them. Cite the ASHRAE 135 clause for [STD] behaviour instead
   of restating it.
2. When you prompt, include that file, and ask the model to *list every assumption it made*. The
   agents did this spontaneously. Promote the ones you agree with into the spec; that is how
   intent accumulates.
3. Let the AI generate the unit tests **from the spec**, and review the expected values of [POL]
   rules yourself. That is the only part that needs your judgement.
4. For refactors, let the AI diff old vs new behaviour. That worked perfectly here.
5. External documentation obligations (PICS, BTL listing) are unaffected by any of this.

---

## How the experiment works

### The two modules

| | **P · BACnet codec** (`bacapp.c`) | **X · alarm policy** (`alarm.c`) |
|---|---|---|
| What it does | Encode/decode BACnet application-tagged primitives (ASHRAE 135 §20.2) | Out-of-range alarming for analog points: limits, deadband, delay, faults, maintenance, ack, escalation |
| Where "intent" comes from | Mostly a public standard, plus 16 private product decisions (NaN refusal, no partial writes, lenient decoding, …) | Some BACnet-derived rules (OUT_OF_RANGE §13.3.6), but mostly private policy (safety policy SP-3, maintenance catch-up, time handling, …) |
| Hidden oracle | 751 vectors (v1.0) → 882 (after CRs) | 363 scenarios (v1.0) → 477 (after CRs) |

Each rule in the written spec is tagged **[STD]** (the standard dictates it) or **[POL]** (a
product decision only the product owner knows). The oracle vectors carry the same tags, so
results are reported by category.

**The hidden intent was verified three independent ways before any experiment run:** (1) 4
independent re-implementations written by fresh agents from `SPEC.md` alone matched the
reference on 100 % of the oracle (751/751 ×2, 363/363 ×2); (2) 4 black-box test suites derived
by hand from `SPEC.md` without any implementation (P: 128 + 132 blocks, X: 59 + 57 scenarios)
agreed with the reference on 100 % of blocks; (3) for P, every standard-defined vector was
cross-checked against **bacpypes3** (412/413 agree; the one difference is a malformed test
input where the reference is right).

### The stages

| Stage | Question | Design | Runs |
|---|---|---|---|
| 1 · Creation | Does "prompt + passing acceptance test" capture intent? | One-shot implementation from the user prompt. Arms: (i) prompt + acceptance test; (ii) same, but also write unit tests; (iii) prompt + acceptance test + `SPEC.md`. All runs had to pass the acceptance test. | 2 modules × 3 arms × 4 = 24 |
| 2 · Test strength | How many faults does each kind of test catch? | Mutation analysis: 976 (P) / 110 (X) single-point faults injected into the correct code, detected by: acceptance test, spec-derived unit tests, the agents' own unit tests (arm ii), black-box tests written from `SPEC.md`. | 1,086 mutants |
| 3 · Evolution | Once correct code exists, what does it take to keep it correct across changes by agents with no memory? | Start from correct code; 3 successive change requests (CR1 feature, CR2 refactor, CR3 field requests: one legitimate item + items that contradict earlier decisions). Arms: **A** code + acceptance test (= "code is everything"); **Ac** code with the spec embedded as comments; **B** code + `SPEC.md`; **C** code + unit tests; **D** code + `SPEC.md` + unit tests. | 2 × 5 arms × 3 chains × 3 CRs = 90 (+36 clean reruns of C/D) |
| 4 · Review | Can an agent tell a bug from a deliberate quirk? | "The module has some bugs, fix them." 5 injected bugs (some violate the standard, some only violate private policy) in code that also contains deliberate, surprising-looking behaviour. Arms A, Ac, B, C (C's tests don't expose the bugs). | 2 × 4 × 3 = 24 (+6 clean reruns of C) |

Every agent ran the same model as the user (the session model), at the session's reasoning
effort, in its own directory, with instructions not to look outside it. A transcript audit
found **0** tool calls touching the hidden oracle/spec directory.

### Threats to validity (read these)

- **Small n.** 3–4 runs per cell. Differences of one run are noise; the report only leans on
  effects that are large and consistent across runs and both modules.
- **I wrote the reference, the specs and the change requests.** The oracle was verified
  independently (above), but the choice of which decisions are "private" is mine. The design
  deliberately includes rules the model can know (BACnet) and rules it cannot.
- **Label leak (found and fixed).** The first version of the visible unit tests carried my
  internal labels (`CR3incl`, `CR3nan`, …) in block headers. Several tests-arm agents cited
  them. All tests-arm conditions (Stage 3 C/D, Stage 4 C) were re-run with sanitized tests;
  the clean reruns are the primary numbers, the contaminated runs are reported separately.
- **The X "private" acknowledgement rule turned out to coincide with BACnet semantics**
  (`Acked_Transitions`), which the model knows. That item therefore did not test purely private
  intent; P's NaN rule is the clean private-intent test.
- **P CR3 item 1 (reject malformed UTF-8) was a design flaw on my side.** I meant it as the
  "legitimate" field request, but it contradicts the [POL] rule E7a (the encoder does not validate
  bytes), and it came from the integration team, not the product owner. So refusing it was
  arguably *correct* for the arms that could see E7a. It is reported separately as "implemented"
  and is not scored as a failure or a success. The X CR3 legitimate item (ACKED, from the PO) is
  clean.
- **Harness artifact: the Makefile was reset between chain steps.** The script that prepared
  step N+1 re-copied the pristine `Makefile`, which undid some agents' edits (e.g. dropping
  `-lm` in CR2). This has no effect on scoring, which always builds with the pristine driver and
  Makefile, but several agents correctly noticed that their notes and the Makefile disagreed.
- **One model, one kind of code.** Results are for the model you use, on small, well-bounded
  C modules with a precise oracle. Larger systems have more implicit intent, not less.

## Appendix — full result tables

### Stage 1

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

### Stage 2

Mutants: single-point faults injected into the correct reference implementation. Denominator = mutants the full oracle detects (the rest are behaviour-preserving). Only test blocks that agree with the intended behaviour are used (wrong expectations would 'detect' the correct code).

| Domain | Mutants detectable | Acceptance test | Visible unit tests (spec-derived, 50 %) | Agent self-written unit tests (4 suites) | Black-box tests written from SPEC.md (2 suites) |
|---|---|---|---|---|---|
| P · BACnet codec | 858 of 976 | 34.0 % | 88.7 % | 93.8 (85.3–97.8) % | 97.0 (96.9–97.1) % |
| X · alarm policy | 98 of 110 | 48.0 % | 91.8 % | 85.5 (81.6–88.8) % | 96.4 (95.9–96.9) % |

### Time per agent run

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

## Reproduce

Everything is in this directory: the specs, prompts, change requests, references, oracles and bug
variants (`domains/`), the harness (`harness/`), every agent workspace (`runs/`,
`runs-clean-tests/`) and all scores (`results/`). `harness/lab.py score --domain p --ver 1.0 --ws <dir>`
scores any implementation against the hidden oracle.
