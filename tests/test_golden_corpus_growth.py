"""Golden corpus growth set (workstream A5, 2026-10-24).

Copyright 2026 Brian Acosta. Licensed under the Apache License,
Version 2.0 (see LICENSE in the repo root).

72 adversarial cases generated programmatically by
scripts/generate_corpus.py and admitted only after screening
through the real decision path (see that script for the rules).
The cases live in tests/corpus_growth_cases.json; this file runs
each one through gather_evidence -> run_checklist -> build_state
-> adjudicate and asserts the constructed ground truth: the eight
determinate facts, the expected verdict, and the exact split cents
for SPLIT cases.

Three probe cases were generated, mismatched, and triaged in
writing as genuine adjudicator findings (partial-split address
guard gap x2; condition substring false positive on a praise
message). They are EXCLUDED from this corpus on purpose — the
details are recorded in the cases file's meta.screening.findings
and in the A5 report; enshrining a known-wrong verdict as a
golden case would hide the finding.

Grok is stubbed unavailable and the shadow forest is stubbed out,
exactly as the benchmark generator screens: both fallthrough
branches return INVESTIGATE when Tier 0 does not settle and Grok
is off, so verdicts here are Tier 0 / fallthrough truth.
"""
import json
from collections import Counter
from pathlib import Path

import pytest

from src import decision_engine
from src.checklist import run_checklist
from src.disputes import submit_evidence
from src.evidence import gather_evidence
from src.laya_forest import build_state
from src.models import Deal, DealTerms

DATA = Path(__file__).parent / "corpus_growth_cases.json"
_PAYLOAD = json.loads(DATA.read_text())
CASES = _PAYLOAD["cases"]


@pytest.fixture(autouse=True)
def _deterministic_tiers(monkeypatch):
    """Grok unavailable (documented fallback) + shadow stubbed:
    Tier 0 and the fallthrough decide every case here."""

    def _no_grok(deal, results):
        raise ImportError("XAI disabled in tests")

    monkeypatch.setattr(decision_engine, "_tier2_grok", _no_grok)
    monkeypatch.setattr(decision_engine, "_tier1_shadow",
                        lambda deal, results: None)


def _materialize(case):
    """Build (deal, results) via the real evidence/checklist path.
    Same mapping as the benchmark generator's materialize()."""
    terms = DealTerms(
        amount_cents=case["amount_cents"], currency="USD",
        item_description=case["item"],
        buyer_email="buyer@example.com",
        seller_email="seller@example.com",
        delivery_zip=case["delivery_zip"],
        window_days=case["window_days"])
    deal = Deal(id=case["id"], terms=terms)
    if case.get("buyer_confirmed"):
        deal.buyer_confirmed_at = "2026-11-05T12:00:00Z"
    if case.get("dispute_evidence"):
        deal.dispute_filed = True  # submissions require an open dispute
        for rec in case["dispute_evidence"]:
            submit_evidence(deal, rec["party"], rec["kind"],
                            text=rec.get("text", ""),
                            data=dict(rec.get("data") or {}))
    deal.dispute_filed = bool(case.get("dispute_filed"))
    evidence = gather_evidence(deal, case["tracking"],
                               case["days_since_funding"],
                               case.get("messages"))
    evidence["seller_completion_rate"] = case["seller_completion_rate"]
    if case.get("authorized_cents") is not None:
        evidence["authorized_cents"] = case["authorized_cents"]
    return deal, run_checklist(deal, evidence)


@pytest.mark.parametrize("case", CASES,
                         ids=[c["id"] for c in CASES])
def test_growth_case(case):
    deal, results = _materialize(case)
    # Constructed ground-truth facts, verified independently of the
    # verdict (a sensor-level regression fails here even if Tier 0
    # happens to land on the same verdict).
    assert build_state(deal, results) == case["facts"], case["id"]
    decision = decision_engine.adjudicate(deal, results)
    assert decision.verdict.value == case["expected_verdict"], (
        f"{case['id']}: {case['desc']}")
    if case["expected_verdict"] == "split":
        assert (decision.split_release_cents
                == case["expected_split_release_cents"]), case["id"]


def test_growth_corpus_totals_and_categories():
    meta = _PAYLOAD["meta"]
    assert meta["case_count"] == len(CASES) == 72
    assert meta["screening"]["generated"] == 75
    assert meta["screening"]["admitted"] == 72
    assert len(meta["screening"]["rejected"]) == 3
    # All three rejections are the triaged probe findings.
    assert len(meta["screening"]["findings"]) == 3
    by_cat = Counter(c["category"] for c in CASES)
    assert by_cat == {
        "amount_edges": 9, "window_edges": 9, "partial_deliveries": 9,
        "carrier_chaos": 9, "unknown_density": 7,
        "distractor_evidence": 9, "valence_mix": 10,
        "dispute_timing": 10}
    by_verdict = Counter(c["expected_verdict"] for c in CASES)
    assert by_verdict == {"release": 18, "refund": 20,
                          "investigate": 22, "split": 12}


def test_growth_corpus_unique_and_disjoint_from_benchmark():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    bench = json.loads(
        (Path(__file__).parent.parent / "eval"
         / "benchmark_cases.json").read_text())
    bench_ids = {c["id"] for c in bench["cases"]}
    assert not (set(ids) & bench_ids)
    bench_triples = {(c["amount_cents"], c["item"],
                      c["delivery_zip"]) for c in bench["cases"]}
    triples = {(c["amount_cents"], c["item"], c["delivery_zip"])
               for c in CASES}
    assert not (triples & bench_triples)
    assert len(triples) == len(CASES)
