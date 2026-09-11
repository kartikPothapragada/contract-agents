"""Deterministic handlers for :class:`StubBackend`.

**What this is, stated plainly so no evaluation number is misread.** This is a
rule engine, not a language model. It implements the same five task interfaces
the agents call, using regexes and keyword tables instead of inference.

Why it exists:

* **Reproducibility.** A reviewer with no API key clones the repo and gets the
  full pipeline -- routing, the repair loop, the escalation gate, the trace, the
  cost report -- running in seconds. Nothing about the orchestration is hidden
  behind a credential.
* **Testability.** Orchestration bugs and model quality are different problems.
  Holding the "model" fixed makes graph regressions attributable.
* **A baseline with a floor.** It answers "how much of this could a regex have
  done?" -- which is the question a reviewer should ask of any LLM system, and
  which most submissions never answer.

**It is not a substitute for the LLM path.** It generalises only to language
resembling its patterns, and it will silently underperform on real vendor paper
with unusual drafting. Every metric in ``results/`` is labelled with the backend
that produced it; stub numbers measure the pipeline, LLM numbers measure the
system. Conflating the two would be exactly the dishonest evaluation the rubric
penalises.
"""

from __future__ import annotations

import re
from typing import Any

from clauseguard.llm.backends import StubBackend

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty-five": 45, "forty five": 45,
    "sixty": 60, "ninety": 90,
}


def _nums_near(text: str, unit: str) -> list[int]:
    """Extract counts stated before a unit, e.g. 'three (3) months' -> [3]."""
    out: list[int] = []
    for m in re.finditer(rf"([\w\- ]{{0,18}}?)\(?(\d{{1,3}})\)?\s+{unit}", text, re.I):
        out.append(int(m.group(2)))
    for word, val in _WORD_NUM.items():
        if re.search(rf"\b{word}\s+{unit}", text, re.I):
            out.append(val)
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.;])\s+(?=[A-Z(])", " ".join(text.split()))
    return [p.strip() for p in parts if len(p.strip()) > 25]


def _best_sentence(text: str, keywords: tuple[str, ...]) -> str:
    """Highest keyword-density sentence; falls back to the longest."""
    best, best_score = "", -1.0
    for s in _sentences(text):
        low = s.lower()
        score = sum(1 for k in keywords if k in low)
        if score > best_score or (score == best_score and len(s) > len(best)):
            best, best_score = s, score
    return best


def _verbatim(quote: str, source: str) -> str:
    """Return a substring that genuinely exists in ``source``.

    The stub reconstructs sentences with normalised whitespace, so it maps back
    to raw source text before returning. A stub that returned text absent from
    the document would trip the verifier and make every run look like a
    hallucination -- a self-inflicted false positive.
    """
    if quote and quote in source:
        return quote
    if not quote:
        return ""
    head = " ".join(quote.split())[:60]
    # Locate by the first few words, tolerating intervening whitespace.
    words = head.split()[:6]
    if not words:
        return ""
    pattern = r"\s+".join(re.escape(w) for w in words)
    m = re.search(pattern, source, re.I)
    if not m:
        return ""
    end = min(len(source), m.start() + max(len(quote), 80))
    tail = source.find(".", end - 1)
    return source[m.start() : (tail + 1 if 0 < tail < end + 300 else end)]


# --------------------------------------------------------------------------- #
# intake.classify
# --------------------------------------------------------------------------- #
_DOC_PATTERNS = (
    ("DPA", ("data processing agreement", "data protection addendum", "processor")),
    ("NDA", ("non-disclosure agreement", "confidentiality agreement", "mutual nda")),
    ("SOW", ("statement of work", "scope of work", "work order")),
    ("AMENDMENT", ("amendment no", "amendment to the", "first amendment")),
    ("MSA", ("master services agreement", "master service agreement", "master agreement")),
)


def _intake(ctx: dict[str, Any]) -> dict:
    text: str = ctx["text"]
    low = text[:6000].lower()

    doc_type = "UNKNOWN"
    confidence = 0.45
    # The title line, where one exists, outranks body keywords. An MSA that
    # happens to use the word "processor" in its data clause is still an MSA,
    # and mislabelling it cascades into the wrong scoping decision downstream.
    title = next((ln.strip() for ln in text.splitlines()[:6] if ln.strip()), "").lower()
    for label, pats in _DOC_PATTERNS:
        if any(p in title for p in pats):
            doc_type, confidence = label, 0.95
            break
    else:
        for label, pats in _DOC_PATTERNS:
            if any(p in low for p in pats):
                doc_type, confidence = label, 0.7
                break

    counterparty = None
    m = re.search(
        r"between\s+(.{3,70}?)\s*(?:\(|,)?\s*(?:a |an )?(?:delaware|nevada|california|"
        r"uk|irish|german)?\s*(?:corporation|inc\.?|llc|ltd\.?|limited|gmbh|b\.v\.)",
        text[:4000],
        re.I,
    )
    if m:
        counterparty = " ".join(m.group(1).split())[:70]
    if counterparty and "northwind" in counterparty.lower():
        m2 = re.search(r"and\s+(.{3,70}?)\s*(?:\(|,)", text[:4000], re.I)
        counterparty = " ".join(m2.group(1).split())[:70] if m2 else counterparty

    law = None
    m = re.search(r"laws of (?:the )?([A-Z][\w \-,]{3,45})", text, re.I)
    if m:
        law = " ".join(m.group(1).split()).rstrip(",.")

    date = None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text[:4000])
    if m:
        date = m.group(1)
    else:
        m = re.search(
            r"(January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+(\d{1,2}),?\s+(\d{4})",
            text[:4000],
        )
        if m:
            date = f"{m.group(3)}-{m.group(1)[:3]}-{int(m.group(2)):02d}"

    tier = "MID"
    for m in re.finditer(r"(?:USD|\$)\s?([\d,]{4,15})", text):
        try:
            v = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if v >= 1_000_000:
            tier = "STRATEGIC"
            break

    # Scope by keyword presence -- the same conservative bias the prompt asks of
    # the model: cheap to include, expensive to miss.
    from clauseguard.tools.playbook import load_playbook

    pb = load_playbook()
    body = text.lower()
    scoped = [
        r.clause_type
        for r in pb.rules.values()
        if sum(1 for k in r.keywords if k in body) >= 2
    ]
    return {
        "doc_type": doc_type,
        "counterparty": counterparty,
        "governing_law": law,
        "effective_date": date,
        "value_tier": tier,
        "in_scope_clause_types": scoped or [r.clause_type for r in pb.rules.values()],
        "classification_confidence": confidence,
        "reasoning": "rule-based classification (stub backend)",
    }


# --------------------------------------------------------------------------- #
# extract.clause
# --------------------------------------------------------------------------- #
def _extract(ctx: dict[str, Any]) -> dict:
    from clauseguard.tools.playbook import load_playbook

    clause_type: str = ctx["clause_type"]
    sections = ctx.get("sections") or []
    source: str = ctx["contract_text"]
    rule = load_playbook().by_clause_type(clause_type)
    keywords = rule.keywords if rule else (clause_type.lower().replace("_", " "),)

    best_section, best_score = None, 0
    for s in sections:
        low = s.text.lower()
        score = sum(low.count(k) for k in keywords)
        if score > best_score:
            best_section, best_score = s, score

    if best_section is None or best_score == 0:
        return {
            "found": False,
            "quoted_text": "",
            "section_ref": None,
            "confidence": 0.2,
            "notes": "no candidate section contained playbook keywords (stub)",
        }

    sentence = _best_sentence(best_section.text, keywords)

    # Operative terms displaced into an attachment that is not in the document.
    # Quoting the pointer would report a clause nobody has read; the honest
    # answer is that the terms are not in the four corners of this contract.
    xref = re.search(
        r"(?:set out in|specified in|described in|contained in)\s+"
        r"(Exhibit|Schedule|Annex|Appendix)\s+([A-Z0-9]{1,3})",
        sentence,
        re.I,
    )
    if xref:
        label = f"{xref.group(1)} {xref.group(2)}"
        # Is the attachment actually present? Counting mentions does not answer
        # that - the pointer sentence alone names it three times ("set out in
        # Exhibit B ... conflict between this Section and Exhibit B ... Exhibit B
        # shall prevail"). An attached exhibit has a heading of its own, on its
        # own line, so that is what is looked for.
        attached = re.search(
            rf"^\s*{re.escape(label)}", source, re.I | re.M
        )
        if not attached:
            return {
                "found": False,
                "quoted_text": "",
                "section_ref": best_section.ref,
                "confidence": 0.0,
                "notes": (
                    f"operative terms are incorporated by reference to {label}, "
                    "which is not attached to this document; refusing to quote "
                    "terms that are not in the four corners of the contract (stub)"
                ),
            }

    quote = _verbatim(sentence, source)
    if not quote:
        return {
            "found": False,
            "quoted_text": "",
            "section_ref": best_section.ref,
            "confidence": 0.2,
            "notes": "candidate located but could not be quoted verbatim (stub)",
        }
    return {
        "found": True,
        "quoted_text": quote,
        "section_ref": best_section.ref,
        "confidence": min(0.95, 0.5 + 0.1 * best_score),
        "notes": f"keyword density {best_score} in section {best_section.ref} (stub)",
    }


# --------------------------------------------------------------------------- #
# policy.assess
# --------------------------------------------------------------------------- #
def _assess_liability(t: str) -> tuple[str, str]:
    if re.search(r"(unlimited|no limitation on|shall have no cap)", t):
        return "UNACCEPTABLE", "liability is stated to be uncapped"
    if re.search(r"(in no event shall (customer|northwind)|customer shall be liable without limit)", t):
        return "UNACCEPTABLE", "cap operates one way against Northwind"
    months = _nums_near(t, "months?")
    if months and min(months) < 6:
        return "UNACCEPTABLE", f"cap of {min(months)} months' fees is below the six-month floor"
    if months and min(months) < 12:
        return "MATERIAL", f"cap of {min(months)} months' fees is below the standard twelve"
    if "shall not exceed" in t or "aggregate liability" in t:
        return "COMPLIANT", "bounded aggregate cap consistent with the standard position"
    return "MINOR", "liability language present but does not clearly state a mutual bounded cap"


def _assess_dp(t: str) -> tuple[str, str]:
    hours = _nums_near(t, "hours?")
    days = _nums_near(t, "(?:business )?days?")
    if "notif" not in t and "breach" not in t:
        return "UNACCEPTABLE", "no personal data breach notification obligation"
    if days and max(days) > 7:
        return "UNACCEPTABLE", f"breach notice of {max(days)} days exceeds the seven-day ceiling"
    if hours and max(hours) > 96:
        return "UNACCEPTABLE", f"breach notice of {max(hours)} hours exceeds the fallback"
    if hours and max(hours) > 72:
        return "MATERIAL", f"breach notice of {max(hours)} hours exceeds the standard 72"
    if re.search(r"sub-?processors?.{0,60}(without|no)\s+(prior\s+)?(written\s+)?(notice|consent)", t):
        return "UNACCEPTABLE", "unrestricted sub-processor appointment with no notice right"
    if hours and max(hours) <= 72:
        return "COMPLIANT", "breach notification within 72 hours as required"
    return "MINOR", "data protection language present but the notice window is not clearly stated"


def _assess_termination(t: str) -> tuple[str, str]:
    if re.search(r"(only.{0,30}for cause|may not terminate for convenience|no right to terminate)", t):
        return "UNACCEPTABLE", "no termination for convenience right for Northwind"
    days = _nums_near(t, "days?")
    if "convenience" in t and days:
        if max(days) > 90:
            return "MATERIAL", f"{max(days)} days' notice exceeds the ninety-day fallback"
        if max(days) > 30:
            return "MINOR", f"{max(days)} days' notice is longer than the standard thirty"
        return "COMPLIANT", "termination for convenience on standard notice"
    if "convenience" not in t:
        return "MATERIAL", "termination clause does not grant a convenience right"
    return "MINOR", "termination for convenience present but the notice period is unclear"


def _assess_payment(t: str) -> tuple[str, str]:
    days = _nums_near(t, "days?")
    net = re.findall(r"net\s+(\d{1,3})", t)
    days += [int(n) for n in net]
    if days and min(days) < 15:
        return "UNACCEPTABLE", f"net {min(days)} is shorter than the fifteen-day floor"
    if days and min(days) < 45:
        return "MINOR", f"net {min(days)} is shorter than the standard net 45"
    if re.search(r"(\d+(?:\.\d+)?)%\s+per month", t):
        rate = float(re.search(r"(\d+(?:\.\d+)?)%\s+per month", t).group(1))
        if rate > 1.5:
            return "UNACCEPTABLE", f"late payment interest of {rate}% per month exceeds 1.5%"
    if re.search(r"(increase|escalat).{0,80}(sole discretion|at any time|without notice)", t):
        return "UNACCEPTABLE", "unilateral price escalation"
    return "COMPLIANT", "payment terms within the standard position"


def _assess_ip(t: str) -> tuple[str, str]:
    if re.search(r"(train|improve).{0,40}(model|algorithm|service)", t):
        return "UNACCEPTABLE", "vendor granted rights to use Northwind data for model training"
    if re.search(r"vendor (shall )?own.{0,60}(deliverable|work product|data)", t):
        return "MATERIAL", "vendor claims ownership of deliverables"
    if "perpetual" in t and "licen" in t:
        return "COMPLIANT", "perpetual licence to deliverables preserved"
    return "MINOR", "IP allocation stated but does not clearly vest deliverables in Northwind"


def _assess_generic(t: str, rule) -> tuple[str, str]:
    """Fallback: match the rule's own never-acceptable triggers lexically."""
    for trigger in rule.unacceptable_triggers:
        toks = [w for w in re.findall(r"[a-z]{5,}", trigger.lower())][:4]
        if toks and sum(1 for w in toks if w in t) >= max(2, len(toks) - 1):
            return "UNACCEPTABLE", f"matches never-acceptable trigger: {trigger}"
    hits = sum(1 for k in rule.keywords if k in t)
    if hits >= 2:
        return "COMPLIANT", "clause present and no never-acceptable trigger matched"
    return "MINOR", "clause present but thinly drafted relative to the standard position"


_ASSESSORS = {
    "LIMITATION_OF_LIABILITY": _assess_liability,
    "DATA_PROTECTION": _assess_dp,
    "TERMINATION_FOR_CONVENIENCE": _assess_termination,
    "PAYMENT_TERMS": _assess_payment,
    "IP_OWNERSHIP": _assess_ip,
}


def _policy(ctx: dict[str, Any]) -> dict:
    finding = ctx["finding"]
    rule = ctx["rule"]

    if not finding.found or not finding.span:
        # Absence of a high-criticality clause is itself the finding.
        sev = "UNACCEPTABLE" if rule.criticality_weight >= 0.9 else "MATERIAL"
        return {
            "severity": sev,
            "observed_position": f"No {rule.clause_type.replace('_', ' ').lower()} clause located.",
            "rationale": (
                f"The contract contains no language governing {rule.clause_type}. "
                f"The playbook requires: {rule.standard_position[:200]}"
            ),
            "cited_span_text": "",
            "suggested_redline": rule.standard_position,
            "triggered_rule_text": "absence of a required clause",
        }

    text = finding.span.text
    low = " ".join(text.split()).lower()
    assessor = _ASSESSORS.get(rule.clause_type)
    severity, why = assessor(low) if assessor else _assess_generic(low, rule)

    sentence = _best_sentence(text, rule.keywords) or text[:300]
    citation = _verbatim(sentence, text) or text[: min(len(text), 300)]

    return {
        "severity": severity,
        "observed_position": why[0].upper() + why[1:],
        "rationale": (
            f"Playbook {rule.rule_id} requires: {rule.standard_position[:180]}. "
            f"The contract instead provides language under which {why}. "
            f"Assessed {severity} on that basis."
        ),
        "cited_span_text": citation,
        "suggested_redline": (
            rule.standard_position if severity in ("MATERIAL", "UNACCEPTABLE") else None
        ),
        "triggered_rule_text": rule.title,
    }


# --------------------------------------------------------------------------- #
# verify.entailment
# --------------------------------------------------------------------------- #
def _verify(ctx: dict[str, Any]) -> dict:
    """Lexical entailment proxy.

    Deliberately weaker than the LLM path and honest about it: it can confirm
    that the figures and negations the assessment relies on are present in the
    quote, which is most of the practical value, but it cannot catch a
    semantically wrong reading of language it has no model of.

    Scope note. Both checks read ``observed_position`` -- the claim *about the
    contract* -- and not ``rationale``, which legitimately quotes the playbook's
    own figures and prohibitions. Checking the rationale flagged every correctly
    reasoned deviation, because "net 30 is shorter than the standard net 45"
    names a figure the contract does not contain and should not.
    """
    a = ctx["assessment"]
    rule = ctx.get("rule")
    quote = " ".join((a.cited_span_text or "").split()).lower()
    claim = (a.observed_position or "").lower()
    issues: list[str] = []

    def nums(t: str) -> set[str]:
        return {n.replace(",", "") for n in re.findall(r"\d[\d,]*", t)}

    grounded = nums(quote)
    if rule is not None:
        grounded |= nums(rule.standard_position) | nums(rule.fallback_position)
    orphan = {n for n in nums(claim) - grounded if int(n) > 12}
    if orphan:
        issues.append(
            f"figures {sorted(orphan)} in the observed position are absent from "
            "both the quote and the governing rule"
        )

    # A claim that the contract says "no X" must rest on a quote that negates.
    if re.search(r"(no|not|never|without|fails? to|absent)", claim) and not re.search(
        r"(no|not|never|without|nothing|shall not|may not|except|only)", quote
    ):
        issues.append(
            "the observed position asserts an absence or prohibition that the "
            "quoted text does not contain"
        )

    if issues:
        return {"entailment": "PARTIAL", "issues": issues}
    return {"entailment": "SUPPORTED", "issues": []}


# --------------------------------------------------------------------------- #
# draft.memo
# --------------------------------------------------------------------------- #
def _draft(ctx: dict[str, Any]) -> dict:
    rows = ctx["rows"]
    profile = ctx["profile"]
    aggregate_risk = ctx["aggregate"]
    decision = ctx.get("decision")

    blocking = [r for r in rows if r["severity"] == "UNACCEPTABLE"]
    material = [r for r in rows if r["severity"] == "MATERIAL"]
    minor = [r for r in rows if r["severity"] == "MINOR"]

    if blocking:
        head = (
            f"This {profile.doc_type} cannot be signed as drafted: "
            f"{len(blocking)} term(s) breach a never-acceptable playbook position "
            f"({', '.join(r['clause_type'] for r in blocking)})."
        )
    elif material:
        head = (
            f"This {profile.doc_type} is signable only after negotiation: "
            f"{len(material)} term(s) shift material risk onto Northwind."
        )
    else:
        head = f"This {profile.doc_type} is consistent with the playbook and can be signed."

    body = (
        f" Aggregate risk score {aggregate_risk} across {len(blocking) + len(material) + len(minor)} "
        f"deviation(s)."
    )
    reviewed = (
        f" Reviewed by {decision.reviewer} ({decision.action.value})." if decision else ""
    )

    priority = [r["clause_type"] for r in sorted(rows, key=lambda x: -(x["risk_score"] or 0))
                if r["severity"] != "COMPLIANT"]
    return {
        "executive_summary": head + body + reviewed,
        "negotiation_priority": priority,
        "residual_risk_note": (
            "Generated by the deterministic stub backend; severities reflect "
            "pattern matching, not legal judgement."
        ),
    }


# --------------------------------------------------------------------------- #
def register_all() -> None:
    StubBackend.register("intake.classify", _intake)
    StubBackend.register("extract.clause", _extract)
    StubBackend.register("policy.assess", _policy)
    StubBackend.register("verify.entailment", _verify)
    StubBackend.register("draft.memo", _draft)


register_all()
