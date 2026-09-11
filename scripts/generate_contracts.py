"""Generate a labelled synthetic contract corpus.

Why synthetic, and what it costs
--------------------------------
The evaluation this system needs is *deviation severity against a specific
playbook*. No public corpus carries that label, because the label is a function
of one company's policy -- CUAD annotates where a clause is, not whether it is
acceptable to Northwind. So the severity ground truth has to be constructed.

The construction is inverted on purpose: a variant with a known severity is
chosen first, then rendered into contract prose. The label is therefore a
property of the generator, not of a human reading the output, which removes the
annotation noise that would otherwise dominate a 24-document set.

What this does not prove, stated plainly:

* Real vendor paper is longer, worse formatted, and drafts around these
  positions in ways no template bank captures. Extraction accuracy here is an
  upper bound.
* The prose is templated, so a model could in principle learn the template
  rather than the legal content. ``--adversarial`` exists to attack that: it
  emits the same severities in deliberately awkward drafting (defined terms,
  double negatives, cross-references, figures spelled out).
* Clause *location* is evaluated separately against CUAD
  (``scripts/eval_extraction_cuad.py``), which is real human-annotated text.
  Severity is synthetic; extraction is not.

Usage
-----
    python scripts/generate_contracts.py --n 24 --seed 42
    python scripts/generate_contracts.py --n 8 --adversarial --out data/contracts/adversarial
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Variant:
    severity: str
    body: str
    note: str


# --------------------------------------------------------------------------- #
# Variant banks. Each entry's severity is the ground-truth label.
# --------------------------------------------------------------------------- #
VARIANTS: dict[str, list[Variant]] = {
    "LIMITATION_OF_LIABILITY": [
        Variant(
            "COMPLIANT",
            "Except for the Excluded Claims, the aggregate liability of either party "
            "arising out of or related to this Agreement shall not exceed the greater "
            "of (a) the fees paid or payable in the twelve (12) months preceding the "
            "event giving rise to the claim and (b) USD 500,000. This limitation is "
            "mutual and applies to all claims other than the Excluded Claims.",
            "mutual bounded cap at the standard position",
        ),
        Variant(
            "MATERIAL",
            "The aggregate liability of Vendor arising out of or related to this "
            "Agreement shall not exceed the fees paid in the nine (9) months preceding "
            "the claim.",
            "cap below twelve months and one-directional",
        ),
        Variant(
            "UNACCEPTABLE",
            "The aggregate liability of Vendor arising out of or related to this "
            "Agreement shall not exceed the fees paid in the three (3) months "
            "preceding the claim. Customer shall be liable to Vendor without "
            "limitation for any breach of Section 9 (Fees).",
            "three-month cap plus uncapped liability running against Northwind",
        ),
    ],
    "DATA_PROTECTION": [
        Variant(
            "COMPLIANT",
            "Vendor acts as a processor and shall process Personal Data only on the "
            "documented instructions of Customer. Vendor shall notify Customer of any "
            "Personal Data Breach without undue delay and in any event within "
            "seventy-two (72) hours of becoming aware of it. Vendor shall not appoint "
            "a Sub-processor without prior written notice to Customer and shall afford "
            "Customer a right to object.",
            "72-hour notice with sub-processor notice right",
        ),
        Variant(
            "MATERIAL",
            "Vendor shall notify Customer of any Personal Data Breach within ninety-six "
            "(96) hours of confirming the incident. Vendor may appoint Sub-processors "
            "and shall publish an updated list on its website.",
            "96-hour notice, sub-processor notice replaced by a web page",
        ),
        Variant(
            "UNACCEPTABLE",
            "Vendor shall notify Customer of any security incident affecting Personal "
            "Data within thirty (30) days of completing its internal investigation. "
            "Vendor may engage Sub-processors at its sole discretion without notice to "
            "or consent from Customer.",
            "30-day notice and unrestricted sub-processors",
        ),
    ],
    "TERMINATION_FOR_CONVENIENCE": [
        Variant(
            "COMPLIANT",
            "Customer may terminate this Agreement for convenience upon thirty (30) "
            "days' prior written notice to Vendor, in which case Vendor shall refund "
            "any prepaid fees for the unused portion of the then-current term.",
            "30-day convenience right with pro-rata refund",
        ),
        Variant(
            "MINOR",
            "Customer may terminate this Agreement for convenience upon sixty (60) "
            "days' prior written notice, and Vendor shall refund prepaid unused fees.",
            "60-day notice, within the pre-approved fallback",
        ),
        Variant(
            "UNACCEPTABLE",
            "This Agreement may be terminated only for cause following an uncured "
            "material breach. Customer shall have no right to terminate for "
            "convenience, and all prepaid fees are non-refundable in all "
            "circumstances.",
            "no convenience right, fees forfeited",
        ),
    ],
    "PAYMENT_TERMS": [
        Variant(
            "COMPLIANT",
            "Customer shall pay all undisputed amounts within forty-five (45) days of "
            "receipt of a valid invoice. Vendor may increase fees on no less than "
            "ninety (90) days' notice, provided that no increase shall exceed the "
            "lesser of CPI or five percent (5%) per annum.",
            "net 45 with capped escalation",
        ),
        Variant(
            "MINOR",
            "Customer shall pay all undisputed amounts within thirty (30) days of "
            "receipt of a valid invoice, and Vendor shall apply a one percent (1%) "
            "discount for payment within ten (10) days.",
            "net 30 with early payment discount, within fallback",
        ),
        Variant(
            "UNACCEPTABLE",
            "Customer shall pay all invoiced amounts within ten (10) days of the "
            "invoice date. Overdue amounts accrue interest at three percent (3%) per "
            "month. Vendor may adjust fees at any time in its sole discretion.",
            "net 10, 3% monthly interest, unilateral escalation",
        ),
    ],
    "IP_OWNERSHIP": [
        Variant(
            "COMPLIANT",
            "All Deliverables created specifically for Customer under this Agreement "
            "shall be owned by Customer upon payment. Vendor retains ownership of its "
            "pre-existing materials and grants Customer a perpetual, worldwide, "
            "royalty-free licence to use such materials as embedded in the "
            "Deliverables.",
            "deliverables vest in Northwind",
        ),
        Variant(
            "UNACCEPTABLE",
            "Vendor shall own all Deliverables and all data derived from Customer's "
            "use of the Services. Customer grants Vendor a perpetual, irrevocable "
            "licence to use Customer Data to train and improve Vendor's models and "
            "service offerings.",
            "vendor owns deliverables and trains on customer data",
        ),
    ],
    "CONFIDENTIALITY": [
        Variant(
            "COMPLIANT",
            "Each party shall protect the Confidential Information of the other party "
            "with no less than reasonable care, and these obligations shall survive "
            "for three (3) years following termination. Trade secrets shall be "
            "protected for so long as they remain trade secrets.",
            "mutual, 3-year survival",
        ),
        Variant(
            "UNACCEPTABLE",
            "Customer shall protect the Confidential Information of Vendor. Nothing "
            "shall restrict Vendor from using any information retained in the unaided "
            "memory of its personnel. These obligations survive for six (6) months.",
            "one-way, residuals clause, short survival",
        ),
    ],
    "AUTO_RENEWAL": [
        Variant(
            "COMPLIANT",
            "This Agreement shall automatically renew for successive twelve (12) month "
            "terms unless either party gives written notice of non-renewal at least "
            "thirty (30) days prior to the end of the then-current term.",
            "12-month renewal with 30-day opt out",
        ),
        Variant(
            "MATERIAL",
            "This Agreement shall automatically renew for successive twelve (12) month "
            "terms unless either party gives written notice of non-renewal at least "
            "one hundred and twenty (120) days prior to the end of the then-current "
            "term.",
            "120-day notice window exceeds the ceiling",
        ),
    ],
    "GOVERNING_LAW": [
        Variant(
            "COMPLIANT",
            "This Agreement shall be governed by the laws of the State of Delaware, "
            "USA, without regard to its conflict of laws principles, and the parties "
            "submit to the exclusive jurisdiction of the courts located in Delaware.",
            "Delaware, standard position",
        ),
        Variant(
            "UNACCEPTABLE",
            "This Agreement shall be governed by the laws of the Republic of Singapore. "
            "Any dispute shall be finally resolved by arbitration seated in Singapore, "
            "and each party irrevocably waives any right to a jury trial.",
            "non-US/UK/EU forum with mandatory arbitration and jury waiver",
        ),
    ],
    "INDEMNIFICATION": [
        Variant(
            "COMPLIANT",
            "Vendor shall defend, indemnify and hold harmless Customer against any "
            "third party claim alleging that the Services infringe any intellectual "
            "property right, and against any claim arising from Vendor's breach of its "
            "security or data protection obligations.",
            "IP and data breach indemnity present",
        ),
        Variant(
            "UNACCEPTABLE",
            "Customer shall defend, indemnify and hold harmless Vendor against any "
            "third party claim arising from the use of the Services, including claims "
            "arising from any compromise of Vendor's systems.",
            "reverse indemnity covering vendor's own security failures",
        ),
    ],
    "SLA": [
        Variant(
            "COMPLIANT",
            "Vendor shall make the Services available at least 99.5% of the time in "
            "each calendar month, excluding maintenance notified at least five (5) "
            "business days in advance. Service credits escalate with the shortfall, "
            "and failure to meet the commitment in three consecutive months shall "
            "constitute a material breach.",
            "99.5% with escalating credits and chronic-failure exit",
        ),
        Variant(
            "MATERIAL",
            "Vendor shall use commercially reasonable efforts to make the Services "
            "available. Service credits shall be Customer's sole and exclusive remedy "
            "for any failure to meet any availability target.",
            "no committed uptime, credits as sole remedy",
        ),
    ],
}

# Adversarial re-drafts of the SAME severity, to test whether the system reads
# the terms or pattern-matches the template.
ADVERSARIAL: dict[str, list[Variant]] = {
    "LIMITATION_OF_LIABILITY": [
        Variant(
            "UNACCEPTABLE",
            "Notwithstanding anything to the contrary, the Liability Cap shall be as "
            "defined in Section 1.14. For the avoidance of doubt, nothing in this "
            "Section shall be construed to impose any ceiling whatsoever upon the "
            "obligations of Customer under Section 9.",
            "cap hidden behind a defined term; uncapped customer liability by double negative",
        ),
    ],
    "DATA_PROTECTION": [
        Variant(
            "UNACCEPTABLE",
            "Vendor shall provide notification of any Security Incident promptly, and "
            "in no case later than the period specified in Exhibit C, which the parties "
            "acknowledge is not less than one calendar month from the conclusion of "
            "Vendor's investigation.",
            "notice period displaced into an exhibit and stated in words",
        ),
    ],
    "PAYMENT_TERMS": [
        Variant(
            "UNACCEPTABLE",
            "Invoiced sums fall due upon the expiry of ten days from the invoice date. "
            "Sums remaining unpaid thereafter shall bear interest at the rate of thirty "
            "six percent per annum, calculated monthly.",
            "same economics as net-10 + 3%/month, expressed annually and in words",
        ),
    ],
}

BOILERPLATE = {
    "FORCE_MAJEURE": "Neither party shall be liable for any failure or delay in "
    "performance to the extent caused by circumstances beyond its reasonable control.",
    "NOTICES": "All notices shall be in writing and delivered to the addresses set "
    "out on the signature page or to such other address as a party may designate.",
    "ENTIRE_AGREEMENT": "This Agreement constitutes the entire agreement between the "
    "parties and supersedes all prior proposals and understandings.",
    "SEVERABILITY": "If any provision is held unenforceable, the remaining provisions "
    "shall continue in full force and effect.",
}

HEADINGS = {
    "LIMITATION_OF_LIABILITY": "Limitation of Liability",
    "DATA_PROTECTION": "Data Protection",
    "TERMINATION_FOR_CONVENIENCE": "Term and Termination",
    "PAYMENT_TERMS": "Fees and Payment",
    "IP_OWNERSHIP": "Intellectual Property",
    "CONFIDENTIALITY": "Confidentiality",
    "AUTO_RENEWAL": "Renewal",
    "GOVERNING_LAW": "Governing Law and Dispute Resolution",
    "INDEMNIFICATION": "Indemnification",
    "SLA": "Service Levels",
    "FORCE_MAJEURE": "Force Majeure",
    "NOTICES": "Notices",
    "ENTIRE_AGREEMENT": "Entire Agreement",
    "SEVERABILITY": "Severability",
}

VENDORS = [
    "Cirrus Analytics, Inc.", "Helioscope Software Ltd.", "Northbeam Data GmbH",
    "Quantic Logistics LLC", "Verdant Cloud Services, Inc.", "Orrery Systems B.V.",
    "Palisade Integrations, Inc.", "Marlowe Technical Services Limited",
]


def render_contract(
    doc_id: str,
    rng: random.Random,
    bank: dict[str, list[Variant]],
    *,
    drop_clause: str | None = None,
    cross_reference: str | None = None,
) -> tuple[str, dict]:
    vendor = rng.choice(VENDORS)
    tcv = rng.choice([45_000, 120_000, 380_000, 1_400_000, 2_600_000])
    tier = "SMALL" if tcv < 100_000 else ("MID" if tcv < 1_000_000 else "STRATEGIC")
    year = rng.choice([2025, 2026])
    month = rng.choice(
        ["January", "February", "March", "April", "May", "June", "September", "October"]
    )
    day = rng.randint(1, 28)

    clause_types = [c for c in bank if c != drop_clause]
    rng.shuffle(clause_types)
    ordered = clause_types + list(BOILERPLATE)
    rng.shuffle(ordered)

    lines = [
        "MASTER SERVICES AGREEMENT",
        "",
        f"This Master Services Agreement (the \"Agreement\") is entered into as of "
        f"{month} {day}, {year} (the \"Effective Date\") between Northwind Industries, "
        f"Inc., a Delaware corporation (\"Customer\"), and {vendor} (\"Vendor\").",
        "",
        f"The total contract value under this Agreement is USD {tcv:,}.",
        "",
        "1. Definitions",
        "",
        "Capitalised terms have the meanings given to them in this Agreement. "
        "\"Deliverables\" means the work product prepared by Vendor for Customer. "
        "\"Personal Data\" has the meaning given in applicable data protection law.",
        "",
    ]

    gold: dict[str, str] = {}
    notes: dict[str, str] = {}
    section_no = 2
    for clause_type in ordered:
        heading = HEADINGS.get(clause_type, clause_type.title())
        lines.append(f"{section_no}. {heading}")
        lines.append("")
        if clause_type in BOILERPLATE:
            lines.append(BOILERPLATE[clause_type])
        elif clause_type == cross_reference:
            # Edge case: operative terms live in an exhibit that is not attached.
            lines.append(
                f"The provisions governing {heading.lower()} are set out in Exhibit B "
                "(Commercial Terms), which is incorporated into this Agreement by "
                "reference and forms an integral part of it. In the event of any "
                "conflict between this Section and Exhibit B, Exhibit B shall prevail."
            )
            gold[clause_type] = "UNVERIFIABLE"
            notes[clause_type] = "operative terms incorporated by reference to a missing exhibit"
        else:
            variant = rng.choice(bank[clause_type])
            lines.append(variant.body)
            gold[clause_type] = variant.severity
            notes[clause_type] = variant.note
        lines.append("")
        section_no += 1

    if drop_clause:
        gold[drop_clause] = "ABSENT"
        notes[drop_clause] = "clause deliberately omitted from the contract"

    lines += [
        f"{section_no}. Counterparts",
        "",
        "This Agreement may be executed in counterparts, each of which shall be "
        "deemed an original.",
        "",
        "IN WITNESS WHEREOF, the parties have executed this Agreement as of the "
        "Effective Date.",
    ]

    meta = {
        "doc_id": doc_id,
        "vendor": vendor,
        "tcv_usd": tcv,
        "value_tier": tier,
        "effective_date": f"{year}-{month[:3]}-{day:02d}",
        "gold_severity": gold,
        "variant_notes": notes,
        "dropped_clause": drop_clause,
        "cross_referenced_clause": cross_reference,
    }
    return "\n".join(lines), meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data/contracts/synthetic")
    ap.add_argument(
        "--adversarial",
        action="store_true",
        help="use awkwardly drafted variants carrying the same severities",
    )
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    bank = dict(VARIANTS)
    if args.adversarial:
        for ct, variants in ADVERSARIAL.items():
            bank[ct] = variants

    manifest = []
    for i in range(args.n):
        doc_id = f"{'adv' if args.adversarial else 'syn'}-{i + 1:03d}"
        # Every 5th contract omits a required clause; every 7th buries one in an
        # unattached exhibit. Both are common in real vendor paper and both are
        # failure modes worth measuring, not edge cases worth avoiding.
        drop = "LIMITATION_OF_LIABILITY" if i % 5 == 4 else None
        xref = "LIMITATION_OF_LIABILITY" if (i % 7 == 6 and not drop) else None
        text, meta = render_contract(doc_id, rng, bank, drop_clause=drop, cross_reference=xref)
        (out / f"{doc_id}.txt").write_text(text, encoding="utf-8")
        manifest.append(meta)

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    dropped = sum(1 for m in manifest if m["dropped_clause"])
    xrefs = sum(1 for m in manifest if m["cross_referenced_clause"])
    print(f"wrote {len(manifest)} contracts to {out}")
    print(f"  {dropped} with a deliberately omitted clause")
    print(f"  {xrefs} with terms buried in an unattached exhibit")
    print(f"  manifest: {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
