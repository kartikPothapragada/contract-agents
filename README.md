# ClauseGuard — agentic contract risk triage

**Avathon AI/ML Hiring Challenge — Track A (Agentic / Multi-Agent AI) × Scenario 2 (Gen AI for Enterprise Documents)**
Kartik Pothapragada

---

## The problem

A mid-market enterprise signs a few hundred vendor agreements a year — MSAs,
DPAs, SOWs. Every one arrives on the vendor's paper, and every one has to be
checked against the company's own contracting playbook: is the liability cap
mutual and bounded? Is breach notice within 72 hours? Can we terminate for
convenience?

Legal teams do not scale with deal volume. What actually happens is triage by
availability: the strategic deals get read properly, the long tail gets skimmed,
and the expensive surprise is found two years later during an incident.

**ClauseGuard triages the queue.** Six agents classify the document, locate each
clause, compare it against the playbook, score the exposure, verify their own
claims against the source text, and either clear the contract or put a specific,
evidenced escalation packet in front of counsel.

The design commitment that shapes everything else: **the system is never allowed
to report a finding it cannot ground in the contract.** When it cannot verify
itself, it escalates rather than asserts.

---

## Run it

Nothing here needs an API key. The default backend is a deterministic rule
engine that implements every interface the agents call, so the full graph — the
routing, the repair loop, the human gate, the traces, the cost report — runs
offline in seconds.

```bash
git clone <repo> && cd contract-agents
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

```bash
python scripts/generate_contracts.py --n 24 --seed 42
```

```bash
python scripts/run_scenarios.py
```

That prints both required end-to-end traces and writes them to `results/traces/`.
(The scripts put `src` on the path themselves; `pip install -e .` also works.)

To run against a real model instead:

```bash
export ANTHROPIC_API_KEY=...   # or OPENAI_API_KEY, or run ollama serve
python scripts/run_scenarios.py --backend anthropic
```

Reproduce every reported number:

```bash
python scripts/generate_contracts.py --n 24 --seed 42 && python scripts/generate_contracts.py --n 8 --seed 7 --adversarial --out data/contracts/adversarial && python scripts/eval_retrieval.py && python scripts/evaluate.py --backend stub && python scripts/evaluate.py --backend stub --corpus data/contracts/adversarial && python -m pytest tests/ -q
```

Requires **Python 3.11+** (PEP 604 `X | Y` annotations at runtime in the Pydantic
schemas). Tested on 3.11.5.

---

## The agent system

```
                          ┌──────────────────────────────────────────┐
                          │            playbook (14 rules)           │
                          │   hybrid retrieval: BM25 + dense + RRF   │
                          └────────────────┬─────────────────────────┘
                                           │ tool
  contract ──► INTAKE ──► EXTRACTOR ──► POLICY ──► RISK ──► VERIFIER ──┬──► DRAFTER ──► memo
               classify    locate        compare    score    critique   │
               + scope     spans         vs rule    (no LLM) own claims │
                              ▲                                         │
                              │                                         │
                              └──── REPAIR (budget 2) ◄─────────────────┤
                                           ▲                            │
                                           │ reject                [escalate]
                                           │                            │
                                      HUMAN GATE ◄─────────────────────-┘
                                   approve / override / reject
```

| Agent | Role | Tier | Tools |
|---|---|---|---|
| **Intake** | Classify doc type, counterparty, forum, value tier; scope which playbook rules are engaged | fast | `segment_contract` |
| **Extractor** | Locate the governing span for each in-scope clause type | fast | `section_retrieval` (hybrid) |
| **Policy** | Compare observed language against the playbook rule, assign severity, draft redline | **deep** | `playbook_retrieval` (hybrid) |
| **Risk** | Score exposure and decide escalation | **none** | `score_clause` |
| **Verifier** | Adversarially check every claim against the source | deep | `check_span`, numeral comparison |
| **Drafter** | Assemble the counsel-ready memo | deep | — |

### Four decisions that carry the design

**1. Risk scoring is deterministic Python, never an LLM.**
The escalation threshold decides whether a lawyer sees a contract. An LLM
computing it opens three failures at once: arithmetic drift, prompt sensitivity
(5.8 on one run, 6.2 on the next, flipping the gate), and unauditability — a
regulator asking "why was this not reviewed?" cannot be answered with a sampled
token. The model supplies *categorical judgement*; Python supplies the
*arithmetic and the threshold*. `score = severity_points × criticality_weight ×
value_multiplier`, every factor versioned in the playbook.

**2. Offsets are computed, never reported.**
Ask a model for character positions and it returns confident, plausible, wrong
integers. The extractor returns quoted text; Python finds that text in the
source and derives the offsets. If the quote cannot be located, the finding is
marked not-found rather than recorded at a guessed position.

**3. The verifier is a separate adversarial role, and it runs free checks first.**
A model grading its own output grades it generously, and a self-check shares the
context that produced the error. So verification is its own agent, and it checks
in cost order: exact span match (free) → numeral comparison (free) → LLM
entailment (paid, and only on grounded findings with non-trivial severity).

**4. Absence is a finding.**
A contract with no liability cap is the worst thing this system will ever see. A
pipeline that only reasons over text it found returns a clean report for it. So
three clause types are never dropped by the model's scoping judgement, and a
missing clause is assessed on the consequence of its absence.

### Message schema

Every inter-agent message is an `AgentMessage` envelope with a discriminated-union
payload — no agent ever receives a bare dict or a string. Full definitions in
[`src/clauseguard/schemas/messages.py`](src/clauseguard/schemas/messages.py).

```json
{
  "schema_version": "1.0",
  "msg_id": "msg_302a686ebc59",
  "trace_id": "syn-003-a71f2c94",
  "parent_msg_id": "msg_ba31d0917c42",
  "sender": "policy",
  "recipient": "risk",
  "msg_type": "deviation.assessment",
  "created_at": "2026-09-11T14:22:07.913204+00:00",
  "payload": {
    "kind": "deviation.assessment",
    "clause_type": "LIMITATION_OF_LIABILITY",
    "rule_id": "PB-LIAB-01",
    "severity": "UNACCEPTABLE",
    "observed_position": "Cap of 3 months' fees is below the six-month floor",
    "rationale": "Playbook PB-LIAB-01 requires a mutual cap at the greater of ...",
    "cited_span_text": "shall not exceed the fees paid in the three (3) months",
    "suggested_redline": "Aggregate liability of each party is capped at ..."
  },
  "usage": {"model": "claude-sonnet-4-5", "prompt_tokens": 1834, "completion_tokens": 212, "usd": 0.008682},
  "latency_ms": 2140.7
}
```

`trace_id` correlates a run; `parent_msg_id` reconstructs the causal DAG, so
"what caused this escalation?" is answerable by walking backwards
(`TraceBus.lineage`). `usage` sits on the envelope rather than inside agent code,
so cost accounting is a property of the transport and stays complete for agents
nobody remembered to instrument.

Payload types: `document.profile`, `clause.finding`, `deviation.assessment`,
`risk.score`, `verification.verdict`, `escalation.request`, `human.decision`,
`redline.memo`, `tool.call`, `tool.result`, `agent.error`.

### The human-in-the-loop gate

Implemented with LangGraph `interrupt()` plus a checkpointer, so a run genuinely
**suspends** and can be resumed hours later in a different process — a lawyer
does not review a contract inside a 30-second function call.

The reviewer gets, per escalated clause: the playbook's standard position, what
the contract actually says, the verbatim span, the model's reasoning, the
computed score, the routing owner, and whether the system could verify its own
claim. Three actions:

- **APPROVE** — findings stand, memo is drafted
- **OVERRIDE** — reviewer sets a different severity; recorded alongside the
  model's original call so disagreement stays measurable
- **REJECT** — sends the work back for re-extraction, scoped by the reviewer

Three independent escalation triggers, OR-ed rather than blended: any clause over
threshold, aggregate over threshold, or **any unverified assertion**. The third is
what makes it a safety gate rather than a scoring heuristic — it fires on system
uncertainty, independent of risk level.

---

## Results

Two corpora. **Severity labels are synthetic** — no public corpus labels
deviation severity against *one company's* playbook, because that label is a
function of the policy. The generator picks a variant with a known severity and
renders it into prose, so the label is a property of the generator rather than of
someone's reading. The adversarial corpus carries the *same* severities in
deliberately awkward drafting (defined terms, double negatives, figures displaced
into exhibits, rates restated annually).

All numbers below are the **deterministic rule-engine backend**. That is a
floor, not the system's ceiling — see the honesty note.

### Clause-level judgement

| Metric | Synthetic (24 docs, 233 judgements) | Adversarial (8 docs, 78) |
|---|---:|---:|
| Severity accuracy (exact) | 0.880 | 0.872 |
| Adjacent-band rate | 0.056 | 0.051 |
| **Dangerous-miss rate** | **0.000** (0/120) | **0.140** (6/43) |
| False-alarm rate | 0.093 (9/97) | 0.000 (0/32) |
| Absence detection recall | 1.000 (7 cases) | 1.000 (2) |
| Groundedness rate | 1.000 (233 checked) | 1.000 (78) |
| Escalation recall / precision | 1.000 / 0.826 | 0.875 / 1.000 |
| Cost / latency per contract | $0.00 / 7.1 s | $0.00 / 10.0 s |

**Dangerous-miss rate is the headline, not accuracy.** The loss function is
brutally asymmetric: a false alarm costs a lawyer ten minutes; a real risk graded
safe costs the liability cap. A system can be 90% accurate and worthless if its
10% sits entirely in that cell.

### The result that matters most

In-distribution the rule engine has **zero** dangerous misses. On adversarial
drafting it misses **every single liability clause** — 6 of 6, all
`LIMITATION_OF_LIABILITY`, all graded MINOR when the gold label is UNACCEPTABLE.

The clause it fails on reads:

> *"Notwithstanding anything to the contrary, the Liability Cap shall be as
> defined in Section 1.14. For the avoidance of doubt, nothing in this Section
> shall be construed to impose any ceiling whatsoever upon the obligations of
> Customer under Section 9."*

Uncapped customer liability, expressed as a double negative, with the cap itself
displaced into a defined term. There is no regex for that. **This gap is the
entire argument for putting an LLM in the Policy agent** — and it is measured
rather than asserted, which is why the weak baseline was strengthened first
(4 extra clause assessors took the synthetic dangerous-miss rate from 42% to 0%
before this comparison was drawn).

Note the escalation gate still caught 7 of 8 adversarial contracts, because other
clauses in those documents tripped it. Defence in depth works — but it caught
them for the *wrong reason*, and a reviewer reading that memo would see a clean
liability position.

### Retrieval ablation

32 labelled playbook queries, stratified by signal type.

| Mode | recall@1 | recall@3 | MRR@3 |
|---|---:|---:|---:|
| BM25 only | 0.719 | 0.844 | 0.760 |
| Dense only | 0.688 | **0.969** | 0.812 |
| **Hybrid (RRF)** | **0.750** | 0.938 | **0.839** |

recall@1 by query kind:

| Kind | n | BM25 | Dense | Hybrid |
|---|---:|---:|---:|---:|
| lexical ("net 15", "72 hours") | 11 | **1.000** | **1.000** | **1.000** |
| semantic (paraphrase, no shared vocabulary) | 11 | 0.364 | 0.545 | **0.636** |
| mixed | 8 | **0.875** | 0.500 | 0.750 |
| adversarial (vocabulary points at the wrong rule) | 2 | 0.500 | 0.500 | **0.000** |

Hybrid wins overall recall@1 and MRR, and wins on the semantic queries that BM25
cannot touch. **It loses on both adversarial queries** — where both legs rank the
same distractor first, RRF compounds the agreement instead of correcting it.
Fusion is not a safety net against correlated error. With 2 queries this is
directional, not significant, but it is the right place to look next.

Dense-only beats hybrid on recall@3. If the Policy agent consumed the top 3 rules
rather than the top 1, dense-only would be the better choice — the hybrid is
justified by the top-1 regime this system actually uses.

### Honesty note on these numbers

They were produced by the **deterministic rule engine**, not a language model.
That backend exists so the repo is reproducible without credentials, and so
orchestration bugs are separable from model quality. Its numbers measure **the
pipeline**; they are a floor on what the system does, and they should not be read
as LLM performance. Run `--backend anthropic` for that. Every file in `results/`
records the backend that produced it.

The synthetic corpus is templated, so a model could in principle learn the
template rather than the legal content — which is exactly what the adversarial
split exists to detect, and exactly what it did detect.

---

## Repository layout

```
src/clauseguard/
  schemas/messages.py      inter-agent message schema (v1.0)
  agents/                  intake, extractor, policy, risk, verifier, drafter
  orchestration/
    graph.py               LangGraph state machine + human gate + routing
    trace.py               trace bus: causal DAG, cost attribution, replay
    runner.py              suspend/resume entry points
    state.py               graph state + repair budget
  tools/
    retrieval.py           BM25 (hand-rolled) + dense + RRF fusion
    playbook.py            playbook store, the Policy agent's retrieval tool
    segmentation.py        deterministic sectioning with exact offsets
    scoring.py             deterministic risk scoring and escalation
    verification.py        span grounding, numeral comparison
  llm/
    backends.py            anthropic / openai / ollama / stub, tiering, cost
    stub_logic.py          the deterministic rule engine

data/playbook/playbook.yaml    14-rule synthetic contracting playbook
scripts/                       generate_contracts, run_scenarios, evaluate, eval_retrieval
eval/retrieval_queries.yaml    32 labelled playbook queries
results/                       traces, evaluation JSON, scenario summaries
tests/                         35 tests
writeup/                       technical write-up (PDF)
```

## Cost and latency

Multi-agent systems get expensive invisibly. Five levers, in order of effect:

1. **Clause scoping at intake** — the playbook has 14 rules; a given contract
   engages 5–8. The deep-tier Policy agent runs only on those.
2. **Model tiering** — agents declare `fast` or `deep`, not a model name.
   Classification and extraction run cheap; only policy reasoning and
   verification run deep.
3. **Free checks before paid ones** — the verifier's span and numeral checks cost
   nothing and resolve most cases before the entailment call.
4. **Deterministic tools for anything non-linguistic** — segmentation, scoring,
   grounding. Zero tokens, and they are the parts that must be reproducible.
5. **Cached section index across repair passes** — re-embedding an unchanged
   document was ~1.5 s per pass for a byte-identical result.

What was traded away: the extractor runs one LLM call per clause type rather than
one batched call per contract. Batching would cut calls ~5×, at the cost of
per-clause confidence and clean per-clause repair. For a system whose value is
per-clause auditability, that was the wrong trade — but it is the first thing to
revisit under cost pressure.

## Limitations

- **Severity labels are synthetic.** Extraction could be validated against CUAD's
  human annotations; severity cannot be validated against anything public.
- **Text in, not PDF in.** Segmentation assumes numbered headings and degrades on
  scanned documents, tables and exhibit-heavy paper. It flags low confidence
  rather than mis-segmenting silently, but it does not solve it.
- **English, US/EU commercial contracts only.**
- **The playbook is a flat rule list.** Real playbooks have conditional
  positions ("if the vendor processes special-category data, then…"). Nothing
  here models that.
- **Single-document.** Amendments, order forms and MSAs that modify each other
  are reviewed in isolation, which is wrong for the amendment case.

## Deliverables

- **Technical write-up PDF:** [`writeup/ClauseGuard_TrackA_Scenario2.pdf`](writeup/ClauseGuard_TrackA_Scenario2.pdf)
- **Write-up:** [`writeup/ClauseGuard_TrackA_Scenario2.md`](writeup/ClauseGuard_TrackA_Scenario2.md)
- **Agent traces:** [`results/traces/scenario_a_success.md`](results/traces/scenario_a_success.md),
  [`results/traces/scenario_b_edge_case.md`](results/traces/scenario_b_edge_case.md)
- **Walkthrough video:** _(link to be added)_
