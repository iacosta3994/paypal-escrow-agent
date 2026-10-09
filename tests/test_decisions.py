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
