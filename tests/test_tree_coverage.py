"""Tree-coverage audit, as tests (improvement plan S3, 2026-10-24;
design revised the same day on ladder evidence).

Every DECISIVE fact gets TWO readers: a model tree (its angle) and
the aggregator's deterministic RELEASE AUDIT, which downgrades any
RELEASE vote contradicted by a known-bad decisive fact in the union
of ground-truth facts. The first design — second MODEL readers —
was measured and rejected: the ladder gate fell 94.7% -> 87.7%
pooled because the added pairings are questions this checkpoint
answers badly (WITHIN_WINDOW: 0.500 from risk, 0.333 from
fulfillment; new pairings 0.821 vs 0.923 kept). These tests pin the
revised design so the coverage can't silently regress either way:
model reads stay at baseline, and the audit covers every decisive
fact.

SELLER_HISTORY_GOOD stays single-reader (risk) by design: it is
non-decisive — no Tier 0 set contains it and it never carries a
verdict alone — so it is also absent from the audit table.
"""
import pytest

from src import laya_forest, laya_questions as lq
from src.models import Verdict

DECISIVE = {"SHIPPED", "DELIVERED", "ADDRESS_MATCH", "WITHIN_WINDOW",
            "CONDITION_OK", "BUYER_COMPLAINT", "AMOUNT_MATCH"}


def test_every_decisive_fact_has_a_model_reader_and_the_audit():
    cov = lq.coverage()
    audit_facts = {fact for fact, _ in laya_forest.RELEASE_AUDIT}
    for fact in DECISIVE:
        assert cov[fact], f"{fact} has no model reader"
        assert fact in audit_facts, f"{fact} missing from release audit"


def test_seller_history_is_the_only_single_reader_by_design():
    cov = lq.coverage()
    assert cov["SELLER_HISTORY_GOOD"] == ["risk"]
    assert "SELLER_HISTORY_GOOD" not in {
        f for f, _ in laya_forest.RELEASE_AUDIT}


def test_model_reads_stay_at_the_measured_baseline():
    """Regression pin for the ladder finding: expanding model reads
    cost ~7 pooled points. If this fails, someone re-added pairings
    — the ladder gate must be re-run and must pass first."""
    assert lq.TREE_QUESTIONS == {
        "fulfillment": ("shipped", "delivered", "address_match"),
        "condition": ("condition_ok", "complaint"),
        "risk": ("shipped", "within_window", "amount_match",
                 "seller_history_good"),
    }


# --- release-audit behavior, through aggregate() ---

def _release_trees(**fact_overrides):
    """Three release-grade tree outputs over one shared fact set."""
    facts = {"SHIPPED": "yes", "DELIVERED": "yes",
             "ADDRESS_MATCH": "yes", "WITHIN_WINDOW": "yes",
             "CONDITION_OK": "yes", "BUYER_COMPLAINT": "no",
             "AMOUNT_MATCH": "yes", "SELLER_HISTORY_GOOD": "yes"}
    facts.update(fact_overrides)
    per_tree = {
        "fulfillment": ("SHIPPED", "DELIVERED", "ADDRESS_MATCH"),
        "condition": ("CONDITION_OK", "BUYER_COMPLAINT"),
        "risk": ("SHIPPED", "WITHIN_WINDOW", "AMOUNT_MATCH",
                 "SELLER_HISTORY_GOOD"),
    }
    answers = {
        "fulfillment": {"shipped": "yes", "delivered": "yes",
                        "address_match": "yes"},
        "condition": {"condition_ok": "yes", "complaint": "no"},
        "risk": {"shipped": "yes", "within_window": "yes",
                 "amount_match": "yes", "seller_history_good": "yes"},
    }
    return [{"tree": t, **answers[t],
             "_facts": {k: facts[k] for k in keys}}
            for t, keys in per_tree.items()]


def test_clean_facts_release_stands():
    agg = laya_forest.aggregate(_release_trees())
    assert agg["verdict"] == Verdict.RELEASE
    assert agg["release_vetoed"] == []


@pytest.mark.parametrize("fact,bad", [
    ("SHIPPED", "no"), ("WITHIN_WINDOW", "no"),
    ("CONDITION_OK", "no"), ("BUYER_COMPLAINT", "yes"),
    ("ADDRESS_MATCH", "no"), ("DELIVERED", "no"),
    ("AMOUNT_MATCH", "no"),
])
def test_audit_downgrades_release_on_any_known_bad_decisive_fact(
        fact, bad):
    agg = laya_forest.aggregate(_release_trees(**{fact: bad}))
    assert agg["verdict"] != Verdict.RELEASE
    assert agg["release_vetoed"], f"no veto recorded for {fact}"


def test_audit_ignores_unknown_facts():
    """UNKNOWNs never veto. (The verdict here is INVESTIGATE, but it
    comes from the trees' own certainty gating — RELEASE votes may
    not rest on guessed facts — not from the audit.)"""
    agg = laya_forest.aggregate(
        _release_trees(WITHIN_WINDOW="unknown", CONDITION_OK="unknown"))
    assert agg["release_vetoed"] == []
    assert agg["verdict"] == Verdict.INVESTIGATE


def test_audit_skipped_without_facts_legacy_shape():
    trees = [{k: v for k, v in t.items() if k != "_facts"}
             for t in _release_trees()]
    agg = laya_forest.aggregate(trees)
    assert agg["verdict"] == Verdict.RELEASE
    assert agg["release_vetoed"] == []
