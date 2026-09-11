# ClauseGuard — Agentic Contract Risk Triage
**Kartik Pothapragada · Track A (Agentic / Multi-Agent AI) × Scenario 2 (Gen AI for Enterprise Documents)**

---

## 1. Problem & domain

A mid-market enterprise signs a few hundred vendor agreements a year, all on the
vendor's paper, all needing a check against its own contracting playbook. Legal
headcount does not scale with deal volume, so what happens in practice is triage
by availability: strategic deals get read, the long tail gets skimmed, and the
expensive term surfaces two years later during an incident.

ClauseGuard triages that queue. Six agents classify the document, locate each
clause, compare it against a versioned playbook, score the exposure, verify their
own claims against the source text, and either clear the contract or place a
specific, evidenced escalation packet in front of counsel.

**Why Track A is the right fit, and why it was not the obvious one.** Track D
(RAG) is the reflexive answer for "enterprise documents", and it is the wrong
one here. The deliverable is not an answer to a question — it is a *decision with
a consequence*: does this contract go to a lawyer? That decision needs
conditional routing, a bounded retry, a suspendable approval gate, and an audit
trail of who concluded what from which text. A retrieval pipeline produces a
grounded answer; it does not produce an accountable decision. Track A does.

**Q1 — why agentic rather than one LLM call or classical ML?**
A single prompt could plausibly grade clauses. It could not do three things this
problem requires. *It cannot check itself* — a model asked to grade its own
output grades it generously, and shares the context that produced the error;
separating the Verifier into an adversarial role is what makes groundedness
enforceable. *It cannot suspend* — the human gate must stop the run, persist, and
resume hours later in another process. *It cannot be partially retried* — when
one clause fails verification, re-extracting that clause alone costs one call;
re-prompting a monolith costs the whole contract.
Classical ML fails earlier: supervised clause classification needs labels that
are a function of *one company's playbook*, and the playbook changes by
amendment, not by retraining. When the legal team edits a YAML line, this system
changes behaviour immediately.

---

## 2. Approach & algorithm decisions

**Q2 — orchestration framework.** LangGraph, over CrewAI and AutoGen.
The control flow needed is a state machine with a suspend point, not a
conversation. CrewAI reaches a demo faster and its role abstraction reads better,
but its human input is a blocking console prompt; AutoGen's is a turn in a chat
loop. Both model the human as a fast participant rather than an asynchronous
approver, and this gate has to survive a process boundary. LangGraph's
`interrupt()` plus a checkpointer is exactly that primitive. Second, routing
(escalate / repair / continue) is a business rule with an audit requirement —
conditional edges put it in one readable function, whereas a conversational
framework leaves it inside a model's choice of who to speak to next, which
cannot be shown to a compliance reviewer.
**Accepted trade-off:** more wiring, and no emergent inter-agent dialogue. That
was the point. The value of a risk gate is that it behaves identically every
time; this problem wants *less* emergence, not more.

**Q3 — agent decomposition.** Six agents, split along the axis of *what kind of
thing is being decided*, not along document structure.
Intake (classify + scope) is separate from Policy (judge) so a misclassified
document produces a visibly wrong profile rather than a plausible risk report
built on a wrong premise. Risk is separate from Policy because it contains **no
LLM at all** — the escalation threshold decides whether a lawyer sees a contract,
and an LLM computing it introduces arithmetic drift, prompt sensitivity (5.8 one
run, 6.2 the next, flipping the gate) and unauditability. The model supplies
categorical judgement; Python supplies the arithmetic and the threshold.
Verifier is separate for the self-grading reason above. Drafter is separate and
is denied access to the raw contract, so it cannot reintroduce an assertion the
Verifier just rejected.
*Why not fewer?* Merging Verifier into Policy destroys the adversarial property.
*Why not more?* A per-clause-type agent was considered and rejected: fourteen
near-identical agents differing only by prompt is a lookup table wearing a
costume, and the playbook already is that table.

**Retrieval (a tool, not the deliverable).** BM25 + dense, fused with Reciprocal
Rank Fusion. Playbook lookup is a mixed query type — some queries turn on
paraphrase, others on unforgiving literals ("net 15", "72 hours", "SOC 2"). Dense
retrieval blurs exactly those numerals; BM25 nails them and is helpless on
paraphrase. RRF rather than score interpolation because BM25 scores and cosine
similarities are not on a comparable scale, and normalising them adds a tuning
parameter that must be re-fit whenever the corpus changes. BM25 is implemented
in-repo (~40 lines) rather than imported, so `k1` and `b` stay visible.

**Q4 — where it fails silently, and what catches it.**
The dangerous failures in this system are quiet by construction — a fluent,
well-reasoned, wrong finding. Four mitigations, in cost order:

1. **Fabricated citations.** Every assessment must carry a verbatim span; it is
   checked as an exact substring of the source. Free, and it catches the dominant
   failure — a model paraphrasing the contract into language that sounds right
   and does not exist.
2. **Real quote, wrong number.** The span check passes because the quote is
   genuine; only the figure is wrong. Caught by comparing numerals categorically.
   This check exists because a *test caught the fuzzy-match floor waving a
   substitution through*: "three (3) months" → "thirty (30) months" scores 0.93
   similarity on a long quote. Character similarity is simply the wrong
   instrument for digit substitution. Numeric conflicts route to **escalate**,
   not retry — re-extraction returns the same passage, so only a human catches it.
3. **Hallucinated offsets.** The extractor returns quoted text and *Python*
   derives the character positions. A model asked for offsets returns confident,
   plausible, wrong integers, and every downstream check would then verify
   against a fiction.
4. **Silence about a missing clause.** A pipeline that reasons only over text it
   found returns a clean report for a contract with no liability cap — its worst
   possible output. Three clause types are never dropped by the model's scoping,
   and absence is assessed on its consequence.

Unbounded agent loops are the other silent failure. The repair budget is 2,
enforced on the graph state rather than inside any agent, and exhausting it
escalates rather than spinning.

---

## 3. Results & error analysis

All figures below are the **deterministic rule-engine backend**, which exists so
the repo runs without credentials and so orchestration bugs stay separable from
model quality. These numbers are a **floor on the pipeline**, not LLM
performance; every file in `results/` records its backend.

| | Synthetic (24 docs, 233 judgements) | Adversarial (8 docs, 78) |
|---|---:|---:|
| Severity accuracy | 0.880 | 0.872 |
| **Dangerous-miss rate** | **0.000** (0/120) | **0.140** (6/43) |
| False-alarm rate | 0.093 | 0.000 |
| Absence detection recall | 1.000 | 1.000 |
| Groundedness rate | 1.000 | 1.000 |
| Escalation recall / precision | 1.000 / 0.826 | 0.875 / 1.000 |

**The headline metric is deliberately not accuracy.** The loss function is
asymmetric: a false alarm costs a lawyer ten minutes; a real risk graded safe
costs the liability cap. Dangerous-miss rate isolates that cell.

**The finding that matters.** In-distribution: zero dangerous misses. On
adversarially drafted paper: **every liability clause missed, 6 of 6**, all graded
MINOR against a gold label of UNACCEPTABLE. The clause reads *"nothing in this
Section shall be construed to impose any ceiling whatsoever upon the obligations
of Customer"* — uncapped liability as a double negative, with the cap displaced
into a defined term. There is no regex for that. **That measured gap is the
argument for the LLM in the Policy agent**, and it is credible only because the
rule baseline was strengthened first: four extra clause assessors took the
synthetic dangerous-miss rate from 42% to 0% before the comparison was drawn.
A baseline left weak would have overstated the LLM's value.

The escalation gate still caught 7 of 8 adversarial contracts — but for the
*wrong reason*, via other clauses, while the memo showed a clean liability
position. Defence in depth is not a substitute for the clause-level fix.

**Retrieval ablation** (32 labelled queries): hybrid leads on recall@1 (0.750 vs
0.719 BM25, 0.688 dense) and MRR@3 (0.839), and wins on semantic queries (0.636
vs 0.364 BM25). It **loses on both adversarial queries** (0.000 vs 0.500 each):
where both retrievers rank the same distractor first, RRF compounds the agreement
rather than correcting it. Fusion is not a safety net against correlated error.
Also honest: dense-only beats hybrid on recall@3 (0.969 vs 0.938) — the hybrid is
justified only by the top-1 regime this system actually uses.

**Where false alarms come from:** 9 of 97 compliant clauses graded risky, almost
all clause types with no dedicated assessor falling through to generic trigger
matching. That is the right direction to fail, but at volume it is what trains a
reviewer to ignore the gate.

---

## 4. Production & limitations

**One production consideration — updating the playbook without downtime.** The
playbook is versioned data, not code, and every assessment records the `rule_id`
and playbook version it was made under. A legal team amending a position edits
YAML; the next run picks it up. What is *not* solved: contracts assessed under
v3.2 and reviewed under v3.3 will disagree, and nothing currently re-triages the
backlog on a version bump. The right design is to treat a playbook change as an
event that re-scores affected clauses from cached extractions — extraction is the
expensive step and is version-independent, so re-scoring a quarter's contracts
against a new position should cost almost nothing. That is the first thing I
would build next.

**One limitation that blocks real deployment: severity ground truth is
synthetic.** Extraction can be validated against CUAD's human annotations;
severity cannot be validated against anything public, because the label is a
function of one company's policy. Before deployment this needs a few hundred
clauses dual-annotated by two counsel, with inter-annotator agreement reported —
if two lawyers agree on MATERIAL vs MINOR only 70% of the time, that is the real
ceiling and every number above needs reading against it.

Also open: PDF ingestion (segmentation assumes numbered headings and degrades on
scanned paper — it flags low confidence rather than mis-segmenting silently, but
does not solve it); flat playbook rules with no conditional positions; and
single-document review, which is simply wrong for amendments that modify an MSA.

**With more time,** in order: measure the LLM backend against the same corpora so
the reported floor becomes a reported result; add the dual-annotation study;
batch clause extraction (≈5× fewer calls, at the cost of per-clause repair
granularity — the first thing to revisit under cost pressure); and attack the
correlated-retriever failure the adversarial queries exposed.
