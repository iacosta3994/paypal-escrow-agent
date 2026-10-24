"""Tests for the checklist + tier-0 adjudication (no LLM calls)."""
from src.checklist import run_checklist
from src.decision_engine import adjudicate
from src.evidence import gather_evidence
from src.models import Deal, DealTerms, Verdict


def make_deal() -> Deal:
    return Deal(
        id="test-1",
        terms=DealTerms(
            amount_cents=50000, currency="USD",
            item_description="camera",
            buyer_email="b@x.com", seller_email="s@x.com",
            delivery_zip="47172",
        ),
    )


def test_clean_delivery_releases_without_llm():
    deal = make_deal()
    tracking = {"number": "1Z1", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z",
                "delivered": True, "delivered_at": "2026-11-05T14:00:00Z",
                "delivered_zip": "47172"}
    ev = gather_evidence(deal, tracking, days_since_funding=5)
    results = run_checklist(deal, ev)
    d = adjudicate(deal, results)
    assert d.verdict == Verdict.RELEASE
    assert d.tier == 0 and d.cost_usd == 0.0


def test_never_ships_refunds_without_llm():
    deal = make_deal()
    tracking = {"number": "1Z2", "carrier": "UPS", "stalled_days": 9}
    ev = gather_evidence(deal, tracking, days_since_funding=9)
    results = run_checklist(deal, ev)
    d = adjudicate(deal, results)
    assert d.verdict == Verdict.REFUND
    assert d.tier == 0 and d.cost_usd == 0.0


# --- Shadow mode (2026-10-23, Option A): the Laya forest is a sensor,
# --- not a judge. Its verdict is logged, never returned.

from unittest.mock import patch

from src import laya_forest
from src.laya_forest import aggregate
from src.models import Decision


def _stub_shadow(verdict, confidence, votes):
    shadow = Decision(deal_id="stub", verdict=verdict,
                      confidence=confidence, reasoning="stub shadow",
                      tier=1, cost_usd=0.0)
    return laya_forest.ShadowResult(decision=shadow, votes=votes)


def test_shadow_disagreement_clean_delivery_investigates():
    """Scenario 1 mid-flight: Tier 0 can't settle yet. The forest says
    RELEASE — as a shadow that must NOT be returned; the case escalates
    and the disagreement lands in the audit fields."""
    deal = make_deal()
    tracking = {"number": "1Z1", "carrier": "UPS",
                "pickup_scan": "2026-11-02T10:00:00Z", "in_transit": True}
    ev = gather_evidence(deal, tracking, days_since_funding=3)
    results = run_checklist(deal, ev)
    stub = _stub_shadow(Verdict.RELEASE, 0.6667, {
        "fulfillment": Verdict.RELEASE, "condition": Verdict.RELEASE,
        "risk": Verdict.INVESTIGATE})
    with patch.object(laya_forest, "forest_shadow", return_value=stub):
        d = adjudicate(deal, results)
    assert d.verdict == Verdict.INVESTIGATE  # never the forest's RELEASE
    assert d.shadow_verdict == "release"
    assert d.shadow_confidence == 0.6667
    # 1 of 3 trees backed the final INVESTIGATE verdict.
    assert abs(d.agreement - 1 / 3) < 0.01
    assert any("shadow verdict: release" in e for e in d.key_evidence)


def test_shadow_disagreement_never_ships_investigates():
    """Scenario 2 before the stall threshold: the forest says REFUND —
    shadow-only, so the case escalates instead of refunding on the
    forest's word."""
    deal = make_deal()
    tracking = {"number": "1Z2", "carrier": "UPS", "stalled_days": 3}
    ev = gather_evidence(deal, tracking, days_since_funding=3)
    results = run_checklist(deal, ev)
    stub = _stub_shadow(Verdict.REFUND, 0.3333, {
        "fulfillment": Verdict.INVESTIGATE,
        "condition": Verdict.INVESTIGATE, "risk": Verdict.REFUND})
    with patch.object(laya_forest, "forest_shadow", return_value=stub):
        d = adjudicate(deal, results)
    assert d.verdict == Verdict.INVESTIGATE  # never the forest's REFUND
    assert d.shadow_verdict == "refund"
    assert d.shadow_confidence == 0.3333
    # 2 of 3 trees backed the final INVESTIGATE verdict.
    assert abs(d.agreement - 2 / 3) < 0.01


def test_ambiguous_condition_dispute_tier0_investigates():
    """Scenario 3: delivered but damaged. Tier 0 settles it as
    INVESTIGATE on its own — the shadow forest is not even consulted,
    and nothing can override Tier 0."""
    deal = make_deal()
    tracking = {"number": "1Z3", "carrier": "FedEx",
                "pickup_scan": "2026-11-03T09:00:00Z",
                "delivered": True, "delivered_at": "2026-11-06T11:00:00Z",
                "delivered_zip": "47172",
                "exception": "package visibly damaged"}
    ev = gather_evidence(deal, tracking, days_since_funding=5,
                         messages=["box arrived dented, not sure of model"])
    results = run_checklist(deal, ev)
    with patch.object(laya_forest, "forest_shadow",
                      side_effect=AssertionError("shadow must not run")):
        d = adjudicate(deal, results)
    assert d.verdict == Verdict.INVESTIGATE
    assert d.tier == 0
    assert d.shadow_verdict is None and d.agreement is None


def test_aggregate_unanimous_release_full_confidence():
    trees = [
        {"tree": "fulfillment", "shipped": "yes", "delivered": "yes"},
        {"tree": "condition", "condition_ok": "yes", "complaint": "no"},
        {"tree": "risk", "seller_failed": "no", "window_breached": "no"},
    ]
    agg = aggregate(trees)
    assert agg["verdict"] == Verdict.RELEASE
    assert agg["confidence"] == 1.0  # tree agreement, not the old 0.7


def test_aggregate_refund_majority():
    trees = [
        {"tree": "fulfillment", "shipped": "no", "delivered": "unknown"},
        {"tree": "condition", "condition_ok": "unknown", "complaint": "no"},
        {"tree": "risk", "seller_failed": "yes", "window_breached": "yes"},
    ]
    agg = aggregate(trees)
    assert agg["verdict"] == Verdict.REFUND
    assert abs(agg["confidence"] - 2 / 3) < 0.01


def test_aggregate_split_votes_investigate():
    trees = [
        {"tree": "fulfillment", "shipped": "yes", "delivered": "yes"},
        {"tree": "condition", "condition_ok": "no", "complaint": "yes"},
        {"tree": "risk", "seller_failed": "yes", "window_breached": "no"},
    ]
    agg = aggregate(trees)
    assert agg["verdict"] == Verdict.INVESTIGATE
    assert abs(agg["confidence"] - 1 / 3) < 0.01


def test_audit_records_shadow_fields():
    import os
    import tempfile

    from src import audit, config

    d = Decision(deal_id="t-shadow", verdict=Verdict.INVESTIGATE,
                 confidence=0.0, reasoning="escalated", tier=1,
                 cost_usd=0.0, shadow_verdict="release",
                 shadow_confidence=0.6667, agreement=0.3333)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "audit.jsonl")
        with patch.object(config, "AUDIT_LOG_PATH", path):
            audit.record(d)
            rows = audit.read_all()
    assert rows[0]["verdict"] == "investigate"
    assert rows[0]["shadow_verdict"] == "release"
    assert rows[0]["shadow_confidence"] == 0.6667
    assert rows[0]["agreement"] == 0.3333
